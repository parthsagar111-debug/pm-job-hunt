"""decide() — every branch, with the real jobs that motivated each rule.

An audit of 461 classified jobs found 81 in the wrong tab because the model was
applying policy as well as reading JDs. These cases pin the policy so a threshold
change in CONFIG shows up as a test diff rather than as jobs quietly moving tabs.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decision_rules import CONFIG, UNVERIFIED_PREFIX, decide


def features(**overrides):
    """A plausible mid-range job; each test overrides only what it is about."""
    base = {
        "is_pm_role": True,
        "title_level": "senior",
        "years_min": None,
        "years_max": None,
        "domain_class": "core",
        "hard_blockers": [],
        "requires_managing_pms": None,
        "stated_max_ctc_lpa": None,
        "jd_quality": "ok",
        "fit_score": 70,
        "reason": "reason",
        "gap": "None",
    }
    base.update(overrides)
    return base


# ── the ten outcomes from the brief
def test_snapmint_search_core_high_fit_applies():
    assert decide(features(domain_class="core", fit_score=85, years_min=5, years_max=8)) == ("Apply", "")


def test_purplle_brand_platform_core_applies():
    assert decide(features(domain_class="core", fit_score=80, years_min=3, years_max=6)) == ("Apply", "")


def test_policybazaar_core_with_a_three_year_ceiling_is_maybe():
    assert decide(features(domain_class="core", years_min=2, years_max=3, fit_score=75)) == ("Maybe", "")


def test_solarsquare_adjacent_with_a_three_year_ceiling_is_skipped():
    assert decide(features(domain_class="adjacent", years_min=2, years_max=3, fit_score=70)) == ("Skip", "")


def test_wrike_adjacent_mid_fit_is_maybe():
    assert decide(features(domain_class="adjacent", fit_score=55, years_min=4)) == ("Maybe", "")


@pytest.mark.parametrize("domain", ["core", "adjacent", "non_core"])
def test_infra_blocker_skips_whatever_the_domain(domain):
    """Cohesity, SonicWall — storage/networking depth isn't learnable in a notice period."""
    assert decide(features(domain_class=domain, fit_score=90,
                           hard_blockers=["deep_infra_security_networking"])) == ("Skip", "")


def test_mandatory_cs_degree_skips():
    """Zeta — the candidate has an MBA and BMS, no B.Tech."""
    assert decide(features(hard_blockers=["mandatory_cs_or_engineering_degree"],
                           fit_score=95)) == ("Skip", "")


def test_garbage_jd_is_maybe_and_flagged():
    """Never Skip on an absence of evidence — the IIMJobs category menu is not a finding."""
    decision, prefix = decide(features(jd_quality="garbage", is_pm_role=False, fit_score=0))
    assert decision == "Maybe"
    assert prefix == UNVERIFIED_PREFIX


def test_thin_jd_is_also_unverified():
    decision, prefix = decide(features(jd_quality="thin"))
    assert (decision, prefix) == ("Maybe", UNVERIFIED_PREFIX)


def test_product_marketing_role_is_skipped():
    """Cyara Sr PMM — a marketing role, however 'product' the title sounds."""
    assert decide(features(is_pm_role=False, fit_score=70)) == ("Skip", "")


def test_low_stated_ctc_skips():
    """Super Plastronics at 9 LPA — below the floor, whatever the fit."""
    assert decide(features(stated_max_ctc_lpa=9, fit_score=90)) == ("Skip", "")


# ── rule ordering and the remaining branches
def test_unreadable_jd_beats_every_other_rule():
    """Checked first on purpose: 'not a PM role' read off a nav menu is meaningless."""
    decision, prefix = decide(features(
        jd_quality="garbage", is_pm_role=False,
        hard_blockers=["deep_infra_security_networking"], title_level="apm"))
    assert (decision, prefix) == ("Maybe", UNVERIFIED_PREFIX)


@pytest.mark.parametrize("level", ["intern", "apm"])
def test_junior_titles_skip(level):
    assert decide(features(title_level=level, fit_score=90)) == ("Skip", "")


def test_two_year_ceiling_skips_even_in_core():
    assert decide(features(domain_class="core", years_max=2, fit_score=90)) == ("Skip", "")


def test_four_year_ceiling_continues_in_core_but_not_adjacent():
    assert decide(features(domain_class="core", years_max=4, fit_score=70)) == ("Apply", "")
    assert decide(features(domain_class="adjacent", years_max=5, fit_score=90)) == ("Maybe", "")


def test_no_years_stated_falls_through_to_fit():
    """Most JDs state no numbers; that must not itself change the outcome."""
    assert decide(features(years_min=None, years_max=None,
                           domain_class="core", fit_score=80)) == ("Apply", "")


def test_over_senior_is_maybe_not_skip():
    assert decide(features(title_level="vp_plus", fit_score=90)) == ("Maybe", "")
    assert decide(features(years_min=14, fit_score=90)) == ("Maybe", "")
    assert decide(features(requires_managing_pms=5, fit_score=90)) == ("Maybe", "")


def test_over_senior_and_unrelated_domain_skips():
    assert decide(features(title_level="vp_plus", domain_class="non_core", fit_score=90)) == ("Skip", "")


def test_managing_three_pms_is_not_over_senior():
    """The candidate has managed 2 APMs; 3 is a stretch, not a disqualifier."""
    assert decide(features(requires_managing_pms=3, domain_class="core", fit_score=70)) == ("Apply", "")


def test_fit_thresholds_by_domain():
    assert decide(features(domain_class="core", fit_score=65)) == ("Apply", "")
    assert decide(features(domain_class="core", fit_score=64)) == ("Maybe", "")
    assert decide(features(domain_class="adjacent", fit_score=80)) == ("Apply", "")
    assert decide(features(domain_class="adjacent", fit_score=79)) == ("Maybe", "")
    assert decide(features(domain_class="non_core", fit_score=50)) == ("Maybe", "")
    assert decide(features(domain_class="non_core", fit_score=49)) == ("Skip", "")


def test_missing_fit_score_does_not_crash():
    out = decide({"is_pm_role": True, "jd_quality": "ok", "domain_class": "non_core",
                  "hard_blockers": [], "title_level": "senior"})
    assert out == ("Skip", "")


def test_thresholds_are_configurable():
    """CONFIG is the one place to tune; decide() must actually read it."""
    loosened = dict(CONFIG, FIT_APPLY_CORE=50)
    assert decide(features(domain_class="core", fit_score=55)) == ("Maybe", "")
    assert decide(features(domain_class="core", fit_score=55), loosened) == ("Apply", "")


def test_ctc_at_the_floor_is_not_skipped():
    assert decide(features(stated_max_ctc_lpa=CONFIG["MIN_CTC_LPA"], fit_score=70))[0] == "Apply"
