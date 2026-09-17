"""
gulf_eval_hosted.py — Gulf PM Jobs (hosted / GitHub Actions version)
====================================================================
Replaces the old worldwide LinkedIn Global feed. Only searches GCC countries
(UAE, Saudi Arabia, Qatar, Bahrain, Oman, Kuwait), where the employer sponsors
the work visa as a matter of course and Indian hires are routine — so there's
no per-job "do they sponsor visas?" check like the worldwide feed needed.

Runs ONCE and exits. Triggered via workflow_dispatch (cron-job.org or manually).

Source:   LinkedIn (per-country location search, paginated)
Output:   Google Sheet (Apply / Maybe / Skip tabs, same columns as PM Eval)
Notify:   ntfy push notification after run

Required environment variables (set as GitHub Actions secrets):
  ANTHROPIC_API_KEY             — Claude API key
  GOOGLE_SERVICE_ACCOUNT_JSON   — full JSON contents of service account key
  GULF_SPREADSHEET_ID           — Google Sheet ID for this script's output
  NTFY_TOPIC                    — your ntfy topic name
Optional:
  TIME_RANGE                    — "24h" (default) or "week" for a 7-day backfill
"""

import sys
import os
import re
import time
import random

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core_eval_hosted import fetch_linkedin, evaluate_batch
from sheets_writer import save_eval_jobs, load_seen_urls
from ntfy_notify import run_summary
from playwright_browser import close_browser
from datetime import datetime

SPREADSHEET_ID = os.environ.get("GULF_SPREADSHEET_ID", "")
TIME_RANGE     = (os.environ.get("TIME_RANGE") or "24h").strip().lower()

# ─────────────────────────────────────────────
# SEARCHES
# ─────────────────────────────────────────────
# Searches are paginated, so one broad "Product Manager" search already pulls
# Senior/Group/Principal PM listings — extra keywords only for the two big
# markets, to catch titles LinkedIn's matching doesn't fold in.
GULF_SEARCHES = [
    ("United Arab Emirates", ["Product Manager", "Product Owner", "Head of Product"]),
    ("Saudi Arabia",         ["Product Manager", "Product Owner", "Head of Product"]),
    ("Qatar",                ["Product Manager"]),
    ("Bahrain",              ["Product Manager"]),
    ("Oman",                 ["Product Manager"]),
    ("Kuwait",               ["Product Manager"]),
]

# LinkedIn's location search leaks the odd remote/elsewhere listing, and card
# locations are sometimes just a city or metro ("Riyadh Region", "Doha
# Metropolitan Area") — so match countries AND major cities, on word boundaries
# ("oman" must not match "Romania").
_GULF_LOCATION = re.compile(
    r"\b("
    r"united arab emirates|uae|dubai|abu dhabi|sharjah|ajman|ras al khaimah|fujairah|al ain|"
    r"saudi arabia|ksa|riyadh|jeddah|jiddah|dammam|khobar|al khobar|dhahran|neom|mecca|makkah|medina|"
    r"qatar|doha|lusail|al rayyan|"
    r"bahrain|manama|"
    r"oman|muscat|"
    r"kuwait"
    r")\b",
    re.I,
)

def is_gulf_location(location: str) -> bool:
    return bool(_GULF_LOCATION.search(location or ""))

MAX_PER_COMPANY = 3   # per run — stops one big hirer flooding a week-long backfill

# ─────────────────────────────────────────────
# CLAUDE PROMPT
# ─────────────────────────────────────────────
GULF_EVAL_PROMPT = """You are a recruiter evaluating Gulf (GCC) job listings for a candidate based in India.
Give an Apply / Maybe / Skip decision with a one-line reason.

Use this distribution as a rough guide: ~20% Apply, ~35% Maybe, ~45% Skip.
When in doubt between Apply and Maybe, pick Maybe. When in doubt between Maybe and Skip, pick Maybe.
Only Skip when there is a clear disqualifying reason.

Candidate Profile:
- Title: Senior Product Manager, 9+ years experience
- Domain: B2C consumer internet, D2C e-commerce, health & wellness, fintech, food-tech
- Strengths: Funnel optimisation, A/B experimentation, AI-powered personalisation,
  lifecycle engagement, retention, monetisation, SQL, Mixpanel, WebEngage, MoEngage
- Education: MBA (Chetana Institute), BMS Marketing — no CS/B.Tech degree
- Location: Mumbai, India — ready to relocate to the UAE, Saudi Arabia, Qatar, Bahrain, Oman or Kuwait
- Visa: Indian citizen; will need the employer-sponsored work visa, which is standard in the GCC.
  Do NOT treat "needs visa sponsorship" as a gap unless the listing explicitly rules it out.
- Languages: English, Hindi — does not speak Arabic

Apply if: title and seniority match, domain overlaps even partially, no hard blockers.
Maybe if: title fits but domain is unfamiliar, or seniority is off but role is interesting —
this includes cases where the role's stated experience range is lower or higher than the
candidate's 9+ years. A numeric experience-range mismatch by itself is NEVER a Skip.
Also Maybe if: the listing prefers candidates already in the country / immediate joiners,
or lists Arabic as "preferred" / "a plus" rather than required.

Hard Skip ONLY if:
1. Restricted to local nationals — e.g. "UAE Nationals only", "Emiratisation", "Saudi nationals only",
   "Saudization", "Qatari nationals", "Kuwaiti nationals", "Omani nationals", "Bahraini nationals"
2. Arabic fluency is explicitly required / mandatory (not just preferred)
3. Explicitly requires B.Tech/CS degree (not just "preferred")
4. Role title itself is explicitly junior — "Associate Product Manager" or "APM" in the title
5. Domain is purely supply chain, warehouse ops, or clinical healthcare with no consumer product angle
6. Role title is completely unrelated — project coordinator, account manager, program manager
7. Clearly requires deep expertise in a domain with zero overlap (e.g. oil & gas engineering, defence)

Respond ONLY in this exact format — no extra text, no preamble:
Decision: Apply / Maybe / Skip
Reason: [max 15 words]
Gap: [biggest gap or None]"""

