"""
sheets_writer.py — shared Google Sheets output for pm_eval and linkedin_global
==============================================================================
Writes evaluated jobs to a Google Sheet with 3 permanent tabs:
  Apply / Maybe / Skip

Each row includes a Month column (e.g. "2026-07") so you can filter
by month within each tab. No new tabs are created over time — just filter.

Setup (one-time):
  1. Google Cloud project → enable Google Sheets API
  2. Create a service account → download JSON key
  3. Share both Google Sheets with the service account email (Editor)
  4. Set env var GOOGLE_SERVICE_ACCOUNT_JSON = full contents of JSON key
     (paste entire JSON as the secret value in GitHub Actions)

See README for the full walkthrough.
"""

import os
import json
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Tab names — fixed, never change
TAB_APPLY = "Apply"
TAB_MAYBE = "Maybe"
TAB_SKIP  = "Skip"

# Column headers per tab
HEADERS_EVAL = [
    "Month", "Date Found", "Title", "Company", "Location",
    "Source", "Decision", "Reason", "Gap", "URL",
]

HEADERS_GLOBAL = [
    "Month", "Date Found", "Title", "Company", "Location",
    "Source", "Decision", "Reason", "Gap",
    "Relocation", "Visa Sponsorship", "URL",
]

# ─────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────
def _get_client() -> gspread.Client:
    """
    Supports two auth modes:
    - Env var GOOGLE_SERVICE_ACCOUNT_JSON (GitHub Actions / Render)
      Set this secret to the full contents of your service account JSON file.
    - Local JSON file path (local testing)
      Set env var GOOGLE_SERVICE_ACCOUNT_FILE to the path, or place
      service_account.json in the same folder as this script.
    """
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        info  = json.loads(sa_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        path  = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE",
                               os.path.join(os.path.dirname(__file__), "service_account.json"))
        creds = Credentials.from_service_account_file(path, scopes=SCOPES)
    return gspread.authorize(creds)

# ─────────────────────────────────────────────
# SHEET SETUP
# ─────────────────────────────────────────────
def _ensure_tab(sh: gspread.Spreadsheet, name: str, headers: list) -> gspread.Worksheet:
    """Get tab by name, creating it with headers if it doesn't exist yet."""
    try:
        return sh.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=5000, cols=len(headers))
        ws.append_row(headers, value_input_option="USER_ENTERED")
        try:
            ws.format(f"A1:{chr(64 + len(headers))}1",
                      {"textFormat": {"bold": True}})
        except Exception:
            pass
        return ws


def _load_seen_urls(sh: gspread.Spreadsheet) -> set:
    """Read all URLs already in the sheet (all 3 tabs) for dedup."""
    seen = set()
    for tab in (TAB_APPLY, TAB_MAYBE, TAB_SKIP):
        try:
            ws   = sh.worksheet(tab)
            vals = ws.col_values(1)   # temporarily read col A
            # URL is last column — get it properly
            all_vals = ws.get_all_values()
            if not all_vals:
                continue
            header = all_vals[0]
            try:
                url_idx = header.index("URL")
                for row in all_vals[1:]:
                    if len(row) > url_idx and row[url_idx]:
                        seen.add(row[url_idx])
            except ValueError:
                pass
        except gspread.WorksheetNotFound:
            pass
    return seen

