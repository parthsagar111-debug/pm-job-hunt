"""
global_visa_hosted.py — Worldwide PM jobs that offer visa sponsorship
=====================================================================
Third feed, after pm_eval_hosted.py (India) and gulf_eval_hosted.py (GCC).
No Apply/Maybe/Skip fit evaluation: a job is kept only if its JD says the
employer sponsors/supports a visa. Relocation support alone does NOT count.

Countries: everywhere except India and the Gulf — those two are covered by the
other feeds. The US is split by state when a country search hits LinkedIn's
~1000-result pagination cap.

Runs ONCE and exits. Triggered via workflow_dispatch (cron-job.org or manually).

Source:   LinkedIn (per-country search + guest JD endpoint, no browser), plus
          Adzuna when ADZUNA_APP_ID/ADZUNA_APP_KEY are set
Output:   Google Sheet — "Listings" (YES / CONDITIONAL) and "No Sponsorship"
          (the rejected ones, with the quoted evidence)
Notify:   ntfy push notification after run

Required environment variables (set as GitHub Actions secrets):
  ANTHROPIC_API_KEY             — Claude API key
  GOOGLE_SERVICE_ACCOUNT_JSON   — full JSON contents of service account key
  GLOBAL_VISA_SPREADSHEET_ID    — Google Sheet ID for this script's output
  NTFY_TOPIC                    — your ntfy topic name
Optional:
  ADZUNA_APP_ID / ADZUNA_APP_KEY — free key from developer.adzuna.com; without
                                   them the Adzuna source is skipped entirely
  TIME_RANGE                    — "24h" (default) or "week"

Sizing, from the 2026-09-17 probe of one 24h window (see git history, branch
probe/global-visa): 2,575 product roles worldwide → 426 JDs mentioning visa
terms → 11 YES + 6 CONDITIONAL. Runtime ~2h15m, Haiku cost $0.75 when whole
JDs were sent; this version sends only the visa lines, so ~$0.08/run.
"""

import os
import random
import re
import sys
import time
import urllib.parse
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests

from core_eval_hosted import (
    _li_get_with_retry, _parse_li_cards, _LI_TPR, _load_api_key, claude_structured,
    fetch_jd_guest, JobCollector, LI_LIMITER,
)
from gulf_eval_hosted import is_gulf_location
from sponsor_registry import SponsorRegistry
import adzuna_source
from sheets_writer import save_visa_jobs, load_seen_urls, TAB_LISTINGS, TAB_NO_SPONSOR
from ntfy_notify import push

SPREADSHEET_ID = os.environ.get("GLOBAL_VISA_SPREADSHEET_ID", "")
TIME_RANGE     = (os.environ.get("TIME_RANGE") or "24h").strip().lower()
MAX_RUNTIME_S  = 5 * 3600   # GitHub's job cap is 6h — stop early and still save
T0             = time.time()

# ─────────────────────────────────────────────
# COUNTRIES — LinkedIn location names. India + GCC excluded (other feeds cover them).
# ─────────────────────────────────────────────
# Cut from 128 countries to these 10 on 2026-09-17, Parth's call, on the evidence
# of two full worldwide sweeps (the probe and the first live run): EVERY sponsoring
# role found came from the US, UK, Germany, Spain, Canada or Cyprus, and 82 of the
# 128 countries returned no product roles at all. The zero-yield countries kept
# here (Netherlands, Ireland, Singapore, Australia) do sponsor Indians readily —
# their listings just don't say so yet, and they're cheap to keep. Dropping the
# rest took a run from ~115 min to ~70 without losing a single hit.
COUNTRIES = [
    "United States",    # 909 roles/day, 5 hits — split by state below, it always hits the cap
    "United Kingdom",   # 129 roles/day, 2 hits
    "Germany",          #  72 roles/day, 1-2 hits
    "Canada",           #  61 roles/day, 1 hit in the probe
    "Spain",            #  43 roles/day, 1 hit in the probe
    "Netherlands",      #  38 roles/day, 0 JD hits — but the IND sponsor register covers it
    "Australia",        #  18 roles/day, 0 hits so far
    "Ireland",          #  29 roles/day, 0 hits so far
    "Cyprus",           # tiny market, but 1 hit in both runs
    # Added 2026-09-18, replacing Singapore (2 full sweeps, 0 hits, no sponsor register):
    "France",           #  71-81 roles/day — highest volume of the countries not yet covered
    "Poland",           #  42 roles/day — IT sector that relocates routinely
    "Sweden",           #  24 roles/day — employer-led permits, English-first workplaces
]

