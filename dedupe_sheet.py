#!/usr/bin/env python3
"""One-off repair: remove duplicate-URL rows from the PM Eval tabs.

Why this exists
---------------
Until 2026-11-05, sheets_writer._URL_PATTERN gated the dedup read on an allowlist
of scraped hosts. Wellfound was added as a source without being added there, so
every Wellfound URL already in the Sheet was discarded on the read. The source was
invisible to dedup in both places it matters — the pre-evaluation filter and the
write-time filter — so each run re-classified the same 30-day backlog at full API
price and re-appended it.

Two runs landed before the fix (08:35 and 09:45 UTC). All 27 of the second run's
Wellfound rows were duplicates of the first's; none were new. The pattern fix stops
new duplicates, but cannot retract the rows already written. Hence this.

It is not Wellfound-specific: it removes any row whose URL already appeared higher
up the same tab, so it also cleans anything the old allowlist let through earlier.

Safety
------
Dry-run by default. It prints exactly what it would delete and exits without
touching the Sheet unless APPLY=true. Deletions go bottom-up so earlier row
indices stay valid, and the FIRST occurrence of each URL is always kept — the
oldest row, with the original "Date Found".
"""

import os
import sys
from collections import defaultdict

from sheets_writer import (
    TAB_APPLY, TAB_MAYBE, TAB_SKIP, _clean_url, _get_client, _with_retry,
)

TABS = (TAB_APPLY, TAB_MAYBE, TAB_SKIP)
APPLY = os.environ.get("APPLY", "").strip().lower() == "true"


def duplicate_rows(ws) -> tuple[list[tuple[int, str, str]], int]:
    """Row indices to delete on this tab, as (row_number, title, url).

    Reads only the columns needed — URL to find duplicates, Title/Company to make
    the report legible. A full get_all_values() would pull every 45,000-char JD
    cell down with it.
    """
    headers = [h.strip() for h in (_with_retry(ws.row_values, 1) or [])]
    if "URL" not in headers:
        print(f"  {ws.title}: no URL column — skipped")
        return [], 0

    def col(name):
        return (_with_retry(ws.col_values, headers.index(name) + 1)[1:]
                if name in headers else [])

    urls, titles, companies = col("URL"), col("Title"), col("Company")

    first_seen, dupes = {}, []
    for i, raw in enumerate(urls):
        url = _clean_url(raw)
        if not url:
            continue
        row_no = i + 2                      # 1-indexed, and row 1 is the header
        if url in first_seen:
            label = (titles[i] if i < len(titles) else "") or "(untitled)"
            where = (companies[i] if i < len(companies) else "") or "?"
            dupes.append((row_no, f"{label} @ {where}", url))
        else:
            first_seen[url] = row_no
    return dupes, len(first_seen)


def main() -> None:
    sid = os.environ.get("PM_EVAL_SPREADSHEET_ID")
    if not sid:
        sys.exit("PM_EVAL_SPREADSHEET_ID is not set")

    sh = _with_retry(_get_client().open_by_key, sid)
    print("Dedupe PM Eval tabs —", "APPLYING CHANGES" if APPLY else "DRY RUN (nothing will be deleted)")
    print("=" * 70)

    total, by_source = 0, defaultdict(int)
    for tab in TABS:
        try:
            ws = sh.worksheet(tab)
        except Exception as e:
            print(f"  {tab}: could not open ({e})")
            continue

        dupes, uniques = duplicate_rows(ws)
        print(f"\n  {tab}: {uniques} unique URL(s), {len(dupes)} duplicate row(s)")
        for row_no, label, url in dupes:
            print(f"    row {row_no:>5}  {label[:62]:<62}  {url}")
            by_source[url.split("/")[2]] += 1
        total += len(dupes)

        if dupes and APPLY:
            # Bottom-up: deleting row 40 first would shift every row above it.
            for row_no, _, _ in sorted(dupes, reverse=True):
                _with_retry(ws.delete_rows, row_no)
            print(f"    -> deleted {len(dupes)} row(s) from {tab}")

    print("\n" + "=" * 70)
    print(f"  {total} duplicate row(s) across {len(TABS)} tab(s)")
    for host, n in sorted(by_source.items(), key=lambda kv: -kv[1]):
        print(f"    {host:<28} {n}")
    if total and not APPLY:
        print("\n  Nothing was changed. Re-run with APPLY=true to delete these rows.")


if __name__ == "__main__":
    main()
