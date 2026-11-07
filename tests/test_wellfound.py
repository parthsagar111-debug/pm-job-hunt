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


def test_no_per_job_fetch_however_many_pages(one_page):
    """The requests are per page, never per job — that is what keeps the source free."""
    calls = one_page(page([listing(str(i), "Product Manager", ["Pune"]) for i in range(20)]))
    jobs = core.fetch_wellfound()
    assert len(jobs) == 20                              # deduped by id across pages
    assert len(calls) <= len(core.WELLFOUND_CITIES) * core.WELLFOUND_MAX_PAGES
    assert all(c.startswith("https://wellfound.com/role/l/") for c in calls)


# ── pagination (2026-11-07)
#
# Page 1 is a small slice: of 35 unique India PM listings inside the 30-day window,
# only 11 were on page 1. Reading page 1 alone missed 69% of the source — Avacasa
# and the Daloopa pair among them — and the gap was invisible because the run
# reported "0 new" perfectly truthfully about the slice it had read.

def _paged(monkeypatch, pages_by_city):
    """Serve a different document per (city, page); record what was asked for."""
    calls = []

    def fake(url, **kw):
        calls.append(url)
        city = url.split("/product-manager/")[1].split("?")[0]
        n = int(url.split("page=")[1]) if "page=" in url else 1
        pages = pages_by_city.get(city, [])
        return _Resp(pages[n - 1] if n <= len(pages) else pages[0])   # past the end -> page 1

    monkeypatch.setattr(core, "_LI_GET", fake)
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    return calls


def test_listings_past_page_one_are_collected(monkeypatch):
    p1 = page([listing("a", "Product Manager", ["Pune"])])
    p2 = page([listing("b", "Senior Product Manager", ["Pune"])])
    p3 = page([listing("c", "Group Product Manager", ["Pune"])])
    _paged(monkeypatch, {c: [p1, p2, p3] for c in core.WELLFOUND_CITIES})
    assert {j["job_id"] for j in core.fetch_wellfound()} == {"wf_a", "wf_b", "wf_c"}


def test_a_fresh_listing_deep_in_the_pages_is_not_missed(monkeypatch):
    """Depth is not just backlog: a 1.4-day-old listing sat on page 4 in the probe,
    so stopping at the first page without fresh rows would drop live jobs."""
    stale = [page([listing(f"old{p}", "Product Manager", ["Pune"], age_days=25)])
             for p in range(3)]      # distinct pages, as the real ones are
    fresh = page([listing("new", "Product Manager", ["Pune"], age_days=1)])
    _paged(monkeypatch, {c: stale + [fresh] for c in core.WELLFOUND_CITIES})
    assert "wf_new" in {j["job_id"] for j in core.fetch_wellfound()}


def test_a_page_past_the_end_does_not_restart_the_city(monkeypatch):
    """Wellfound serves page 1 again rather than 404ing, so the loop has to notice
    it is being handed what it already read — otherwise every city burns the full
    page budget re-reading page 1 and the counts still look healthy."""
    p1 = page([listing("a", "Product Manager", ["Pune"])])
    p2 = page([listing("b", "Product Manager", ["Pune"])])
    calls = _paged(monkeypatch, {c: [p1, p2] for c in core.WELLFOUND_CITIES})
    jobs = core.fetch_wellfound()

    per_city = len(calls) / len(core.WELLFOUND_CITIES)
    assert per_city == 3            # page 1, page 2, then page 3 = page 1 again -> stop
    assert per_city < core.WELLFOUND_MAX_PAGES
    assert {j["job_id"] for j in jobs} == {"wf_a", "wf_b"}


def test_the_page_budget_is_bounded(monkeypatch):
    """Every page distinct: the cap, not the data, has to stop it."""
    pages = [page([listing(f"j{n}", "Product Manager", ["Pune"])]) for n in range(50)]
    calls = _paged(monkeypatch, {c: pages for c in core.WELLFOUND_CITIES})
    core.fetch_wellfound()
    assert len(calls) == len(core.WELLFOUND_CITIES) * core.WELLFOUND_MAX_PAGES


def test_the_budget_is_deep_enough_for_what_was_measured():
    """11 of 35 listings were on page 1; page 5 still held one that pages 1-4 did not."""
    assert core.WELLFOUND_MAX_PAGES >= 5


def test_an_empty_page_ends_the_city(monkeypatch):
    p1 = page([listing("a", "Product Manager", ["Pune"])])
    calls = _paged(monkeypatch, {c: [p1, page([])] for c in core.WELLFOUND_CITIES})
    core.fetch_wellfound()
    assert len(calls) == len(core.WELLFOUND_CITIES) * 2


def test_one_bad_page_does_not_cost_the_remaining_cities(monkeypatch):
    seen = {"n": 0}
    html = page([listing("a", "Product Manager", ["Pune"])])

    def fake(url, **kw):
        seen["n"] += 1
        if seen["n"] == 1:
            return _Resp("", status=503)
        return _Resp(html)

    monkeypatch.setattr(core, "_LI_GET", fake)
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    assert core.fetch_wellfound()        # the other cities still produced their listing


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
