"""An associate/junior posting must cost nothing to reject.

Rule 0 in decide() rejects it on the published title, which needs neither the JD nor
the model. Until 2026-10-27 the pipeline still fetched the JD and made the API call
first, then threw the answer away — about 5% of calls.

The row is still written. Dropping it outright would leave its URL out of the dedup
set, and a posting that stays up would be re-collected and re-fetched every 30 minutes.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core_eval_hosted as core
from decision_rules import decide


@pytest.fixture
def no_spending(monkeypatch):
    """Anything that costs money or bandwidth becomes an error."""
    def boom(*a, **k):
        raise AssertionError("a junior posting must not reach the network or the API")

    monkeypatch.setattr(core, "fetch_jd_text", boom)
    monkeypatch.setattr(core, "extract_features", boom)
    monkeypatch.setattr(core, "claude_structured", boom)


@pytest.mark.parametrize("title", [
    "Associate Product Manager",
    "Associate Product Manager - E-Commerce SaaS",
    "APM - toolkits",
    "Junior Product Owner",
    "Product Management Intern",
    "Assistant Product Manager (Supply chain )",
])
def test_a_junior_posting_costs_nothing(title, no_spending):
    out = core.evaluate_job_features({"title": title, "source": "LinkedIn",
                                      "url": "https://example.test/1"}, decide)
    assert out["decision"] == "Skip"
    assert out["features"]["title_is_junior"] is True


def test_the_row_is_still_written(no_spending):
    """It must look like a normal row to save_eval_jobs, or dedup loses the URL."""
    out = core.evaluate_job_features({"title": "Associate Product Manager",
                                      "source": "LinkedIn"}, decide)
    assert set(out) == {"decision", "reason", "gap", "jd", "features"}
    assert out["reason"]            # something a human can read in the sheet
    assert out["gap"] == "None"
    assert out["jd"] == ""          # nothing was fetched, so nothing to store


def test_the_decision_still_comes_from_the_rule_engine(no_spending):
    """Hardcoding "Skip" here would be a second copy of rule 0, free to drift."""
    calls = []

    def spy(features):
        calls.append(features)
        return "Skip", ""

    core.evaluate_job_features({"title": "APM - growth", "source": "LinkedIn"}, spy)
    assert calls == [{"title_is_junior": True}]


def test_a_senior_posting_is_not_short_circuited(monkeypatch):
    """The gate must not swallow ordinary titles — that was the Zepto failure."""
    seen = []
    monkeypatch.setattr(core, "fetch_jd_text", lambda job: seen.append("fetched") or "JD text")
    monkeypatch.setattr(core, "extract_features",
                        lambda job, jd=None, region_note="": ({"is_pm_role": True, "title_level": "senior",
                                               "title_is_junior": False, "domain_class": "core",
                                               "fit_score": 70, "jd_quality": "ok",
                                               "hard_blockers": [], "years_min": None,
                                               "years_max": None, "reason": "r", "gap": "None",
                                               "requires_managing_pms": None}, "JD text"))
    out = core.evaluate_job_features({"title": "Senior Product Manager",
                                      "source": "LinkedIn"}, decide)
    assert seen == ["fetched"]
    assert out["decision"] == "Apply"


def test_the_jd_quality_summary_ignores_rows_that_were_never_fetched(capsys):
    """Otherwise every title-rejected row shows up as an 'unknown' JD quality."""
    import pm_eval_hosted as pm
    jobs = [
        {"source": "LinkedIn", "evaluation": {"features": {"title_is_junior": True}}},
        {"source": "LinkedIn", "evaluation": {"features": {"jd_quality": "ok"}}},
    ]
    pm._print_jd_quality_summary(jobs)
    out = capsys.readouterr().out
    assert "unknown" not in out
    assert "1 ok" in out
