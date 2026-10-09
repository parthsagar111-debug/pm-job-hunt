"""Who counts as a junior posting, decided from the published title.

Two opposite mistakes were made here within a fortnight:

- The model's title_level "apm" was a hard reject, which binned a Zepto "Product
  Manager" (core, fit 62, no blockers) because the JD body said "Associate Product
  Manager I" somewhere.
- Softening that label put 29 genuine associate postings into Maybe in one backfill,
  including "Associate Product Manager @ BigBasket" at fit 92.

The title the employer published is a fact and settles it; the model's reading of
seniority only caps the outcome. The titles below are the real ones from the
2026-10-09 run, 41 jobs the model called "apm", of which 29 were genuinely junior.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core_eval_hosted as core
from decision_rules import JUNIOR_TITLE_PREFIX, decide

JUNIOR_TITLES = [
    "Associate Product Manager",
    "Associate Product Manager (Hotels)",
    "Associate Product Manager - E-Commerce SaaS",
    "Associate Product Manager – Finance Tech",
    "Senior Associate Product Manager",
    "Associate Product Owner",
    "Associate Technical Product Manager",
    "Assistant Product Manager (Supply chain )",
    "APM - toolkits",
    "Product & Operations - APM | 2-3 yrs | Startup | B2C",
    "Business Analyst-APM",
    "Junior Product Owner",
    "Jr. Product Manager",
    "Product Management Trainee",
    "Graduate Product Analyst",
    "Product Management Intern",
    "Entry-level Product Manager",
    "Associate - Digital Product Management",
]

SENIOR_TITLES = [
    "Product Manager",
    "Senior Product Manager",
    "Group Product Manager - Supply Chain & Operations Domain",
    "Principal Product Manager",
    "Lead Product Manager- Technical",
    "Director of Product",
    "Head of Product",
    "Growth Manager",
    "CP Product Owner",
    "Product Owner",
    "Staff Product Manager",
    "Product Manager II",
    # "Associate" as a band, but these are senior roles
    "Associate Director - Product Management",
    "Associate Director/Director - Product",
    "Associate Vice President - Product",
    "Associate Principal - Product Management",
    # India's "Assistant Manager" band runs well into mid-level
    "Assistant Manager, Product Management",
    "Deputy Manager - Product Management",
]


@pytest.mark.parametrize("title", JUNIOR_TITLES)
def test_a_junior_title_is_recognised(title):
    assert core.title_says_junior(title) is True


@pytest.mark.parametrize("title", SENIOR_TITLES)
def test_a_senior_title_is_not(title):
    assert core.title_says_junior(title) is False


def features(**overrides):
    base = {
        "is_pm_role": True, "title_level": "apm", "years_min": 1, "years_max": 3,
        "domain_class": "core", "hard_blockers": [], "requires_managing_pms": None,
        "stated_max_ctc_lpa": None, "jd_quality": "ok", "fit_score": 92,
        "reason": "reason", "gap": "None", "title_is_junior": False,
    }
    base.update(overrides)
    return base


def test_a_genuine_associate_posting_is_rejected():
    """Associate Product Manager @ BigBasket — core domain, fit 92, still a step down."""
    assert decide(features(title_is_junior=True)) == ("Skip", "")


def test_a_genuine_associate_posting_is_rejected_whatever_the_model_called_it():
    for level in ("apm", "pm", "senior", "lead_principal"):
        assert decide(features(title_is_junior=True, title_level=level))[0] == "Skip"


def test_an_ordinary_title_the_model_called_apm_only_caps_at_maybe():
    """Product Manager @ Zepto — the label came from the JD body, not the title."""
    assert decide(features(title_is_junior=False, fit_score=62)) == (
        "Maybe", JUNIOR_TITLE_PREFIX)


def test_a_junior_title_rejects_even_when_the_jd_never_loaded():
    """The title needs no JD to be true, and IIMJobs/Hirist JDs fail often enough
    that waiting for one would park associate postings in Maybe."""
    assert decide(features(title_is_junior=True, jd_quality="garbage")) == ("Skip", "")
    assert decide(features(title_is_junior=True, jd_quality="thin")) == ("Skip", "")


def test_the_computed_flag_beats_a_contradicting_model_label():
    """Both directions, in one place, so neither regression can come back."""
    assert decide(features(title_is_junior=True, title_level="senior"))[0] == "Skip"
    assert decide(features(title_is_junior=False, title_level="apm"))[0] == "Maybe"


def test_extract_features_sets_the_flag_from_the_scraped_title(monkeypatch):
    """It must come from the job dict, never from the model's reply."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub")
    monkeypatch.setattr(core, "build_feature_prompt", lambda: "prefix")
    reply = {"is_pm_role": True, "title_level": "senior", "years_min": 5, "years_max": 8,
             "domain_class": "core", "hard_blockers": [], "requires_managing_pms": None,
             "stated_max_ctc_lpa": None, "jd_quality": "ok", "fit_score": 70,
             "reason": "r", "gap": "None"}
    monkeypatch.setattr(core, "claude_structured", lambda *a, **k: (dict(reply), {}))
    monkeypatch.setattr(core, "fetch_jd_text", lambda job: "Own the roadmap. " * 40)

    junior, _ = core.extract_features({"title": "Associate Product Manager", "source": "LinkedIn"})
    senior, _ = core.extract_features({"title": "Senior Product Manager", "source": "LinkedIn"})
    assert junior["title_is_junior"] is True
    assert senior["title_is_junior"] is False
