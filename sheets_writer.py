"""
sheets_writer.py — Google Sheets output for pm_eval
"""

import os
import json
import re
import time
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

def _now_ist():
    return datetime.now(IST)

import gspread
from google.oauth2.service_account import Credentials

def _with_retry(fn, *args, retries: int = 5, base_delay: float = 1.5, **kwargs):
    """Call fn with exponential backoff on transient Google API errors (429/500/502/503)."""
    last_err = None
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            status = None
            try:
                status = e.response.status_code
            except Exception:
                pass
            if status in (429, 500, 502, 503) and attempt < retries - 1:
                delay = base_delay * (2 ** attempt)
                print(f"  [sheets] Transient API error ({status}), retrying in {delay:.1f}s "
                      f"(attempt {attempt + 1}/{retries})...")
                time.sleep(delay)
                last_err = e
                continue
            raise
    if last_err:
        raise last_err

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

TAB_APPLY = "Apply"
TAB_MAYBE = "Maybe"
TAB_SKIP  = "Skip"

# Global visa feed: one plain listing tab, no Apply/Maybe/Skip fit judgment.
TAB_LISTINGS   = "Listings"
TAB_NO_SPONSOR = "No Sponsorship"
HEADERS_VISA   = [
    "Month", "Date Found", "Visa", "Evidence", "Title", "Company",
    "Location", "Posted", "Source", "URL", "JD",
    # Company-level signal from the UK/NL government sponsor registers. Deliberately
    # its own column and never a decision: a licence means the employer CAN sponsor,
    # not that they will for this role. Appended last so existing rows stay aligned.
    "Sponsor licence",
]
# Rejects skip the JD cell — they're for scanning, not for storing 45k-char cells.
HEADERS_VISA_NO = [h for h in HEADERS_VISA if h != "JD"]

HEADERS_EVAL = [
    "Month", "Date Found", "Title", "Company", "Location",
    "Source", "Decision", "Reason", "Gap", "URL", "JD",
]

# Sheets caps cell contents at 50,000 chars — stay well under that.
JD_MAX_CHARS = 45000

def _jd_for_row(jd_text: str) -> str:
    """Write JD for every decision — Apply, Maybe, AND Skip alike — since the
    fetch already happened for every job regardless of outcome (evaluate_job
    calls fetch_jd_text before it even knows the decision). Previously Skip
    was excluded specifically because _load_seen_urls used to scan every
    cell on every tab on every run; that's fixed below (URL-column-only, not
    a full-grid scan) so storing JD on Skip no longer costs anything there.
    2026-08-2x, Parth's call: job-automation's Agent 1 no longer live-fetches
    JD for the PM Eval sheet at all (see agent1_hosted.py's _eval_jd_fetcher)
    — the scraper is now the ONLY place this ever gets fetched, so it has to
    actually capture it for every row, not just Apply/Maybe."""
    return jd_text[:JD_MAX_CHARS] if jd_text else ""

_URL_PATTERN = re.compile(
    r'https?://(www\.)?('
    r'linkedin\.com/jobs/view/|naukri\.com/job-listings-|iimjobs\.com/j/|hirist\.tech/j/|'
    r'adzuna\.[a-z.]+/'      # Adzuna redirect links, or dedup would re-add them daily
    r')\S+'
)

def _get_client() -> gspread.Client:
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        info  = json.loads(sa_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        path  = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE",
                               os.path.join(os.path.dirname(__file__), "service_account.json"))
        creds = Credentials.from_service_account_file(path, scopes=SCOPES)
    return gspread.authorize(creds)

def _ensure_tab(sh: gspread.Spreadsheet, name: str, headers: list) -> gspread.Worksheet:
    """Get or create tab. Writes headers only if row 1 is empty, and appends any
    header this code writes that the tab doesn't have yet — a new column added to
    the end would otherwise arrive as an unlabelled column on an existing sheet.
    Existing header cells are never reordered or overwritten, so old rows stay aligned."""
    try:
        ws = sh.worksheet(name)
        # Only fix headers if row 1 is empty — never overwrite on existing data tabs
        existing = [h.strip() for h in (_with_retry(ws.row_values, 1) or [])]
        if existing and any(h not in existing for h in headers):
            missing = [h for h in headers if h not in existing]
            start   = len(existing) + 1
            try:
                ws.update_cell(1, start, missing[0]) if len(missing) == 1 else ws.update(
                    f"{gspread.utils.rowcol_to_a1(1, start)}", [missing],
                    value_input_option="USER_ENTERED")
                print(f"  [sheets] {name}: added column header(s) {missing}")
            except Exception as e:
                print(f"  [sheets] Warning: couldn't add header(s) {missing} to {name}: {e}")
        first_cell = existing[0] if existing else ""
        if not first_cell.strip():
            ws.update("A1", [headers], value_input_option="USER_ENTERED")
            try:
                ws.format(f"A1:{chr(64 + len(headers))}1", {"textFormat": {"bold": True}})
            except Exception:
                pass
        return ws
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=5000, cols=len(headers))
        ws.append_row(headers, value_input_option="USER_ENTERED")
        try:
            ws.format(f"A1:{chr(64 + len(headers))}1", {"textFormat": {"bold": True}})
        except Exception:
            pass
        return ws