# ─────────────────────────────────────────────
# FETCH
# ─────────────────────────────────────────────
def fetch_gulf_jobs(time_range: str, seen: set) -> list:
    all_jobs       = []
    seen_ids       = set()
    company_counts = {}

    for country, keywords in GULF_SEARCHES:
        print(f"\n🌴 [{country}]")
        for kw in keywords:
            print(f"    [{kw}]", end=" ", flush=True)
            jobs = fetch_linkedin(kw, time_range, location=country, paginate=True, limit=1000)
            new = off_region = 0
            for job in jobs:
                if job["job_id"] in seen_ids:
                    continue
                seen_ids.add(job["job_id"])
                if job["url"].split("?")[0] in seen:
                    continue
                if not is_gulf_location(job["location"]):
                    off_region += 1
                    continue
                co = job.get("company", "").lower().strip()
                if company_counts.get(co, 0) >= MAX_PER_COMPANY:
                    continue
                company_counts[co] = company_counts.get(co, 0) + 1
                job["source"] = "LinkedIn Gulf"
                all_jobs.append(job)
                new += 1
            print(f"    → {new} new" + (f"  ({off_region} outside the Gulf dropped)" if off_region else ""))
            time.sleep(random.uniform(8, 14))   # spacing reduces LinkedIn 429s
    return all_jobs

# ─────────────────────────────────────────────
# MAIN — single run, then exit
# ─────────────────────────────────────────────
def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*55}")
    print(f"  GULF PM EVAL (HOSTED) — {TIME_RANGE}")
    print(f"  [{now}]")
    print(f"  Countries: {' · '.join(c for c, _ in GULF_SEARCHES)}")
    print(f"{'='*55}")

    if TIME_RANGE not in ("24h", "week"):
        print(f"  ERROR: TIME_RANGE must be '24h' or 'week', got {TIME_RANGE!r}. Exiting.")
        sys.exit(1)
    if not SPREADSHEET_ID:
        print("  ERROR: GULF_SPREADSHEET_ID env var not set. Exiting.")
        sys.exit(1)

    # Dedup before evaluation — evaluating already-seen jobs burns Claude API tokens.
    try:
        seen = load_seen_urls(SPREADSHEET_ID)
        print(f"  Dedup: {len(seen)} known URL(s) loaded from Sheet")
    except Exception as e:
        print(f"  ERROR: could not load dedup state from Sheet ({e}).")
        print("  Aborting run rather than risk re-evaluating everything at full API cost.")
        sys.exit(1)

    all_jobs = fetch_gulf_jobs(TIME_RANGE, seen)

    print(f"\n{'='*55}")
    print(f"  Total: {len(all_jobs)} jobs  |  Evaluating with Claude AI...")
    print(f"{'='*55}\n")

    if not all_jobs:
        print("  Nothing new this run.")
        run_summary("Gulf PM Eval", 0, 0, 0)
        return

    try:
        evaluated_jobs, aborted = evaluate_batch(all_jobs, GULF_EVAL_PROMPT)
    finally:
        try:
            close_browser()
        except Exception:
            pass

    if not evaluated_jobs:
        print("\n  No jobs were successfully evaluated this run (API failures only).")
        run_summary("Gulf PM Eval — FAILED", 0, 0, 0)
        sys.exit(1)

    n_apply, n_maybe, n_skip = save_eval_jobs(SPREADSHEET_ID, evaluated_jobs)

    label = "Gulf PM Eval — partial (API errors)" if aborted else "Gulf PM Eval"
    run_summary(label, n_apply, n_maybe, n_skip)

    print(f"\n{'='*55}")
    print(f"  Done. Apply: {n_apply}  Maybe: {n_maybe}  Skip: {n_skip}")
    if aborted:
        print("  NOTE: run was aborted early due to repeated API errors — some jobs untouched, will retry next run.")
    print(f"{'='*55}\n")

    if aborted:
        sys.exit(1)  # surface as a failed run in GitHub Actions even though partial results were saved

if __name__ == "__main__":
    main()
