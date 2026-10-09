"""The 7-day backfill path.

The scheduled runs ask LinkedIn for the last 24 hours and read only the search
page, which LinkedIn caps at ~60 results per keyword. That cap is silent: on a
busy day the 61st job simply never existed as far as the feed was concerned. Week
mode exists to go back and get them, so these tests pin that it (a) paginates and
(b) widens the freshness filter, while the 24h default keeps both off.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core_eval_hosted as core


def test_multi_search_does_not_paginate_by_default(monkeypatch):
    seen = []

    def fake(kw, time_range, paginate=False, limit=None, **kw2):
        seen.append({"kw": kw, "time_range": time_range, "paginate": paginate, "limit": limit})
        return []

    monkeypatch.setattr(core, "fetch_linkedin", fake)
    core.fetch_linkedin_multi("ignored")
    assert len(seen) == len(core.LINKEDIN_KEYWORDS)
    assert all(c["paginate"] is False for c in seen)
    assert all(c["time_range"] == "24h" for c in seen)


def test_multi_search_forwards_pagination_and_window(monkeypatch):
    seen = []

    def fake(kw, time_range, paginate=False, limit=None, **kw2):
        seen.append({"time_range": time_range, "paginate": paginate, "limit": limit})
        return []

    monkeypatch.setattr(core, "fetch_linkedin", fake)
    core.fetch_linkedin_multi("ignored", time_range="week", paginate=True, limit=2000)
    assert all(c == {"time_range": "week", "paginate": True, "limit": 2000} for c in seen)


def test_every_keyword_is_still_searched(monkeypatch):
    """Nine keywords, deduplicated by job_id — a backfill must not lose one."""
    def fake(kw, time_range, paginate=False, limit=None, **kw2):
        return [{"job_id": "li_" + kw.replace(" ", ""), "title": kw},
                {"job_id": "li_shared", "title": "Product Manager"}]

    monkeypatch.setattr(core, "fetch_linkedin", fake)
    out = core.fetch_linkedin_multi("ignored", time_range="week", paginate=True)
    assert len(out) == len(core.LINKEDIN_KEYWORDS) + 1   # the shared id appears once


def test_the_week_window_is_seven_days():
    assert core._LI_TPR["week"] == "r604800"
    assert core._LI_TPR["24h"] == "r86400"


@pytest.mark.parametrize("posted,in_day,in_week", [
    ("2 hours ago", True, True),
    ("3 days ago", False, True),
    ("6 days ago", False, True),
    ("2 weeks ago", False, False),
])
def test_freshness_filters_differ(posted, in_day, in_week):
    job = {"posted": posted}
    assert core.within_24hrs(job) is in_day
    assert core.within_week(job) is in_week


def test_pm_eval_defaults_to_24h(monkeypatch):
    """A scheduled run must never accidentally become a backfill."""
    monkeypatch.delenv("TIME_RANGE", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub")
    import importlib
    import pm_eval_hosted
    importlib.reload(pm_eval_hosted)
    assert pm_eval_hosted.TIME_RANGE == "24h"


def test_pm_eval_reads_the_env_var(monkeypatch):
    monkeypatch.setenv("TIME_RANGE", "  WEEK  ")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub")
    import importlib
    import pm_eval_hosted
    importlib.reload(pm_eval_hosted)
    assert pm_eval_hosted.TIME_RANGE == "week"
    monkeypatch.delenv("TIME_RANGE")
    importlib.reload(pm_eval_hosted)