US_STATES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware",
    "District of Columbia", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa",
    "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey",
    "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
    "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah",
    "Vermont", "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming",
]

CAP_NEAR = 950   # scanned ≥ this → LinkedIn's ~1000 ceiling probably truncated the results

_INDIA = re.compile(r"\b(india|bengaluru|bangalore|mumbai|delhi|new delhi|gurugram|gurgaon|noida|"
                    r"hyderabad|pune|chennai|kolkata|ahmedabad)\b", re.I)

# ─────────────────────────────────────────────
# TITLE FILTER — product roles only, no junior, no design/engineering-of-product titles
# ─────────────────────────────────────────────
# core.is_pm_role is deliberately not used: it also admits BDM / category /
# growth / strategy titles, which balloon this feed's JD-fetch volume.
_PRODUCT_TITLE = re.compile(
    r"\bproduct\b.*\b(manager|owner|lead|head|director|management|officer)\b"
    r"|\b(head|director|vp|vice president|chief)\b.*\bproduct\b"
    r"|\bcpo\b|\b(group|senior|principal|staff) pm\b", re.I)
_JUNIOR = re.compile(r"\b(associate product|apm|intern\w*|graduate|junior|entry[- ]level|trainee"
                     r"|working student|apprentice)\b", re.I)
# "Director of Product Design" slipped through the probe — it's a design role, not PM.
_NOT_PM = re.compile(r"\bproduct (design\w*|engineer\w*|market\w* (analyst|specialist)|support"
                     r"|develop(er|ment engineer)|architect|data scientist)\b"
                     r"|\b(ux|ui|graphic|industrial) design\w*\b", re.I)

def is_product_role(title: str) -> bool:
    return bool(_PRODUCT_TITLE.search(title)) and not _JUNIOR.search(title) and not _NOT_PM.search(title)

# ─────────────────────────────────────────────
# SEARCH
# ─────────────────────────────────────────────
def search(location: str, keyword: str = "Product Manager",
           time_range: str = "24h") -> tuple[list[dict], int, int]:
    """Returns (PM-titled jobs, listings scanned, pages). Paginates to LinkedIn's cap."""
    q = (f"keywords={urllib.parse.quote(keyword)}&location={urllib.parse.quote(location)}"
         f"&f_TPR={_LI_TPR.get(time_range, 'r86400')}&sortBy=DD")
    r = _li_get_with_retry("https://www.linkedin.com/jobs/search/?" + q)
    if r is None:
        return [], 0, 0
    hits, ids = _parse_li_cards(r.text)
    pages = 1
    while pages < 101:
        r = _li_get_with_retry("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
                               f"?{q}&start={len(ids)}")
        if r is None:
            break
        page_hits, page_ids = _parse_li_cards(r.text)
        if not page_ids - ids:
            break
        ids |= page_ids
        hits.extend(page_hits)
        pages += 1
    return hits, len(ids), pages

