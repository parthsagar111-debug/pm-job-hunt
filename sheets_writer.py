"""
sheets_writer.py — shared Google Sheets output for pm_eval and linkedin_global
"""

import os
import json
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
    try:
        return sh.worksheet(name)
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
    seen = set()
    for tab in (TAB_APPLY, TAB_MAYBE, TAB_SKIP):
        try:
            ws = sh.worksheet(tab)
            # Read full first row to find URL column exactly
            header = ws.row_values(1)
            if not header:
                continue
            # Find URL column (1-indexed for gspread)
            url_col = None
            for i, h in enumerate(header):
                if h.strip() == "URL":
                    url_col = i + 1
                    break
            if url_col is None:
                print(f"  [sheets] Warning: URL column not found in {tab}, headers={header}")
                continue
            urls = ws.col_values(url_col)[1:]  # skip header row
            seen.update(_clean_url(u) for u in urls if u.strip())
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
