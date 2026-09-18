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


# ── global visa feed: sponsors vs rejects go to different tabs
class FakeWS:
    def __init__(self, title):
        self.title, self.id, self.row_count, self.rows = title, hash(title) % 100, 5000, []
    def append_rows(self, rows, value_input_option=None):
        self.rows.extend(rows)


class FakeSheet:
    def __init__(self, existing_urls=()):
        self.tabs = {}
        self.existing = list(existing_urls)
    def worksheet(self, name):
        if name not in self.tabs:
            self.tabs[name] = FakeWS(name)
        return self.tabs[name]
    def fetch_sheet_metadata(self):
        return {"sheets": [{"properties": {"sheetId": ws.id, "gridProperties": {"rowCount": 5000}}}
                           for ws in self.tabs.values()]}
    def batch_update(self, body):
        pass


def _patch_sheets(monkeypatch, sheet, seen=()):
    monkeypatch.setattr(sw, "_get_client", lambda: type("C", (), {"open_by_key": lambda s, k: sheet})())
    monkeypatch.setattr(sw, "_load_seen_urls", lambda sh, tabs=None: set(seen))
    monkeypatch.setattr(sw, "_ensure_tab", lambda sh, name, headers: sh.worksheet(name))


def _visa_job(url, verdict, evidence="quote", jd="x" * 500):
    return {"url": url, "visa_verdict": verdict, "visa_evidence": evidence, "jd": jd,
            "title": "Product Manager", "company": "ACME", "location": "Berlin, Germany",
            "posted": "2 hours ago", "source": "LinkedIn Global"}


def test_sponsors_and_rejects_land_in_separate_tabs(monkeypatch):
    sheet = FakeSheet()
    _patch_sheets(monkeypatch, sheet)
    n_yes, n_no = sw.save_visa_jobs(
        "sheet-id",
        [_visa_job("https://www.linkedin.com/jobs/view/1", "YES")],
        [_visa_job("https://www.linkedin.com/jobs/view/2", "NO", "we cannot sponsor visas")],
    )
    assert (n_yes, n_no) == (1, 1)
    listings = sheet.tabs[sw.TAB_LISTINGS].rows
    rejects  = sheet.tabs[sw.TAB_NO_SPONSOR].rows
    assert listings[0][2] == "YES"
    assert rejects[0][2] == "NO" and "cannot sponsor" in rejects[0][3]


def test_reject_rows_carry_no_jd_column():
    """180 rejects a day at 45,000 chars each would bloat the sheet for nothing."""
    assert sw.HEADERS_VISA_NO == sw.HEADERS_VISA[:-1]
    assert "JD" not in sw.HEADERS_VISA_NO


def test_reject_rows_are_shorter_than_listing_rows(monkeypatch):
    sheet = FakeSheet()
    _patch_sheets(monkeypatch, sheet)
    sw.save_visa_jobs("sheet-id",
                      [_visa_job("https://www.linkedin.com/jobs/view/1", "YES")],
                      [_visa_job("https://www.linkedin.com/jobs/view/2", "NO")])
    assert len(sheet.tabs[sw.TAB_LISTINGS].rows[0]) == len(sw.HEADERS_VISA)
    assert len(sheet.tabs[sw.TAB_NO_SPONSOR].rows[0]) == len(sw.HEADERS_VISA_NO)


def test_already_seen_urls_are_skipped_in_both_tabs(monkeypatch):
    sheet = FakeSheet()
    _patch_sheets(monkeypatch, sheet, seen={"https://www.linkedin.com/jobs/view/1",
                                            "https://www.linkedin.com/jobs/view/2"})
    assert sw.save_visa_jobs("sheet-id",
                             [_visa_job("https://www.linkedin.com/jobs/view/1", "YES")],
                             [_visa_job("https://www.linkedin.com/jobs/view/2", "NO")]) == (0, 0)


def test_writes_listings_even_with_no_rejects(monkeypatch):
    sheet = FakeSheet()
    _patch_sheets(monkeypatch, sheet)
    assert sw.save_visa_jobs("sheet-id", [_visa_job("https://www.linkedin.com/jobs/view/9", "CONDITIONAL")]) == (1, 0)
    assert sw.TAB_NO_SPONSOR not in sheet.tabs
