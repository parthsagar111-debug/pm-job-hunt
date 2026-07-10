"""
sheets_writer.py — shared Google Sheets output for pm_eval and linkedin_global
"""

import os
import json
import re
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

def _now_ist():
    return datetime.now(IST)

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

TAB_APPLY = "Apply"
TAB_MAYBE = "Maybe"
TAB_SKIP  = "Skip"

HEADERS_EVAL = [
    "Month", "Date Found", "Title", "Company", "Location",
    "Source", "Decision", "Reason", "Gap", "URL",
]

HEADERS_GLOBAL = [
    "Month", "Date Found", "Title", "Company", "Location",
    "Source", "Decision", "Reason", "Gap",
    "Relocation", "Visa Sponsorship", "URL",
]

_URL_PATTERN = re.compile(
    r'https?://(www\.)?(linkedin\.com/jobs/view/|naukri\.com/job-listings-|iimjobs\.com/j/|hirist\.tech/j/)\S+'
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
    """Get or create tab. Only writes headers if tab is new or row 1 is completely empty."""
    try:
        ws = sh.worksheet(name)
        # Only fix headers if row 1 is empty — never overwrite on existing data tabs
        first_cell = ws.cell(1, 1).value or ""
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

def _load_seen_urls(sh: gspread.Spreadsheet) -> set:
    """Scan every cell in all 3 tabs for job URLs — immune to column misalignment."""
    seen = set()
    for tab in (TAB_APPLY, TAB_MAYBE, TAB_SKIP):
        try:
            ws = sh.worksheet(tab)
            all_values = ws.get_all_values()
            for row in all_values[1:]:  # skip header row
                for cell in row:
                    if cell and _URL_PATTERN.match(cell.strip()):
                        seen.add(_clean_url(cell.strip()))
        except gspread.WorksheetNotFound:
            pass
        except Exception as e:
            print(f"  [sheets] Warning: dedup read failed for {tab}: {e}")
    print(f"  [sheets] Dedup: {len(seen)} existing URLs loaded")
    return seen

def save_eval_jobs(spreadsheet_id: str, jobs: list) -> tuple[int, int, int]:
    client = _get_client()
    sh     = client.open_by_key(spreadsheet_id)
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
        ]
        if dec == "Apply":   rows_apply.append(row)
        elif dec == "Maybe": rows_maybe.append(row)
        else:                rows_skip.append(row)

    if rows_apply: ws_apply.append_rows(rows_apply, value_input_option="USER_ENTERED")
    if rows_maybe: ws_maybe.append_rows(rows_maybe, value_input_option="USER_ENTERED")
    if rows_skip:  ws_skip.append_rows(rows_skip,   value_input_option="USER_ENTERED")

    print(f"  Sheets: +{len(rows_apply)} Apply  +{len(rows_maybe)} Maybe  +{len(rows_skip)} Skip")
    return len(rows_apply), len(rows_maybe), len(rows_skip)


def save_global_jobs(spreadsheet_id: str, jobs: list) -> tuple[int, int, int]:
    client = _get_client()
    sh     = client.open_by_key(spreadsheet_id)
    seen   = _load_seen_urls(sh)

    ws_apply = _ensure_tab(sh, TAB_APPLY, HEADERS_GLOBAL)
    ws_maybe = _ensure_tab(sh, TAB_MAYBE, HEADERS_GLOBAL)
    ws_skip  = _ensure_tab(sh, TAB_SKIP,  HEADERS_GLOBAL)

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
            dec, ev.get("reason", ""), ev.get("gap", ""),
            "Yes" if job.get("relocation_confirmed") else "No",
            "Yes" if job.get("visa_confirmed") else "No",
            url,
        ]
        if dec == "Apply":   rows_apply.append(row)
        elif dec == "Maybe": rows_maybe.append(row)
        else:                rows_skip.append(row)

    if rows_apply: ws_apply.append_rows(rows_apply, value_input_option="USER_ENTERED")
    if rows_maybe: ws_maybe.append_rows(rows_maybe, value_input_option="USER_ENTERED")
    if rows_skip:  ws_skip.append_rows(rows_skip,   value_input_option="USER_ENTERED")

    print(f"  Sheets: +{len(rows_apply)} Apply  +{len(rows_maybe)} Maybe  +{len(rows_skip)} Skip")
    return len(rows_apply), len(rows_maybe), len(rows_skip)
