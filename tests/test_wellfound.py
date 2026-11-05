"""The Wellfound source.

Measured on 2026-11-05 across nine city pages: 185 unique listings, 82 passing the PM
title gate, 10 posted within 7 days, 45 within 30, median age 27 days. So it is a
backlog plus a trickle, not a daily feed — which is why it carries its own 30-day cut
instead of the shared 24-hour filter, and why URL dedup is what stops the backlog
being re-read on every run.

Two properties are worth protecting: the JD arrives inline, so the source costs no
per-job request and cannot be throttled; and yearsExperienceMin/Max come through as
numbers, which is the field the classifier reads least reliably out of prose.
"""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core_eval_hosted as core

NOW = time.time()


def listing(job_id, title, locations, age_days=5, desc="Own the roadmap. " * 30,
            years=(3, 6), remote=False):
    return {
        "__typename": "JobListingSearchResult",
        "id": job_id,
        "title": title,
        "slug": title.lower().replace(" ", "-"),
        "description": desc,
        "locationNames": locations,
        "remote": remote,
        "liveStartAt": NOW - age_days * 86400,
        "yearsExperienceMin": years[0],
        "yearsExperienceMax": years[1],
    }


def page(listings, company="Acme"):
    """A __NEXT_DATA__ document shaped like the real one."""
    data = {}
    refs = []
    for j in listings:
        key = f"JobListingSearchResult:{j['id']}"
        data[key] = j
        refs.append({"__ref": key})
    data["StartupResult:1"] = {"__typename": "StartupResult", "name": company,
                               "highlightedJobListings": refs}
    doc = {"props": {"pageProps": {"apolloState": {"data": data}}}}
    return ('<html><body><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(doc) + "</script></body></html>")


class _Resp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status


@pytest.fixture
def one_page(monkeypatch):
    """Serve the same page for every city, so counts are easy to reason about."""
    def serve(html):
        calls = []

        def fake(url, **kw):
            calls.append(url)
            return _Resp(html)

        monkeypatch.setattr(core, "_LI_GET", fake)
        monkeypatch.setattr(core.time, "sleep", lambda s: None)
        return calls
    return serve


def test_a_listing_becomes_a_job_row(one_page):
    one_page(page([listing("4901355", "Product Manager", ["Bengaluru"])]))
    jobs = core.fetch_wellfound()
    assert jobs, "expected at least one listing"
    j = jobs[0]
    assert j["source"] == "Wellfound"
    assert j["job_id"] == "wf_4901355"
    assert j["title"] == "Product Manager"
    assert j["company"] == "Acme"
    assert j["location"] == "Bengaluru"
    assert j["url"] == "https://wellfound.com/jobs/4901355-product-manager"


def test_the_jd_arrives_inline_and_costs_no_request(one_page, monkeypatch):
    """The whole reason this source cannot be throttled."""
    one_page(page([listing("1", "Product Manager", ["Mumbai"])]))
    job = core.fetch_wellfound()[0]

    def boom(*a, **k):
        raise AssertionError("fetch_jd_text must not make a request for Wellfound")

    monkeypatch.setattr(core, "_li_get_with_retry", boom)
    monkeypatch.setattr(core, "fetch_jd_guest", boom)
    monkeypatch.setattr(core, "make_driver", boom)
    jd = core.fetch_jd_text(job)
    assert len(jd) > core.MIN_JD_CHARS
    assert not core.is_garbage_jd(jd)


def test_one_request_per_city_and_no_per_job_fetch(one_page):
    calls = one_page(page([listing(str(i), "Product Manager", ["Pune"]) for i in range(20)]))
    jobs = core.fetch_wellfound()
    assert len(calls) == len(core.WELLFOUND_CITIES)   # one page each, nothing per job
    assert len(jobs) == 20                            # deduped by id across pages


def test_listings_older_than_the_window_are_dropped(one_page):
    one_page(page([
        listing("fresh", "Product Manager", ["Delhi"], age_days=3),
        listing("stale", "Product Manager", ["Delhi"],
                age_days=core.WELLFOUND_MAX_AGE_DAYS + 5),
    ]))
    ids = {j["job_id"] for j in core.fetch_wellfound()}
    assert ids == {"wf_fresh"}


def test_the_window_is_wide_enough_to_be_worth_having():
    """A 24h cut would have returned 3 of 185 listings in the probe."""
    assert core.WELLFOUND_MAX_AGE_DAYS >= 14


def test_listings_outside_india_are_dropped(one_page):
    """The India pages leak: London, Seattle, New York and San Francisco all showed up
    among the freshest roles in the probe."""
    one_page(page([
        listing("in", "Product Manager", ["Bengaluru"]),
        listing("uk", "Lead product manager (UK)", ["London"]),
        listing("us", "Forward Deployed Product Manager", ["New York"]),
        listing("sf", "Lead product manager", ["Seattle", "San Francisco"]),
    ]))
    ids = {j["job_id"] for j in core.fetch_wellfound()}
    assert ids == {"wf_in"}


def test_a_remote_listing_with_no_city_is_kept(one_page):
    one_page(page([listing("r", "Product Manager", [], remote=True)]))
    assert [j["location"] for j in core.fetch_wellfound()] == ["Remote"]


@pytest.mark.parametrize("years,expected", [
    ((3, 6), "3 - 6 yrs"),
    ((5, None), "5+ yrs"),
    ((None, 4), "up to 4 yrs"),
    ((None, None), ""),
])
def test_structured_experience_is_passed_through(one_page, years, expected):
    """Wellfound states this as a number on ~60% of listings; the model reads it out
    of prose unreliably, so it goes into the Stated Experience field."""
    one_page(page([listing("1", "Product Manager", ["Noida"], years=years)]))
    assert core.fetch_wellfound()[0]["stated_experience"] == expected


def test_a_page_that_fails_does_not_sink_the_run(monkeypatch):
    """One bad city page must not cost the other seven."""
    html = page([listing("1", "Product Manager", ["Pune"])])
    seen = {"n": 0}

    def flaky(url, **kw):
        seen["n"] += 1
        if seen["n"] == 1:
            return _Resp("", status=503)
        if seen["n"] == 2:
            return _Resp("<html>no next data here</html>")
        return _Resp(html)

    monkeypatch.setattr(core, "_LI_GET", flaky)
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    jobs = core.fetch_wellfound()
    assert len(jobs) == 1          # the surviving pages still produced their listing


def test_the_source_is_registered():
    names = [n for n, _ in core.SOURCES]
    assert "Wellfound" in names
    assert core.SOURCE_ICONS.get("Wellfound")


def test_pm_eval_exempts_wellfound_from_the_daily_filter():
    """Its median listing is 27 days old, so the 24h filter would discard nearly all
    of it. The fetcher's own 30-day cut is what bounds the source."""
    import io
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "pm_eval_hosted.py")
    body = io.open(path, encoding="utf-8").read()
    assert 'name not in ("LinkedIn", "Wellfound")' in body