# ─────────────────────────────────────────────
# MAIN WRITE FUNCTION — pm_eval
# ─────────────────────────────────────────────
def save_eval_jobs(spreadsheet_id: str, jobs: list) -> tuple[int, int, int]:
    """
    Write evaluated pm_eval jobs to Google Sheet.
    jobs: list of dicts with keys: title, company, location, source,
          url, evaluation={decision, reason, gap}

    Returns (n_apply, n_maybe, n_skip) counts of newly written rows.
    """
    client = _get_client()
    sh     = client.open_by_key(spreadsheet_id)
    seen   = _load_seen_urls(sh)

    ws_apply = _ensure_tab(sh, TAB_APPLY, HEADERS_EVAL)
    ws_maybe = _ensure_tab(sh, TAB_MAYBE, HEADERS_EVAL)
    ws_skip  = _ensure_tab(sh, TAB_SKIP,  HEADERS_EVAL)

    now       = datetime.now()
    month_str = now.strftime("%Y-%m")
    date_str  = now.strftime("%Y-%m-%d %H:%M")

    rows_apply, rows_maybe, rows_skip = [], [], []

    for job in jobs:
        url = job.get("url", "")
        if not url or url in seen:
            continue
        seen.add(url)

        ev  = job.get("evaluation", {})
        dec = ev.get("decision", "Skip")
        row = [
            month_str,
            date_str,
            job.get("title", ""),
            job.get("company", ""),
            job.get("location", ""),
            job.get("source", ""),
            dec,
            ev.get("reason", ""),
            ev.get("gap", ""),
            url,
        ]
        if dec == "Apply":
            rows_apply.append(row)
        elif dec == "Maybe":
            rows_maybe.append(row)
        else:
            rows_skip.append(row)

    if rows_apply: ws_apply.append_rows(rows_apply, value_input_option="USER_ENTERED")
    if rows_maybe: ws_maybe.append_rows(rows_maybe, value_input_option="USER_ENTERED")
    if rows_skip:  ws_skip.append_rows(rows_skip,  value_input_option="USER_ENTERED")

    print(f"  📊 Sheets: +{len(rows_apply)} Apply  +{len(rows_maybe)} Maybe  +{len(rows_skip)} Skip")
    return len(rows_apply), len(rows_maybe), len(rows_skip)


# ─────────────────────────────────────────────
# MAIN WRITE FUNCTION — linkedin_global
# ─────────────────────────────────────────────
def save_global_jobs(spreadsheet_id: str, jobs: list) -> tuple[int, int, int]:
    """
    Write evaluated linkedin_global jobs to Google Sheet.
    jobs: list of dicts with keys: title, company, location, source,
          url, relocation_confirmed, visa_confirmed,
          evaluation={decision, reason, gap}

    Returns (n_apply, n_maybe, n_skip) counts of newly written rows.
    """
    client = _get_client()
    sh     = client.open_by_key(spreadsheet_id)
    seen   = _load_seen_urls(sh)

    ws_apply = _ensure_tab(sh, TAB_APPLY, HEADERS_GLOBAL)
    ws_maybe = _ensure_tab(sh, TAB_MAYBE, HEADERS_GLOBAL)
    ws_skip  = _ensure_tab(sh, TAB_SKIP,  HEADERS_GLOBAL)

    now       = datetime.now()
    month_str = now.strftime("%Y-%m")
    date_str  = now.strftime("%Y-%m-%d %H:%M")

    rows_apply, rows_maybe, rows_skip = [], [], []

    for job in jobs:
        url = job.get("url", "")
        if not url or url in seen:
            continue
        seen.add(url)

        ev  = job.get("evaluation", {})
        dec = ev.get("decision", "Skip")
        row = [
            month_str,
            date_str,
            job.get("title", ""),
            job.get("company", ""),
            job.get("location", ""),
            job.get("source", ""),
            dec,
            ev.get("reason", ""),
            ev.get("gap", ""),
            "✅" if job.get("relocation_confirmed") else "—",
            "✅" if job.get("visa_confirmed")       else "—",
            url,
        ]
        if dec == "Apply":
            rows_apply.append(row)
        elif dec == "Maybe":
            rows_maybe.append(row)
        else:
            rows_skip.append(row)

    if rows_apply: ws_apply.append_rows(rows_apply, value_input_option="USER_ENTERED")
    if rows_maybe: ws_maybe.append_rows(rows_maybe, value_input_option="USER_ENTERED")
    if rows_skip:  ws_skip.append_rows(rows_skip,  value_input_option="USER_ENTERED")

    print(f"  📊 Sheets: +{len(rows_apply)} Apply  +{len(rows_maybe)} Maybe  +{len(rows_skip)} Skip")
    return len(rows_apply), len(rows_maybe), len(rows_skip)
