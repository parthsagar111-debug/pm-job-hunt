"""LinkedIn card parsing, pagination, and the Sheets row/format helpers."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core_eval_hosted as core
import sheets_writer as sw


def card(job_id, title, company="ACME", location="Berlin, Germany", posted="2 hours ago"):
    return f"""
    <div class="base-card">
      <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/{job_id}?trk=x"></a>
      <h3 class="base-search-card__title">{title}</h3>
      <h4 class="base-search-card__subtitle">{company}</h4>
      <span class="job-search-card__location">{location}</span>
      <time datetime="2026-09-17">{posted}</time>
    </div>"""


def test_parse_extracts_fields_and_normalises_url():
    jobs, ids = core._parse_li_cards(card("4123456789", "Senior Product Manager"))
    assert ids == {"4123456789"}
    job = jobs[0]
    assert job["title"] == "Senior Product Manager"
    assert job["company"] == "ACME"
    assert job["location"] == "Berlin, Germany"
    assert job["url"] == "https://www.linkedin.com/jobs/view/4123456789"   # tracking param dropped
    assert job["job_id"] == "li_4123456789"


def test_parse_filters_non_pm_titles_but_still_counts_the_ids():
    """Page ids drive the pagination stop condition, so they must include rows the
    title filter rejects — otherwise paging stops early on a page of non-PM jobs."""
    html = card("4111111111", "Senior Product Manager") + card("4222222222", "Warehouse Associate")
    jobs, ids = core._parse_li_cards(html)
    assert len(jobs) == 1
    assert ids == {"4111111111", "4222222222"}


def test_parse_survives_a_malformed_card():
    html = card("4111111111", "Product Manager") + '<div class="base-card"><h3>broken</h3></div>'
    jobs, ids = core._parse_li_cards(html)
    assert len(jobs) == 1


def test_pagination_stops_when_a_page_adds_nothing(monkeypatch):
    pages = [card("41", "Product Manager"),
             card("42", "Product Manager"),
             card("42", "Product Manager")]   # repeat -> stop
    calls = {"n": 0}

    class R:
        def __init__(self, text): self.text = text

    def fake_get(url):
        i = calls["n"]
        calls["n"] += 1
        return R(pages[min(i, len(pages) - 1)])

    monkeypatch.setattr(core, "_li_get_with_retry", fake_get)
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    jobs = core.fetch_linkedin("Product Manager", "24h", location="Germany", paginate=True)
    assert calls["n"] == 3          # first page + one new page + one repeat
    assert len(jobs) == 2           # deduped across pages


def test_search_returns_empty_when_linkedin_is_unreachable(monkeypatch):
    monkeypatch.setattr(core, "_li_get_with_retry", lambda url: None)
    assert core.fetch_linkedin("Product Manager", "24h") == []


def test_guest_jd_needs_a_numeric_id(monkeypatch):
    monkeypatch.setattr(core, "_li_get_with_retry", lambda url: None)
    assert core.fetch_jd_guest("not-an-id") == ""


def test_guest_jd_extracts_description(monkeypatch):
    class R:
        text = '<div class="show-more-less-html__markup">Line one\nLine two</div>'
    monkeypatch.setattr(core, "_li_get_with_retry", lambda url: R())
    assert "Line one" in core.fetch_jd_guest("li_4123456789")


# ── sheets helpers
def test_clean_url_strips_query():
    assert sw._clean_url("https://www.linkedin.com/jobs/view/123?trk=abc") == "https://www.linkedin.com/jobs/view/123"


def test_url_pattern_matches_every_source_we_write():
    for url in ["https://www.linkedin.com/jobs/view/4123456789",
                "https://www.naukri.com/job-listings-product-manager-acme-12345",
                "https://www.iimjobs.com/j/some-role-123456",
                "https://www.hirist.tech/j/some-role-123456"]:
        assert sw._URL_PATTERN.match(url), url


def test_jd_column_is_capped_but_written_for_every_decision():
    assert sw._jd_for_row("") == ""
    assert len(sw._jd_for_row("x" * 99999)) == sw.JD_MAX_CHARS


def test_compact_rows_builds_clip_and_height_requests():
    """Row height is what stops the multi-line JD cell inflating every row."""
    class WS:
        def __init__(self, i): self.id, self.row_count, self.title = i, 5000, f"t{i}"

    class SH:
        body = None
        def fetch_sheet_metadata(self):
            return {"sheets": [{"properties": {"sheetId": 1, "gridProperties": {"rowCount": 6000}}}]}
        def batch_update(self, body):
            self.body = body

    sh = SH()
    sw._compact_rows(sh, [WS(1)])
    kinds = [list(r)[0] for r in sh.body["requests"]]
    assert kinds == ["repeatCell", "updateDimensionProperties"]
    clip, height = sh.body["requests"][0]["repeatCell"], sh.body["requests"][1]["updateDimensionProperties"]
    assert clip["range"]["startRowIndex"] == 1                            # header row untouched
    assert clip["range"]["endRowIndex"] == 6000                           # uses live grid size, not stale row_count
    assert clip["cell"]["userEnteredFormat"]["wrapStrategy"] == "CLIP"
    assert height["properties"]["pixelSize"] == sw.ROW_HEIGHT_PX


def test_compact_rows_never_raises_on_api_failure(capsys):
    class SH:
        def fetch_sheet_metadata(self): raise RuntimeError("sheets down")
    sw._compact_rows(SH(), [])          # cosmetic only — must not fail a save
    assert "couldn't compact" in capsys.readouterr().out


# ── fingerprint TTL
def test_is_recent_accepts_both_date_formats():
    from datetime import datetime, timedelta
    cutoff = sw._now_ist() - timedelta(days=21)
    fresh = sw._now_ist().strftime("%Y-%m-%d %H:%M")
    assert sw._is_recent(fresh, cutoff) is True
    assert sw._is_recent(sw._now_ist().strftime("%Y-%m-%d"), cutoff) is True


def test_is_recent_rejects_old_and_unparseable_rows():
    """Old rows must not block new postings: a company|title seen months ago is a new
    opening today, not a re-post. An unreadable date costs one evaluation rather than
    hiding a job."""
    from datetime import timedelta
    cutoff = sw._now_ist() - timedelta(days=21)
    old = (sw._now_ist() - timedelta(days=60)).strftime("%Y-%m-%d %H:%M")
    assert sw._is_recent(old, cutoff) is False
    assert sw._is_recent("", cutoff) is False
    assert sw._is_recent("not a date", cutoff) is False


def test_fingerprint_load_only_keeps_recent_rows():
    from datetime import timedelta
    fresh = sw._now_ist().strftime("%Y-%m-%d %H:%M")
    old   = (sw._now_ist() - timedelta(days=90)).strftime("%Y-%m-%d %H:%M")

    class WS:
        def row_values(self, n):
            return ["Month", "Date Found", "Title", "Company", "Location", "URL"]
        def col_values(self, col):
            return {2: ["Date Found", fresh, old],
                    3: ["Title", "Product Manager", "Product Manager"],
                    4: ["Company", "RecentCo", "AncientCo"],
                    5: ["Location", "Mumbai, India", "Mumbai, India"]}[col]

    class SH:
        def worksheet(self, name): return WS()

    keys = sw._load_seen_fingerprints(SH(), ("Apply",))
    assert any("recentco" in k for k in keys)
    assert not any("ancientco" in k for k in keys)
