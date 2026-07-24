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
    sort_newest_first,
    within_24hrs, evaluate_job,
    SEARCH_KEYWORD,
)
from sheets_writer import save_eval_jobs, load_seen_urls
from ntfy_notify import run_summary
from playwright_browser import close_browser
from datetime import datetime

SPREADSHEET_ID = os.environ.get("PM_EVAL_SPREADSHEET_ID", "")

# ─────────────────────────────────────────────
# EVALUATE A BATCH
# ─────────────────────────────────────────────
CONSECUTIVE_ERROR_LIMIT = 3  # abort early if the API is clearly down (bad key, no funds, outage)

def evaluate_batch(jobs: list) -> tuple[list, bool]:
    """
    Returns (evaluated_jobs, aborted).
    evaluated_jobs only contains jobs that got a real decision — jobs whose API
    call failed (decision == "Error") are left out so they aren't written to the
    Sheet and aren't marked as seen; they'll simply be re-fetched and retried next
    run. If several calls in a row fail, we stop early instead of burning through
    the whole batch against a dead key/empty balance.
    """
    total = len(jobs)
    ok_jobs = []
    consecutive_errors = 0
    for i, job in enumerate(jobs, 1):
        title   = job.get("title", "")
        company = job.get("company", "")
        icon    = SOURCE_ICONS.get(job.get("source", ""), "🔔")
        print(f"  [{i}/{total}] {icon} 📄 {title} @ {company}...", end=" ", flush=True)
        ev    = evaluate_job(job)
        job["evaluation"] = ev
        badge = {"Apply": "✅", "Maybe": "🤔", "Skip": "❌", "Error": "⚠️"}.get(ev["decision"], "—")
        print(f"{badge} {ev['decision']}  |  {ev['reason']}")

        if ev["decision"] == "Error":
            consecutive_errors += 1
            if consecutive_errors >= CONSECUTIVE_ERROR_LIMIT:
                print(f"\n  ⚠️  {consecutive_errors} consecutive evaluation failures — "
                      f"aborting batch early ({total - i} job(s) not attempted). "
                      f"They'll be retried on the next run.")
                return ok_jobs, True
        else:
            consecutive_errors = 0
            ok_jobs.append(job)

        time.sleep(0.5)
    return ok_jobs, False

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
    print(f"  Total: {len(all_jobs)} jobs  |  Evaluating with Claude AI...")
    print(f"{'='*55}\n")

    if not all_jobs:
        print("  Nothing new this run.")
        run_summary("PM Eval", 0, 0, 0)
        return

    evaluated_jobs, aborted = evaluate_batch(all_jobs)

    if not evaluated_jobs:
        print("\n  No jobs were successfully evaluated this run (API failures only).")
        run_summary("PM Eval — FAILED", 0, 0, 0)
        try:
            close_browser()
        except Exception:
            pass
        sys.exit(1)

    # Save to Google Sheets — dedup happens inside save_eval_jobs too (belt & suspenders)
    n_apply, n_maybe, n_skip = save_eval_jobs(SPREADSHEET_ID, evaluated_jobs)

    # ntfy push notification
    label = "PM Eval — partial (API errors)" if aborted else "PM Eval"
    run_summary(label, n_apply, n_maybe, n_skip)

    print(f"\n{'='*55}")
    print(f"  Done. Apply: {n_apply}  Maybe: {n_maybe}  Skip: {n_skip}")
    if aborted:
        print("  NOTE: run was aborted early due to repeated API errors — some jobs untouched, will retry next run.")
    print(f"{'='*55}\n")

    try:
        close_browser()
    except Exception:
        pass

    if aborted:
        sys.exit(1)  # surface as a failed run in GitHub Actions even though partial results were saved

if __name__ == "__main__":
    main()
