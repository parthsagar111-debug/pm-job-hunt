"""
cos_eir_hosted.py — Chief of Staff / EIR Job Feed (hosted / GitHub Actions version)
=====================================================================================
Runs ONCE in 24-hour catch-up mode and exits. Triggered externally (cron-job.org),
same pattern as pm_eval_hosted.py.

No AI evaluation: Apply/Maybe/Skip judgment against a candidate profile doesn't map
well onto Chief of Staff / EIR roles, so this just scrapes, dedupes, and writes a
plain feed of matching listings to a single "Listings" tab for manual review.

Sources:  LinkedIn · Naukri · Hirist · IIMJobs
Output:   Google Sheet, "Listings" tab — dedicated COS_EIR_SPREADSHEET_ID sheet
Notify:   ntfy push notification after run

Required environment variables (set as GitHub Actions secrets):
  GOOGLE_SERVICE_ACCOUNT_JSON   — full JSON contents of service account key
  COS_EIR_SPREADSHEET_ID        — Google Sheet ID for this script's output (separate
                                   sheet from PM_EVAL_SPREADSHEET_ID / GLOBAL_SPREADSHEET_ID)
  NTFY_TOPIC                    — your ntfy topic name

  (No ANTHROPIC_API_KEY needed — there's no evaluation step, so no API cost.)

Dedup still happens BEFORE writing (load_seen_urls from the "Listings" tab up front),
not just at write time — this avoids re-scraping-then-discarding the same listings
and cluttering the sheet with duplicate rows every run.
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core_cos_eir_hosted import (
    SOURCES, SOURCE_ICONS,
    sort_newest_first,
    within_24hrs,
    SEARCH_KEYWORD,
)
from sheets_writer import save_raw_jobs, load_seen_urls, TAB_LISTINGS
from ntfy_notify import listing_summary
from datetime import datetime

SPREADSHEET_ID = os.environ.get("COS_EIR_SPREADSHEET_ID", "")

# ─────────────────────────────────────────────
# MAIN — single 24h run, then exit
# ─────────────────────────────────────────────
def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*55}")
    print(f"  CHIEF OF STAFF / EIR FEED (HOSTED) — 24h catch-up")
    print(f"  [{now}]")
    print(f"  Sources: LinkedIn · Naukri · Hirist · IIMJobs")
    print(f"  (no AI evaluation — raw listing feed)")
    print(f"{'='*55}")

    if not SPREADSHEET_ID:
        print("  ERROR: COS_EIR_SPREADSHEET_ID env var not set. Exiting.")
        sys.exit(1)

    # Dedup against the Listings tab specifically (this sheet has no Apply/Maybe/Skip
    # tabs — everything lives in one plain tab).
    try:
        seen = load_seen_urls(SPREADSHEET_ID, tabs=(TAB_LISTINGS,))
        print(f"  Dedup: {len(seen)} known URL(s) loaded from Sheet")
    except Exception as e:
        print(f"  ERROR: could not load dedup state from Sheet ({e}).")
        print("  Aborting run rather than write duplicate rows blind.")
        sys.exit(1)

    all_jobs       = []
    company_counts = {}

    for name, fetch_fn in SOURCES:
        icon = SOURCE_ICONS.get(name, "🔔")
        print(f"\n{icon} [{name}]")
        try:
            jobs = fetch_fn(SEARCH_KEYWORD, time_range="24h")
            if name != "LinkedIn":
                jobs = [j for j in jobs if within_24hrs(j)]
            jobs = sort_newest_first(jobs)
            filtered = []
            for job in jobs:
                if job.get("url", "").split("?")[0] in seen:
                    continue
                co = job.get("company", "").lower().strip()
                if company_counts.get(co, 0) >= 2:
                    continue
                company_counts[co] = company_counts.get(co, 0) + 1
                filtered.append(job)
            print(f"  {len(filtered)} new job(s)")
            all_jobs.extend(filtered)
        except Exception as e:
            print(f"  ERROR: {e}")
        time.sleep(2)

    print(f"\n{'='*55}")
    print(f"  Total: {len(all_jobs)} new job(s) this run")
    print(f"{'='*55}\n")

    if not all_jobs:
        print("  Nothing new this run.")
        listing_summary("Chief of Staff / EIR", 0)
        return

    n_new = save_raw_jobs(SPREADSHEET_ID, all_jobs)
    listing_summary("Chief of Staff / EIR", n_new)

    print(f"\n{'='*55}")
    print(f"  Done. {n_new} new listing(s) written to Sheet.")
    print(f"{'='*55}\n")

if __name__ == "__main__":
    main()
