"""
pm_eval_hosted.py — PM Job Alert (hosted / GitHub Actions version)
==================================================================
Runs ONCE in 24-hour catch-up mode and exits.
GitHub Actions handles scheduling (every 2 hours via cron).

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
    load_seen_jobs, sort_newest_first,
    within_24hrs, evaluate_job,
    SEARCH_KEYWORD,
)
from sheets_writer import save_eval_jobs
from ntfy_notify import run_summary
from playwright_browser import close_browser
from datetime import datetime

SPREADSHEET_ID = os.environ.get("PM_EVAL_SPREADSHEET_ID", "")

# ─────────────────────────────────────────────
# EVALUATE A BATCH
# ─────────────────────────────────────────────
def evaluate_batch(jobs: list) -> list:
    total = len(jobs)
    for i, job in enumerate(jobs, 1):
        title   = job.get("title", "")
        company = job.get("company", "")
        icon    = SOURCE_ICONS.get(job.get("source", ""), "🔔")
        print(f"  [{i}/{total}] {icon} 📄 {title} @ {company}...", end=" ", flush=True)
        ev    = evaluate_job(job)
        job["evaluation"] = ev
        badge = {"Apply": "✅", "Maybe": "🤔", "Skip": "❌"}.get(ev["decision"], "—")
        print(f"{badge} {ev['decision']}  |  {ev['reason']}")
        time.sleep(0.5)
    return jobs

# ─────────────────────────────────────────────
# MAIN — single 24h run, then exit
# ─────────────────────────────────────────────
def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*55}")
    print(f"  PM EVAL (HOSTED) — 24h catch-up")
    print(f"  [{now}]")
    print(f"  Sources: LinkedIn · Naukri · Hirist · IIMJobs")
    print(f"{'='*55}")

    if not SPREADSHEET_ID:
        print("  ERROR: PM_EVAL_SPREADSHEET_ID env var not set. Exiting.")
        sys.exit(1)

    all_jobs       = []
    seen           = load_seen_jobs()   # returns empty set on hosted — dedup at write time
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
    print(f"  Total: {len(all_jobs)} jobs  |  Evaluating with Claude AI...")
    print(f"{'='*55}\n")

    if not all_jobs:
        print("  Nothing new this run.")
        run_summary("PM Eval", 0, 0, 0)
        return

    all_jobs = evaluate_batch(all_jobs)

    # Save to Google Sheets — dedup happens inside save_eval_jobs
    n_apply, n_maybe, n_skip = save_eval_jobs(SPREADSHEET_ID, all_jobs)

    # ntfy push notification
    run_summary("PM Eval", n_apply, n_maybe, n_skip)

    print(f"\n{'='*55}")
    print(f"  Done. Apply: {n_apply}  Maybe: {n_maybe}  Skip: {n_skip}")
    print(f"{'='*55}\n")

    try:
        close_browser()
    except Exception:
        pass

if __name__ == "__main__":
    main()
