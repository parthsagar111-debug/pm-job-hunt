"""
pm_eval_hosted.py — PM Job Alert (hosted / GitHub Actions version)
==================================================================
Runs ONCE in 24-hour catch-up mode and exits.
Scheduled externally: cron-job.org calls workflow_dispatch every 30 minutes.

Sources:  LinkedIn · Naukri · Hirist · IIMJobs
Output:   Google Sheet (Apply / Maybe / Skip tabs)
Notify:   ntfy push notification after run

Required environment variables (set as GitHub Actions secrets):
  ANTHROPIC_API_KEY             — Claude API key
  GOOGLE_SERVICE_ACCOUNT_JSON   — full JSON contents of service account key
  PM_EVAL_SPREADSHEET_ID        — Google Sheet ID for this script's output
  NTFY_TOPIC                    — your ntfy topic name
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import core_eval_hosted as core
from core_eval_hosted import (
    SOURCES, SOURCE_ICONS,
    sort_newest_first,
    within_24hrs, evaluate_batch,
    SEARCH_KEYWORD, JobCollector, LI_LIMITER,
)
from sheets_writer import save_eval_jobs, load_seen_urls
from ntfy_notify import run_summary
from datetime import datetime

SPREADSHEET_ID = os.environ.get("PM_EVAL_SPREADSHEET_ID", "")

# ─────────────────────────────────────────────
# MAIN — single 24h run, then exit
# ─────────────────────────────────────────────
def main() -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*55}")
    print(f"  PM EVAL (HOSTED) — 24h catch-up")
    print(f"  [{now}]")
    print(f"  Sources: LinkedIn · Naukri · Hirist · IIMJobs")
    print(f"{'='*55}")

    if not SPREADSHEET_ID:
        print("  ERROR: PM_EVAL_SPREADSHEET_ID env var not set. Exiting.")
        sys.exit(1)

    # Dedup MUST happen before evaluation — evaluating already-seen jobs burns
    # Claude API tokens for nothing. Load the real seen-set from the Sheet now,
    # not just at write time.
    try:
        seen = load_seen_urls(SPREADSHEET_ID)
        print(f"  Dedup: {len(seen)} known URL(s) loaded from Sheet")
    except Exception as e:
        print(f"  ERROR: could not load dedup state from Sheet ({e}).")
        print("  Aborting run rather than risk re-evaluating everything at full API cost.")
        sys.exit(1)

    # Same collector the Gulf and global feeds use, so the per-reason counters show
    # exactly why jobs dropped out — otherwise a quiet run is indistinguishable from
    # an over-aggressive filter.
    collector = JobCollector(seen_urls=seen, max_per_company=2)

    for name, fetch_fn in SOURCES:
        icon = SOURCE_ICONS.get(name, "🔔")
        print(f"\n{icon} [{name}]")
        try:
            jobs = fetch_fn(SEARCH_KEYWORD, time_range="24h")
            if name != "LinkedIn":
                jobs = [j for j in jobs if within_24hrs(j)]
            before = collector.counts["kept"]
            for job in sort_newest_first(jobs):
                collector.add(job, label=name)
            print(f"  {collector.counts['kept'] - before} new job(s)")
        except Exception as e:
            print(f"  ERROR: {e}")
        time.sleep(2)

    all_jobs = collector.jobs
    print(f"\n{'='*55}")
    print(f"  Collected: {collector.summary()}")
    print(f"  Total: {len(all_jobs)} jobs  |  Evaluating with Claude AI...")
    print(f"{'='*55}\n")

    if not all_jobs:
        print("  Nothing new this run.")
        print(f"  Rate: {LI_LIMITER.summary()}")
        run_summary("PM Eval", 0, 0, 0)
        return

    evaluated_jobs, aborted = evaluate_batch(all_jobs)

    if not evaluated_jobs:
        print("\n  No jobs were successfully evaluated this run (API failures only).")
        run_summary("PM Eval — FAILED", 0, 0, 0)
        sys.exit(1)

    # Save to Google Sheets — dedup happens inside save_eval_jobs too (belt & suspenders)
    n_apply, n_maybe, n_skip = save_eval_jobs(SPREADSHEET_ID, evaluated_jobs)

    # ntfy push notification
    label = "PM Eval — partial (API errors)" if aborted else "PM Eval"
    run_summary(label, n_apply, n_maybe, n_skip)

    print(f"\n{'='*55}")
    print(f"  Done. Apply: {n_apply}  Maybe: {n_maybe}  Skip: {n_skip}")
    print(f"  Rate: {LI_LIMITER.summary()}")
    if aborted:
        print("  NOTE: run was aborted early due to repeated API errors — some jobs untouched, will retry next run.")
    print(f"{'='*55}\n")


    if aborted:
        sys.exit(1)  # surface as a failed run in GitHub Actions even though partial results were saved

if __name__ == "__main__":
    main()
