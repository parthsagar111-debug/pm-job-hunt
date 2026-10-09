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
    within_24hrs, within_week, evaluate_batch, print_token_report,
    SEARCH_KEYWORD, JobCollector, LI_LIMITER, is_pm_eval_role,
)
from candidate_profile import load_candidate_profile
from decision_rules import decide
from sheets_writer import save_eval_jobs, load_seen_urls
from ntfy_notify import run_summary
from datetime import datetime

SPREADSHEET_ID = os.environ.get("PM_EVAL_SPREADSHEET_ID", "")

# "24h" for the scheduled half-hourly runs, "week" for a one-off 7-day backfill.
# Week mode also turns on LinkedIn pagination — see fetch_linkedin_multi for why the
# 24h path deliberately does not.
TIME_RANGE = (os.environ.get("TIME_RANGE") or "24h").strip().lower()

# A source has to contribute at least this many jobs before its garbage/thin share
# means anything. An incremental run can bring in ONE Hirist job; if the model calls
# that single JD thin, a bare percentage test reads 100% and cries broken selectors.
MIN_JOBS_TO_FLAG_SOURCE = 5


def _print_jd_quality_summary(jobs: list) -> None:
    """How many jobs per source had no usable JD. A jump here means that source's
    selectors broke — previously invisible, because a JD-less job still got a
    confident-looking decision."""
    per_source = {}
    for job in jobs:
        quality = (job.get("evaluation", {}).get("features", {}) or {}).get("jd_quality", "unknown")
        bucket = per_source.setdefault(job.get("source", "(unknown)"), {})
        bucket[quality] = bucket.get(quality, 0) + 1

    if not per_source:
        return
    print("\n  JD quality by source:")
    for source in sorted(per_source):
        counts   = per_source[source]
        total    = sum(counts.values())
        unusable = counts.get("garbage", 0) + counts.get("thin", 0)
        detail   = ", ".join(f"{n} {q}" for q, n in sorted(counts.items()))
        flag = ("   ⚠️  check this source's selectors"
                if total >= MIN_JOBS_TO_FLAG_SOURCE and unusable >= total / 2 else "")
        print(f"    {source:<18} {detail}{flag}")


# ─────────────────────────────────────────────
# MAIN — single 24h run, then exit
# ─────────────────────────────────────────────
def main() -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*55}")
    print(f"  PM EVAL (HOSTED) — {'7-day backfill' if TIME_RANGE == 'week' else '24h catch-up'}")
    print(f"  [{now}]")
    print(f"  Sources: LinkedIn · Naukri · Hirist · IIMJobs")
    print(f"{'='*55}")

    if not SPREADSHEET_ID:
        print("  ERROR: PM_EVAL_SPREADSHEET_ID env var not set. Exiting.")
        sys.exit(1)

    if TIME_RANGE not in ("24h", "week"):
        print(f"  ERROR: TIME_RANGE must be '24h' or 'week', got {TIME_RANGE!r}. Exiting.")
        sys.exit(1)

    # Dedup MUST happen before evaluation — evaluating already-seen jobs burns
    # Claude API tokens for nothing. Load the real seen-set from the Sheet now,
    # not just at write time.
    # Fail here, not 200 jobs in: without the profile every job would be scored
    # against nothing. Never printed — this repo is public.
    try:
        load_candidate_profile()
        print("  Candidate profile: loaded from CANDIDATE_PROFILE")
    except Exception as e:
        print(f"  ERROR: {e}")
        sys.exit(1)

    try:
        seen = load_seen_urls(SPREADSHEET_ID)
        print(f"  Dedup: {len(seen)} known URL(s) from Sheet")
    except Exception as e:
        print(f"  ERROR: could not load dedup state from Sheet ({e}).")
        print("  Aborting run rather than risk re-evaluating everything at full API cost.")
        sys.exit(1)

    # Same collector the Gulf and global feeds use, so the per-reason counters show
    # exactly why jobs dropped out — otherwise a quiet run is indistinguishable from
    # an over-aggressive filter.
    # is_pm_eval_role (word boundaries, no BDM/category titles) applies to this feed
    # only — Gulf and Global keep the original is_pm_role gate inside _parse_li_cards.
    collector = JobCollector(seen_urls=seen, max_per_company=2, title_ok=is_pm_eval_role)

    for name, fetch_fn in SOURCES:
        icon = SOURCE_ICONS.get(name, "🔔")
        print(f"\n{icon} [{name}]")
        try:
            kwargs = {"time_range": TIME_RANGE}
            if name == "LinkedIn" and TIME_RANGE == "week":
                # ~300 per keyword instead of the search page's ~60.
                kwargs.update(paginate=True, limit=2000)
            jobs = fetch_fn(SEARCH_KEYWORD, **kwargs)
            if name != "LinkedIn":
                fresh = within_week if TIME_RANGE == "week" else within_24hrs
                jobs = [j for j in jobs if fresh(j)]
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

    evaluated_jobs, aborted = evaluate_batch(all_jobs, decider=decide)

    if not evaluated_jobs:
        print("\n  No jobs were successfully evaluated this run (API failures only).")
        run_summary("PM Eval — FAILED", 0, 0, 0)
        sys.exit(1)

    _print_jd_quality_summary(evaluated_jobs)

    # Save to Google Sheets — dedup happens inside save_eval_jobs too (belt & suspenders)
    n_apply, n_maybe, n_skip = save_eval_jobs(SPREADSHEET_ID, evaluated_jobs)

    # ntfy push notification
    label = "PM Eval — partial (API errors)" if aborted else "PM Eval"
    run_summary(label, n_apply, n_maybe, n_skip)

    print(f"\n{'='*55}")
    print(f"  Done. Apply: {n_apply}  Maybe: {n_maybe}  Skip: {n_skip}")
    print(f"  Rate: {LI_LIMITER.summary()}")
    print_token_report(len(evaluated_jobs))
    if aborted:
        print("  NOTE: run was aborted early due to repeated API errors — some jobs untouched, will retry next run.")
    print(f"{'='*55}\n")


    if aborted:
        sys.exit(1)  # surface as a failed run in GitHub Actions even though partial results were saved

if __name__ == "__main__":
    main()
