"""decide() — every branch, with the real jobs that motivated each rule.

An audit of 461 classified jobs found 81 in the wrong tab because the model was
applying policy as well as reading JDs. These cases pin the policy so a threshold
change in CONFIG shows up as a test diff rather than as jobs quietly moving tabs.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decision_rules import (
    CHECK_REQS_PREFIX, CONFIG, CONFLICTED_PREFIX, JUNIOR_TITLE_PREFIX,
    UNVERIFIED_PREFIX, decide,
)


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


def test_mandatory_cs_degree_is_flagged_not_rejected():
    """Zeta. The candidate has an MBA and BMS, no B.Tech — but of 40 live PM JDs
    sampled on 2026-10-07 none stated a CS degree as a hard requirement, and this
    blocker was the sole cause of 60 Skips in a single run. It is a note now."""
    assert decide(features(hard_blockers=["mandatory_cs_or_engineering_degree"],
                           fit_score=95)) == ("Apply", CHECK_REQS_PREFIX)


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
    assert decide(features(is_pm_role=False, domain_class="adjacent",
                           fit_score=45)) == ("Skip", "")


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


def test_an_internship_is_still_rejected():
    """Nobody titles a senior role "intern", so that label can be acted on."""
    assert decide(features(title_level="intern", fit_score=90)) == ("Skip", "")


def test_apm_caps_at_maybe_instead_of_rejecting():
    """Flexiple's Product Owner was read as apm five times and pm five times across
    ten sightings of the one role. A label that flips cannot decide a tab."""
    assert decide(features(title_level="apm", fit_score=90)) == ("Maybe", JUNIOR_TITLE_PREFIX)


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
    assert decide(features(domain_class="non_core", fit_score=35)) == ("Maybe", "")
    assert decide(features(domain_class="non_core", fit_score=34)) == ("Skip", "")


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


# ── Skip needs categorical evidence (2026-10-09). The features in these cases are
# copied from the runs that produced the wrong tab, so a regression shows as a diff.
def test_the_zepto_row():
    """LinkedIn title "Product Manager", JD designation "Associate Product Manager I".
    Core domain, fit 62, no blockers — Skipped on the apm label alone."""
    assert decide(features(title_level="apm", years_min=2, years_max=None,
                           domain_class="core", fit_score=62)) == ("Maybe", JUNIOR_TITLE_PREFIX)


def test_the_ferns_n_petals_row():
    """A group PM at a flowers-and-gifts e-commerce company, rejected for a CS degree."""
    assert decide(features(title_level="lead_principal", years_min=6, years_max=9,
                           domain_class="adjacent", fit_score=45,
                           hard_blockers=["mandatory_cs_or_engineering_degree"])) == (
        "Maybe", CHECK_REQS_PREFIX)


def test_the_flexiple_row_lands_in_one_place_either_way():
    """Both readings of the same role must now agree on the tab."""
    apm = decide(features(title_level="apm", years_min=3, years_max=6,
                          domain_class="adjacent", fit_score=45))
    pm = decide(features(title_level="pm", years_min=4, years_max=8,
                         domain_class="adjacent", fit_score=62))
    assert apm[0] == pm[0] == "Maybe"


@pytest.mark.parametrize("blocker", ["mandatory_cs_or_engineering_degree",
                                     "other_mandatory_degree",
                                     "mandatory_platform_certification",
                                     "pure_supply_chain_logistics_saas"])
@pytest.mark.parametrize("domain", ["core", "adjacent", "non_core"])
def test_an_advisory_blocker_never_rejects_on_its_own(blocker, domain):
    decision, prefix = decide(features(domain_class=domain, fit_score=62,
                                       hard_blockers=[blocker]))
    assert decision != "Skip"
    assert CHECK_REQS_PREFIX in prefix


def test_a_supply_chain_role_inside_e_commerce_is_not_blocked():
    """The prompt calls this adjacent, not blocked. The rule used to disagree."""
    decision, _ = decide(features(domain_class="adjacent", fit_score=45,
                                  hard_blockers=["pure_supply_chain_logistics_saas"]))
    assert decision == "Maybe"


@pytest.mark.parametrize("blocker", ["deep_infra_security_networking",
                                     "clinical_or_payer_healthcare_ops",
                                     "post_trade_aml_compliance_accounting",
                                     "manufacturing_erp_industrial",
                                     "specialist_hardware"])
def test_a_categorical_blocker_still_rejects(blocker):
    """Cohesity, SonicWall, Siemens Healthineers — depth no PM craft substitutes for."""
    assert decide(features(domain_class="core", fit_score=90,
                           hard_blockers=[blocker])) == ("Skip", "")


def test_not_a_pm_role_with_a_strong_close_domain_fit_is_conflicted():
    """Growth Manager (core, fit 72) and CP Product Owner (core, fit 62) were both
    Skipped as "not a PM role" by a reply that scored them well in the same breath."""
    assert decide(features(is_pm_role=False, domain_class="core", fit_score=72)) == (
        "Maybe", CONFLICTED_PREFIX)


def test_not_a_pm_role_in_an_unrelated_domain_still_skips():
    """Moody's SAP revenue accounting, Novartis MarTech BA — correct rejections."""
    assert decide(features(is_pm_role=False, domain_class="non_core", fit_score=15)) == ("Skip", "")
    assert decide(features(is_pm_role=False, domain_class="core", fit_score=45)) == ("Skip", "")


def test_prefixes_stack_when_more_than_one_caveat_applies():
    assert decide(features(title_level="apm", domain_class="core", fit_score=72,
                           hard_blockers=["mandatory_cs_or_engineering_degree"])) == (
        "Maybe", JUNIOR_TITLE_PREFIX + CHECK_REQS_PREFIX)


def test_an_unreadable_jd_carries_only_the_unverified_note():
    """Blockers and a title level read off a garbage JD are not evidence of anything."""
    assert decide(features(jd_quality="garbage", title_level="apm",
                           hard_blockers=["mandatory_cs_or_engineering_degree"])) == (
        "Maybe", UNVERIFIED_PREFIX)


def test_a_stated_junior_ceiling_still_rejects_whatever_the_title_says():
    """Softening the apm label must not reopen the door to 0-2 year roles."""
    assert decide(features(title_level="apm", years_max=2, fit_score=90))[0] == "Skip"


def test_every_blocker_the_model_can_emit_is_classified():
    """A new enum in VALID_BLOCKERS that nobody sorted would silently do nothing."""
    import core_eval_hosted as core
    from decision_rules import ADVISORY_BLOCKERS, CATEGORICAL_BLOCKERS
    assert core.VALID_BLOCKERS == CATEGORICAL_BLOCKERS | ADVISORY_BLOCKERS
    assert not (CATEGORICAL_BLOCKERS & ADVISORY_BLOCKERS)