def _clean_url(url: str) -> str:
    return url.split("?")[0].strip() if url else ""

def _load_seen_urls(sh: gspread.Spreadsheet, tabs: tuple = (TAB_APPLY, TAB_MAYBE, TAB_SKIP)) -> set[str]:
    """Reads only the URL column (found by header name) on each tab — not a
    full-grid scan. Skip carries JD text too (see _jd_for_row), up to 45,000
    chars per cell, and this runs on every single scraper invocation — a full
    get_all_values() over that would download and re-scan all of it for
    nothing this function reads. Still immune to column misalignment the way
    a full-cell scan was: the URL column is located by its header text each
    call, never a hardcoded position."""
    seen = set()
    for tab in tabs:
        try:
            ws = sh.worksheet(tab)
            headers = _with_retry(ws.row_values, 1)
            if "URL" not in headers:
                print(f"  [sheets] Warning: {tab} has no URL header — skipping dedup read for it.")
                continue
            url_col = headers.index("URL") + 1
            for cell in _with_retry(ws.col_values, url_col)[1:]:  # skip header row
                if cell and _URL_PATTERN.match(cell.strip()):
                    seen.add(_clean_url(cell.strip()))
        except gspread.WorksheetNotFound:
            pass
        except Exception as e:
            print(f"  [sheets] Warning: dedup read failed for {tab}: {e}")
    print(f"  [sheets] Dedup: {len(seen)} existing URLs loaded")
    return seen

def load_seen_urls(spreadsheet_id: str, tabs: tuple = (TAB_APPLY, TAB_MAYBE, TAB_SKIP)) -> set:
    """
    Public entry point: open the sheet and return the set of already-seen job URLs.

    Callers MUST use this to filter jobs BEFORE sending them to the Claude API for
    evaluation — not just at write time. Evaluating jobs that are already in the
    sheet burns API tokens for nothing, since save_eval_jobs() will just drop them
    again on write.

    Raises on failure (after retries) rather than returning an empty set, so a
    transient Sheets outage doesn't silently look like "nothing has ever been seen"
    and trigger a full-price re-evaluation of everything.
    """
    client = _get_client()
    sh     = _with_retry(client.open_by_key, spreadsheet_id)
    return _load_seen_urls(sh, tabs=tabs)

ROW_HEIGHT_PX = 21   # Sheets' default single-line row height

