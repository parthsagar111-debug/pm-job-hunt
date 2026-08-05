"""
pm_posts_hosted.py — PM Hiring Posts Feed (hosted / GitHub Actions version)
============================================================================
Runs ONCE per invocation and exits. Triggered externally (cron-job.org),
same pattern as cos_eir_hosted.py.

Scrapes LinkedIn organic feed posts (not the Jobs section) for PM hiring
announcements via Apify — no cookies, no LinkedIn account risk. No AI
evaluation: this is a raw deduped feed for manual review.

Required environment variables (set as GitHub Actions secrets):
  APIFY_API_TOKEN             — from https://console.apify.com/settings/integrations
  GOOGLE_SERVICE_ACCOUNT_JSON — full JSON contents of service account key
  PM_POSTS_SPREADSHEET_ID     — Google Sheet ID for this script's output
                                 (separate sheet from PM_EVAL / COS_EIR / GLOBAL)
  NTFY_TOPIC                  — your ntfy topic name
"""

import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core_pm_posts_hosted import fetch_pm_posts, SEARCH_QUERIES
from sheets_writer import save_raw_jobs, load_seen_urls, TAB_LISTINGS
from ntfy_notify import listing_summary

APIFY_API_TOKEN = os.environ.get("APIFY_API_TOKEN", "")
SPREADSHEET_ID  = os.environ.get("PM_POSTS_SPREADSHEET_ID", "")


def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*55}")
    print(f"  PM HIRING POSTS FEED (HOSTED) — 24h catch-up")
    print(f"  [{now}]")
    print(f"  Source: LinkedIn feed posts, via Apify (no cookies)")
    print(f"  Queries: {SEARCH_QUERIES}")
    print(f"  (no AI evaluation — raw listing feed)")
    print(f"{'='*55}")

    if not APIFY_API_TOKEN:
        print("  ERROR: APIFY_API_TOKEN env var not set. Exiting.")
        sys.exit(1)
    if not SPREADSHEET_ID:
        print("  ERROR: PM_POSTS_SPREADSHEET_ID env var not set. Exiting.")
        sys.exit(1)

    try:
        seen = load_seen_urls(SPREADSHEET_ID, tabs=(TAB_LISTINGS,))
        print(f"  Dedup: {len(seen)} known URL(s) loaded from Sheet")
    except Exception as e:
        print(f"  ERROR: could not load dedup state from Sheet ({e}).")
        print("  Aborting run rather than write duplicate rows blind.")
        sys.exit(1)

    try:
        posts = fetch_pm_posts(APIFY_API_TOKEN, posted_limit="24h")
    except Exception as e:
        print(f"  ERROR: Apify run failed ({e}).")
        sys.exit(1)

    new_posts = [p for p in posts if p.get("url", "").split("?")[0] not in seen]
    print(f"\n{'='*55}")
    print(f"  Total: {len(new_posts)} new post(s) this run (of {len(posts)} fetched)")
    print(f"{'='*55}\n")

    if not new_posts:
        print("  Nothing new this run.")
        listing_summary("PM Hiring Posts", 0)
        return

    n_new = save_raw_jobs(SPREADSHEET_ID, new_posts)
    listing_summary("PM Hiring Posts", n_new)

    print(f"\n{'='*55}")
    print(f"  Done. {n_new} new post(s) written to Sheet.")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