def collect_jobs(seen: set[str]) -> list[dict]:
    """One search per country (US by state if it hits the cap), keeping new product
    roles outside India/the Gulf. A role posted in several cities is kept once, with
    the other locations appended — Anthropic's PM Growth showed up in 3 US cities."""
    collector = JobCollector(
        seen_urls=seen,
        location_ok=lambda loc: not (_INDIA.search(loc) or is_gulf_location(loc)),
        title_ok=is_product_role,
        source="LinkedIn Global",
    )

    def run(label, location):
        hits, scanned, pages = search(location, time_range=TIME_RANGE)
        before = collector.counts["kept"]
        for j in hits:
            collector.add(j, label=label)
        print(f"  {label:<34} scanned {scanned:>4} / {pages:>3} pages "
              f"→ +{collector.counts['kept'] - before}"
              + ("   ⚠️ near LinkedIn's 1000 cap" if scanned >= CAP_NEAR else ""), flush=True)
        return scanned

    for country in COUNTRIES:
        if time.time() - T0 > MAX_RUNTIME_S * 0.5:
            print(f"  ⏱ half the runtime budget used — stopping the country sweep at {country}", flush=True)
            break
        scanned = run(country, country)
        if country == "United States" and scanned >= CAP_NEAR:
            print("  United States hit the cap — splitting by state", flush=True)
            for st in US_STATES:
                run(f"US / {st}", f"{st}, United States")

    # Adzuna: a second source over the same countries, skipped entirely when the
    # credentials aren't set. Same collector, so the filters and dedup apply equally
    # and anything LinkedIn already returned is dropped as a duplicate.
    if adzuna_source.is_configured():
        print("\n  [adzuna] fetching...", flush=True)
        for country in COUNTRIES:
            before = collector.counts["kept"]
            for job in adzuna_source.fetch(country):
                collector.add(job, label=f"Adzuna / {country}")
            kept = collector.counts["kept"] - before
            print(f"  Adzuna {country:<24} → +{kept}", flush=True)
    else:
        print("\n  [adzuna] ADZUNA_APP_ID/ADZUNA_APP_KEY not set — LinkedIn only", flush=True)

    print(f"\n  Collected: {collector.summary()}")
    return collector.jobs

# JD fetch lives in core as fetch_jd_guest() — all three feeds share it now.

# "relocation" is deliberately absent: relocation alone doesn't qualify, so a JD
# that only offers relocation never reaches Claude.
_VISA_TERMS = re.compile(r"\b(visas?|sponsor\w*|work permits?|immigration|h-?1b|blue card|"
                         r"skilled worker|work authori[sz]ation|right to work|employment pass|"
                         r"green card)\b", re.I)

EXCERPT_CONTEXT_LINES = 2
EXCERPT_MAX_CHARS     = 3000

def visa_excerpt(jd: str) -> str:
    """The visa-relevant lines plus a little context — this is all Claude sees.
    The probe sent whole JDs and cost $0.75/run; excerpts cut that ~10x."""
    lines = [l.strip() for l in jd.split("\n")]
    wanted = set()
    for i, line in enumerate(lines):
        if _VISA_TERMS.search(line):
            wanted.update(range(max(0, i - EXCERPT_CONTEXT_LINES),
                                min(len(lines), i + EXCERPT_CONTEXT_LINES + 1)))
    if not wanted:
        return ""
    out, prev = [], None
    for i in sorted(wanted):
        if prev is not None and i > prev + 1:
            out.append("[...]")
        if lines[i]:
            out.append(lines[i])
        prev = i
    return "\n".join(out)[:EXCERPT_MAX_CHARS]

