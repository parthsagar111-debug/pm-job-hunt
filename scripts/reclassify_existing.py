"""
reclassify_existing.py — what would the new classifier do with the rows already in
the Sheet?

    python scripts/reclassify_existing.py               # dry run (default): CSV only
    python scripts/reclassify_existing.py --apply       # actually move the rows

Dry run reads every row on Apply/Maybe/Skip, re-evaluates it against its STORED JD
(no scraping), and writes a local CSV of the rows whose tab would change. It writes
NOTHING to the Sheet.

--apply moves those rows, preserving every column — including Resume Match Score and
Resume Decision, which Agent 1 wrote and nothing else may clobber. A row is deleted
from its old tab only after the new tab reports the append succeeded.

Calls the Claude API once per row, so a full pass over a few hundred rows is not free.
Review the CSV first.
"""

import argparse
import csv
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from candidate_profile import load_candidate_profile
from core_eval_hosted import extract_features
from decision_rules import decide
from sheets_writer import (
    TAB_APPLY, TAB_MAYBE, TAB_SKIP, _ensure_tab, _get_client, _with_retry, HEADERS_EVAL,
)

SPREADSHEET_ID = os.environ.get("PM_EVAL_SPREADSHEET_ID", "")
TABS = (TAB_APPLY, TAB_MAYBE, TAB_SKIP)


def _load(sh) -> tuple[list[dict], dict]:
    """All rows across the three tabs, plus each tab's own header row."""
    rows, headers_by_tab = [], {}
    for tab in TABS:
        ws = _ensure_tab(sh, tab, HEADERS_EVAL)
        values = _with_retry(ws.get_all_values)
        if not values:
            continue
        headers = [h.strip() for h in values[0]]
        headers_by_tab[tab] = headers
        for index, raw in enumerate(values[1:], start=2):
            padded = raw + [""] * (len(headers) - len(raw))
            rows.append({"_tab": tab, "_row_number": index, "_cells": padded,
                         **dict(zip(headers, padded))})
    return rows, headers_by_tab


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="move rows between tabs (default: dry run, CSV only)")
    parser.add_argument("--csv", default="reclassify_preview.csv")
    args = parser.parse_args()

    if not SPREADSHEET_ID:
        sys.exit("PM_EVAL_SPREADSHEET_ID is not set.")
    load_candidate_profile()

    client = _get_client()
    sh = _with_retry(client.open_by_key, SPREADSHEET_ID)
    rows, headers_by_tab = _load(sh)
    print(f"Loaded {len(rows)} row(s) across {', '.join(TABS)}.")

    changes, errors = [], 0
    for i, row in enumerate(rows, 1):
        job = {"title": row.get("Title", ""), "company": row.get("Company", ""),
               "location": row.get("Location", ""), "source": row.get("Source", ""),
               "url": row.get("URL", ""), "stated_experience": ""}
        try:
            features, _jd = extract_features(job, jd_text=row.get("JD", ""))
            decision, prefix = decide(features)
        except Exception as e:
            errors += 1
            print(f"  [{i}/{len(rows)}] ERROR {row.get('Company','')}: {type(e).__name__}: {e}")
            continue

        if decision != row["_tab"]:
            changes.append({
                "url": row.get("URL", ""), "title": row.get("Title", ""),
                "company": row.get("Company", ""), "old": row["_tab"], "new": decision,
                "reason": (prefix + (features.get("reason") or "")).strip(),
                "_row": row,
            })
        if i % 25 == 0:
            print(f"  … {i}/{len(rows)} evaluated, {len(changes)} would move")

    with open(args.csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["url", "title", "company", "old", "new", "reason"])
        writer.writeheader()
        for change in changes:
            writer.writerow({k: change[k] for k in writer.fieldnames})

    print(f"\n{len(changes)} row(s) would change tab; {errors} error(s). CSV: {args.csv}")
    for change in changes[:15]:
        print(f"  {change['old']:<5} -> {change['new']:<5} {change['company'][:24]:<24} "
              f"{change['title'][:44]}")
    if len(changes) > 15:
        print(f"  … and {len(changes) - 15} more in the CSV")

    if not args.apply:
        print("\nDry run — nothing was written. Re-run with --apply after reviewing the CSV.")
        return

    print(f"\nApplying {len(changes)} move(s)...")
    moved = 0
    # Deleting shifts row numbers, so delete bottom-up within each tab.
    for change in sorted(changes, key=lambda c: (c["_row"]["_tab"], -c["_row"]["_row_number"])):
        row = change["_row"]
        source_ws = sh.worksheet(row["_tab"])
        target_ws = _ensure_tab(sh, change["new"], HEADERS_EVAL)

        source_headers = headers_by_tab.get(row["_tab"], HEADERS_EVAL)
        target_headers = _with_retry(target_ws.row_values, 1)
        # Re-map by header name: the tabs can carry different trailing columns
        # (Resume Match Score / Resume Decision), and those values must survive.
        by_name = dict(zip(source_headers, row["_cells"]))
        payload = [by_name.get(h, "") for h in target_headers]

        try:
            _with_retry(target_ws.append_row, payload, value_input_option="USER_ENTERED")
        except Exception as e:
            print(f"  append failed for {change['company']}: {e} — leaving the original row alone")
            continue
        try:
            _with_retry(source_ws.delete_rows, row["_row_number"])
            moved += 1
        except Exception as e:
            print(f"  WARNING: appended to {change['new']} but could not delete the old "
                  f"{row['_tab']} row {row['_row_number']} for {change['company']}: {e} — "
                  f"delete it by hand to avoid a duplicate")

    print(f"Moved {moved}/{len(changes)} row(s) at {datetime.now():%Y-%m-%d %H:%M}.")


if __name__ == "__main__":
    main()