def _compact_rows(sh: gspread.Spreadsheet, worksheets: list) -> None:
    """Keep every data row one line tall. The JD cell is multi-line text, and
    Sheets auto-grows a row to fit newlines — so clip wrapping and pin the row
    height on all data rows. Applied to the whole tab each run (one request),
    so it also fixes rows written before this existed. Cosmetic only: never
    fails the save."""
    try:
        meta = _with_retry(sh.fetch_sheet_metadata)   # appends can grow the grid past ws.row_count
        grid_rows = {s["properties"]["sheetId"]: s["properties"]["gridProperties"]["rowCount"]
                     for s in meta["sheets"]}
    except Exception as e:
        print(f"  [sheets] Warning: couldn't compact row heights ({e}) — rows saved fine.")
        return
    requests = []
    for ws in worksheets:
        n_rows = grid_rows.get(ws.id, ws.row_count)
        if n_rows <= 1:
            continue
        rows = {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": n_rows}
        requests.append({"repeatCell": {
            "range":  rows,
            "cell":   {"userEnteredFormat": {"wrapStrategy": "CLIP", "verticalAlignment": "TOP"}},
            "fields": "userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment",
        }})
        requests.append({"updateDimensionProperties": {
            "range":      {"sheetId": ws.id, "dimension": "ROWS",
                           "startIndex": 1, "endIndex": n_rows},
            "properties": {"pixelSize": ROW_HEIGHT_PX},
            "fields":     "pixelSize",
        }})
    if not requests:
        return
    try:
        _with_retry(sh.batch_update, {"requests": requests})
    except Exception as e:
        print(f"  [sheets] Warning: couldn't compact row heights ({e}) — rows saved fine.")

def save_visa_jobs(spreadsheet_id: str, jobs: list[dict],
                   rejected: list[dict] | None = None) -> tuple[int, int]:
    """Global visa feed. Sponsoring roles (YES/CONDITIONAL) go to Listings; the ones
    Claude read and rejected go to "No Sponsorship" with the quoted evidence, so the
    ~180 JDs a day that mention a visa word are inspectable instead of invisible.

    Rejects carry no JD text — the evidence quote is the point, and 180 rows a day
    of 45,000-char cells would bloat the sheet for nothing.

    Returns (listings written, rejects written)."""
    client = _get_client()
    sh     = _with_retry(client.open_by_key, spreadsheet_id)
    seen   = _load_seen_urls(sh, tabs=(TAB_LISTINGS, TAB_NO_SPONSOR))

    now       = _now_ist()
    month_str = now.strftime("%Y-%m")
    date_str  = now.strftime("%Y-%m-%d %H:%M")

    def build(job: dict, with_jd: bool) -> list | None:
        url = _clean_url(job.get("url", ""))
        if not url or url in seen:
            return None
        seen.add(url)
        head = [
            month_str, date_str,
            job.get("visa_verdict", ""), job.get("visa_evidence", ""),
            job.get("title", ""), job.get("company", ""),
            job.get("location", ""), job.get("posted", ""),
            job.get("source", ""), url,
        ]
        jd_cell = [_jd_for_row(job.get("jd", ""))] if with_jd else []
        return head + jd_cell + [job.get("sponsor_licence", "")]

    ws_listings = _ensure_tab(sh, TAB_LISTINGS, HEADERS_VISA)
    rows        = [r for r in (build(j, True) for j in jobs) if r]
    if rows:
        _with_retry(ws_listings.append_rows, rows, value_input_option="USER_ENTERED")

    sheets_touched = [ws_listings]
    no_rows        = []
    if rejected:
        ws_no   = _ensure_tab(sh, TAB_NO_SPONSOR, HEADERS_VISA_NO)
        no_rows = [r for r in (build(j, False) for j in rejected) if r]
        if no_rows:
            _with_retry(ws_no.append_rows, no_rows, value_input_option="USER_ENTERED")
        sheets_touched.append(ws_no)

    _compact_rows(sh, sheets_touched)

    print(f"  Sheets: +{len(rows)} listing(s), +{len(no_rows)} no-sponsorship row(s)")
    return len(rows), len(no_rows)

def save_eval_jobs(spreadsheet_id: str, jobs: list[dict]) -> tuple[int, int, int]:
    client = _get_client()
    sh     = _with_retry(client.open_by_key, spreadsheet_id)
    seen   = _load_seen_urls(sh)

    ws_apply = _ensure_tab(sh, TAB_APPLY, HEADERS_EVAL)
    ws_maybe = _ensure_tab(sh, TAB_MAYBE, HEADERS_EVAL)
    ws_skip  = _ensure_tab(sh, TAB_SKIP,  HEADERS_EVAL)

    now       = _now_ist()
    month_str = now.strftime("%Y-%m")
    date_str  = now.strftime("%Y-%m-%d %H:%M")

    rows_apply, rows_maybe, rows_skip = [], [], []

    for job in jobs:
        url = _clean_url(job.get("url", ""))
        if not url or url in seen:
            continue
        seen.add(url)
        ev  = job.get("evaluation", {})
        dec = ev.get("decision", "Skip")
        row = [
            month_str, date_str,
            job.get("title", ""), job.get("company", ""),
            job.get("location", ""), job.get("source", ""),
            dec, ev.get("reason", ""), ev.get("gap", ""), url,
            _jd_for_row(ev.get("jd", "")),
        ]
        if dec == "Apply":   rows_apply.append(row)
        elif dec == "Maybe": rows_maybe.append(row)
        else:                rows_skip.append(row)

    if rows_apply: _with_retry(ws_apply.append_rows, rows_apply, value_input_option="USER_ENTERED")
    if rows_maybe: _with_retry(ws_maybe.append_rows, rows_maybe, value_input_option="USER_ENTERED")
    if rows_skip:  _with_retry(ws_skip.append_rows,  rows_skip,  value_input_option="USER_ENTERED")

    _compact_rows(sh, [ws_apply, ws_maybe, ws_skip])

    print(f"  Sheets: +{len(rows_apply)} Apply  +{len(rows_maybe)} Maybe  +{len(rows_skip)} Skip")
    return len(rows_apply), len(rows_maybe), len(rows_skip)