# ─────────────────────────────────────────────
# CLAUDE CLASSIFIER — visa support only, no fit judgment
# ─────────────────────────────────────────────
CLASSIFY_PROMPT = """You check job listings for one thing only: does the employer offer VISA sponsorship / visa support for this role?

You are shown only the visa-related lines of the job description, so ignore anything else about the role.

Answer YES only if the text states the employer sponsors, provides, supports or assists with a visa,
work permit, or immigration for this role (e.g. "visa sponsorship available", "we sponsor visas",
"relocation and visa support provided", "we will support your work permit application").
Answer CONDITIONAL if visa sponsorship is described as possible but not guaranteed
(e.g. "visa sponsorship may be available", "considered case by case", "sponsorship limited to certain roles").
Answer NO for everything else, including:
- no sponsorship / must already have the right to work / will not sponsor / citizens or green card holders only
- relocation support WITHOUT any visa or work-permit support mentioned (relocation alone is NO)
- "Visa" as a card brand, "sponsor" meaning retirement plans, events, or security clearance
- visa not really addressed

Record your answer with the record_visa tool: visa (YES / CONDITIONAL / NO) and
evidence (the shortest exact quote from the text supporting the answer, or "None")."""

VISA_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "visa":     {"type": "string", "enum": ["YES", "CONDITIONAL", "NO"],
                     "description": "Does the employer offer visa sponsorship for this role?"},
        "evidence": {"type": "string",
                     "description": "Shortest exact quote supporting the answer, or 'None'."},
    },
    "required": ["visa", "evidence"],
}

USAGE = Counter()

def classify(job: dict, excerpt: str) -> tuple[str, str]:
    """Returns (YES|CONDITIONAL|NO|ERROR, evidence). ERROR is never written to the
    Sheet, so the job is retried on the next run rather than silently dropped."""
    context = (f"Job Title: {job['title']}\nCompany: {job['company']}\n"
               f"Location: {job['location']}\n\nVisa-related lines from the job description:\n{excerpt}")
    last_err = ""
    for attempt in range(3):
        try:
            payload, usage = claude_structured(
                CLASSIFY_PROMPT + "\n\n" + context,
                tool_name="record_visa",
                tool_description="Record whether this listing offers visa sponsorship.",
                input_schema=VISA_TOOL_SCHEMA,
                max_tokens=200,
            )
            USAGE["input_tokens"]  += usage.get("input_tokens", 0)
            USAGE["output_tokens"] += usage.get("output_tokens", 0)
            return payload["visa"], payload["evidence"]
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(5 * (attempt + 1))
    print(f"    ⚠️  classify failed: {last_err}")
    return "ERROR", last_err

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main() -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*60}")
    print(f"  GLOBAL VISA PM FEED — {TIME_RANGE}")
    print(f"  [{now}]  {len(COUNTRIES)} countries, India + Gulf excluded")
    print(f"{'='*60}")

    if TIME_RANGE not in ("24h", "week"):
        print(f"  ERROR: TIME_RANGE must be '24h' or 'week', got {TIME_RANGE!r}. Exiting.")
        sys.exit(1)
    if not SPREADSHEET_ID:
        print("  ERROR: GLOBAL_VISA_SPREADSHEET_ID env var not set. Exiting.")
        sys.exit(1)
    _load_api_key()   # fail fast if the key is missing

    try:
        # Both tabs: a job already logged as "no sponsorship" shouldn't be
        # re-fetched and re-classified every day for the same answer.
        seen = load_seen_urls(SPREADSHEET_ID, tabs=(TAB_LISTINGS, TAB_NO_SPONSOR))
        print(f"  Dedup: {len(seen)} known URL(s) loaded from Sheet")
    except Exception as e:
        print(f"  ERROR: could not load dedup state from Sheet ({e}). Aborting.")
        sys.exit(1)

    print("\n  [1/2] Searching LinkedIn per country...")
    jobs = collect_jobs(seen)
    print(f"\n  {len(jobs)} new product role(s) after {(time.time()-T0)/60:.0f} min of searching")

    print("\n  [2/2] Fetching JDs and checking visa support...")
    keepers, rejected, counts = [], [], Counter()
    registry = SponsorRegistry()
    jd_ok = jd_fail = 0
    for i, job in enumerate(jobs, 1):
        if time.time() - T0 > MAX_RUNTIME_S:
            print(f"  ⏱ runtime limit reached at {i}/{len(jobs)} — saving what's done; "
                  f"the rest will be picked up next run.", flush=True)
            break
        # Adzuna hands us a truncated description with the listing; LinkedIn needs a fetch.
        jd = job.get("jd_text") or (fetch_jd_guest(job["job_id"])
                                    if job["job_id"].startswith("li_") else "")
        if len(jd) < 100:
            jd_fail += 1
            continue
        jd_ok += 1
        excerpt = visa_excerpt(jd)
        if not excerpt:
            counts["no visa terms"] += 1
            # The JD says nothing about visas — but if the employer holds a UK/NL
            # sponsor licence, that's exactly the case the registers exist for, and
            # the job is worth surfacing. Recorded as NOT STATED so the Visa column
            # never implies an offer that wasn't made.
            licence = registry.lookup(job.get("company", ""), job.get("location", ""))
            if licence:
                job["visa_verdict"]   = "NOT STATED"
                job["visa_evidence"]  = ""
                job["sponsor_licence"] = licence
                rejected.append(job)
                counts["licensed sponsor, JD silent"] += 1
            continue
        verdict, evidence = classify(job, excerpt)
        counts[verdict] += 1
        job["visa_verdict"]   = verdict
        job["visa_evidence"]  = evidence
        job["sponsor_licence"] = registry.lookup(job.get("company", ""), job.get("location", ""))
        if verdict in ("YES", "CONDITIONAL"):
            job["jd"] = jd
            keepers.append(job)
            print(f"  [{i}/{len(jobs)}] ✅ {verdict}: {job['title']} @ {job['company']} "
                  f"({job['location']}) — {evidence[:110]}", flush=True)
        elif verdict == "NO":
            # Logged to the "No Sponsorship" tab: these JDs DID mention a visa term,
            # so the evidence quote shows what Claude read it as — usually an explicit
            # refusal. ERROR is left out: it means we never got an answer, so the job
            # must stay un-recorded and be retried next run.
            rejected.append(job)
        if i % 200 == 0:
            print(f"  … {i}/{len(jobs)} JDs done, {len(keepers)} sponsoring so far", flush=True)

    cost = USAGE["input_tokens"] / 1e6 * 1.0 + USAGE["output_tokens"] / 1e6 * 5.0  # Haiku 4.5 $1/$5 per MTok
    n_licensed = sum(1 for j in keepers + rejected if j.get("sponsor_licence"))
    print(f"\n  Rate: {LI_LIMITER.summary()}")
    print(f"  Sponsor registers: {n_licensed} job(s) at a licensed UK/NL sponsor, "
          f"{counts['licensed sponsor, JD silent']} of them with a JD that never mentions visas"
          + (f" | {'; '.join(registry.errors)}" if registry.errors else ""))
    print(f"  JDs: {jd_ok} fetched, {jd_fail} failed  |  "
          f"visa lines found: {sum(counts[k] for k in ('YES','CONDITIONAL','NO','ERROR'))}  |  "
          f"YES {counts['YES']}  CONDITIONAL {counts['CONDITIONAL']}  NO {counts['NO']}  "
          f"ERROR {counts['ERROR']}  |  Haiku ${cost:.2f}")

    n_new, n_no = ((0, 0) if not (keepers or rejected)
                   else save_visa_jobs(SPREADSHEET_ID, keepers, rejected))

    n_yes  = sum(1 for j in keepers if j["visa_verdict"] == "YES")
    n_cond = len(keepers) - n_yes
    push("Global Visa PM — run complete",
         f"{n_new} new listing(s): {n_yes} sponsor, {n_cond} maybe\nCheck your Google Sheet for details.",
         "high" if n_new else "default")

    print(f"\n{'='*60}")
    print(f"  Done in {(time.time()-T0)/60:.0f} min. New rows: {n_new} "
          f"({n_yes} YES, {n_cond} CONDITIONAL)")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    main()
