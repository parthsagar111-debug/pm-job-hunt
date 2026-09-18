# PM Job Hunt — technical brief

Written for external review. Measured numbers come from real runs; anything not
measured is called out as such. Last updated 2026-09-18, after the optimisation
pass described in "What changed" below.

## Purpose
Three scheduled scrapers that find product-manager jobs for one candidate (India-based,
9+ years, B2C / e-commerce / fintech), judge or filter them with the Claude API, and
append them to Google Sheets, with a phone push after each run.

Repo: `github.com/parthsagar111-debug/pm-job-hunt` (public, Python 3.11, single
`master` branch, 105 offline tests in CI).

## Execution model
- **GitHub Actions only.** Every workflow is `workflow_dispatch`-only; GitHub's own
  `schedule:` cron is deliberately unused.
- **cron-job.org** holds the schedules and triggers runs by POSTing to the GitHub REST
  API with a PAT (`{"ref":"master"}`).
- Each run is a **stateless one-shot process**: start, scrape, judge, append, notify,
  exit. No queue, no database, no retry store.
- **All state lives in the Google Sheet.** URL + fingerprint dedup sets are read at
  process start. A job written to the Sheet is never evaluated again.

| Feed | Schedule | Runtime before | Runtime now | Per-run cost |
|---|---|---|---|---|
| PM Eval (India) | every 30 min, 08:05–23:35 IST | 4–5 min | **1.7 min** | ~3–17 jobs judged |
| Gulf PM Eval | 12:10 and 21:10 IST | ~15 min | **1 min** | ~10 jobs |
| Global Visa | 03:00 IST daily | 115 min | **41 min** (12 countries + Adzuna) | **$0.03-0.29** |

## Files
- `core_eval_hosted.py` — shared engine: LinkedIn client (search, pagination, guest JD
  endpoint), the rate limiter, `JobCollector`, Selenium scrapers for the three Indian
  boards, `claude_structured`, `evaluate_job`, `evaluate_batch`.
- `pm_eval_hosted.py` — India feed: 9 LinkedIn keyword searches + 3 Selenium sites,
  24h window, Apply/Maybe/Skip.
- `gulf_eval_hosted.py` — GCC feed: 6 countries, deterministic Arabic-requirement override.
- `global_visa_hosted.py` — 12-country feed: visa-sponsorship filter, no fit judgment.
- `sponsor_registry.py` — UK/NL government sponsor registers, the company-level signal.
- `adzuna_source.py` — second job source; dormant without ADZUNA_APP_ID/KEY.
- `sheets_writer.py` — gspread I/O, dedup reads, row formatting.
- `ntfy_notify.py` — push notification.
- `tests/` — 105 offline tests; `.github/workflows/tests.yml` runs them on every push.

## The binding constraint: LinkedIn's rate limit
Measured against the guest endpoints from a clean IP, 25–60 requests per data point:

| Request rate | Rate-limited |
|---|---|
| 0.7 req/s (the old fixed-sleep pacing) | 0 / 25 |
| 1.0 req/s | 0 / 38 |
| 1.5 req/s | 27 / 60 |
| 2.5 req/s | 46 / 60 |
| ~9 req/s (4–8 concurrent, unpaced) | 25 / 40 and 19 / 40 |

**The wall is ~1 request/second per IP, and it is a rate limit, not a concurrency
limit.** Parallel workers reach the same wall faster; they don't lift it. This is why
the codebase uses a token bucket rather than `asyncio`/`httpx`, and why an async
rewrite would add risk for no throughput (`httpx` also can't do the TLS impersonation
`curl_cffi` provides, so it would likely be blocked outright).

`RateLimiter` in core paces every LinkedIn call in every feed, halves the rate on a
429/999 and eases back up after 20 clean requests. Every run prints
`requests / rate-limited / final rate`; the last three production runs reported
**0 rate-limited** at a sustained 1.00 req/s.

## Pipeline detail
**1. Search.** `curl_cffi` with `impersonate="chrome"` for the TLS fingerprint.
`/jobs/search/` returns ~60 cards; `jobs-guest/.../seeMoreJobPostings/search?start=N`
pages 10 at a time to a hard ceiling near 1,000 results, stopping when a page yields no
unseen IDs. The US always hits that ceiling, so the global feed splits it by state.
Naukri/Hirist/IIMJobs still use headless Selenium — no browserless equivalent has been
verified for them, and that feed is ~100 seconds anyway.

**2. Filter and dedup — `JobCollector`.** One shared path: skip IDs seen this run,
location filter, title filter, drop anything already in the Sheet by URL, collapse a
role posted in several cities within the same run (keeping one row with the locations
joined), cap roles per company. Per-reason counters are
printed so nothing vanishes silently.

**3. Job descriptions.** `jobs-guest/jobs/api/jobPosting/{id}`, parsed with
BeautifulSoup — no browser. 838/838 and 2,570/2,575 in the last two global runs.
Playwright was removed entirely: it cost a 300MB chromium install per workflow run.

**4. Claude.** `claude-haiku-4-5-20251001` via a forced tool call with an enum schema.
Anything malformed — no `tool_use` block, missing field, value outside the enum —
raises and becomes a retryable `Error`, never a decision. `Error` rows are excluded
from the Sheet, so the job is retried next run; 3 consecutive failures abort the batch.
The global feed sends only the visa-matching lines (±2 lines of context) rather than
the whole JD: $0.75/run → $0.16.

**5. Deterministic override.** The Gulf feed forces Skip when the JD makes Arabic
mandatory, because Haiku wrote "Arabic fluency is required" as its reason and still
answered Maybe. It fires only on positive evidence (a spoken/written skill, plus either
"required/must" on the line or a requirements-type heading above it) — an earlier,
looser version produced three false Skips on real jobs, all pinned as tests now.

**6. Write.** `append_rows`, a second dedup pass at write time, then one `batch_update`
setting CLIP wrap and 21px row height so the multi-line JD cell doesn't inflate rows.
Sheets calls use exponential backoff on 429/5xx.

## What changed in the optimisation pass (2026-09-17/18)
1. **Schema-enforced Claude output** — fixes the silent `Skip` fallback that permanently
   blackholed any job whose reply didn't parse.
2. **Guest JD endpoint in all three feeds**, Playwright deleted.
3. **Fingerprint dedup, tried and reverted.** `company|title|country` matched against
   Sheet history blocked far too much: one PM Eval run kept 7 jobs and dropped 168 on
   fingerprints versus 2 on already-seen URLs, because large Indian employers repost
   the same title constantly and each posting is a job worth seeing. A 21-day key
   expiry didn't help (547 of 603 rows were already inside the window). Sheet-level
   dedup is URL-only again; `JobCollector` still merges the same company+title found
   twice **within one run**, joining the locations — measured at 8 of 148 jobs on live
   India data, all genuine duplicates.
4. **One adaptive rate limiter** replacing fixed per-call-site sleeps, which had been
   spending roughly half the available request budget on dead time.
5. **`JobCollector`** replacing two drifting copies of the filter/dedup loop.
6. **105 offline tests + CI.**
7. **404 handling** — an expired posting returns immediately instead of ~2 minutes of
   backoff; LinkedIn's non-standard 999 status is handled explicitly.

### Reviewer proposals deliberately NOT adopted
- **SQLite as the state store.** GitHub runners are ephemeral and no persistence
  mechanism was specified. Every option is worse than the Sheet today: committing a
  binary DB churns the repo, Actions cache can be evicted (losing it means re-paying
  Claude for everything), artifacts need extra plumbing. It was also justified by
  "minute-long Sheet reads" — measured, the dedup read is **0.65 s** for an empty sheet
  and sub-second for ~500 URLs. Revisit when a dedup read exceeds ~10 s.
- **Sheets carrying only Apply/Maybe.** Skip rows are the dedup memory and deliberately
  carry JD text for the separate job-automation project (commit `c743dae`). Dropping
  them, combined with an ephemeral local DB, would mean re-judging rejected jobs forever.
- **`asyncio` + `httpx` + `Semaphore(8)`.** See the rate-limit table: no throughput to
  gain, and `httpx` loses the TLS impersonation.
- **Trimming JDs to the Qualifications section for the fit feeds.** The fit judgment
  needs the "About the role" context, and those feeds judge ~3–17 jobs per run, so the
  saving is cents.
- **Converting ~200 `print()` calls to `logging`.** In Actions the printed lines are the
  log UX and timestamps are already added by the runner; the rewrite is churn with
  regression risk and no functional gain.

## Known weaknesses that remain
1. **Search coverage is capped** at ~1,000 results per query. The US needs a per-state
   split; some state/day combinations may still truncate. Shorter `f_TPR` windows
   (r3600, r21600) are not honoured, so time isn't a usable shard axis.
2. **Sheet-as-database grows unbounded.** Dedup now reads four columns per tab per run
   instead of one. Fine at hundreds of rows; revisit at tens of thousands.
3. **Scraper fragility.** Selenium selectors are hardcoded CSS class fragments; LinkedIn
   card parsing swallows per-card errors, so a markup change degrades to a silent zero.
4. **Recency filtering is lenient**: an unparseable "posted" string passes the window.
5. **Re-posts are re-judged.** With Sheet-level dedup back to URL-only, the same role
   re-advertised under a new LinkedIn id is evaluated again. That's deliberate — the
   alternative hid real openings — and costs fractions of a cent per job.
6. **Detection only finds employers who say it.** Many companies sponsor visas without
   mentioning it, so the global feed's recall is bounded by JD wording, not by the code.
7. **Public repo.** The candidate profile sits in the prompt in plain sight; secrets are
   Actions secrets, but run logs are public.
8. **Judgment quality.** ~60% of Gulf results land in Maybe against a 35% target, so the
   fit judgment carries little signal at the margin.

## Sources beyond LinkedIn (added 2026-09-18)
JD text alone yields almost nothing in steady state: known sponsors are deduped
forever, so a 1,500-role day produced 0-2 confirmed offers. Two additions:

**Government sponsor registers** — the employer side of the same fact, so a company
can be checked when its listing is silent. UK Register of Licensed Sponsors (free
CSV followed from the gov.uk page, ~121,500 Skilled Worker entries) and the NL IND
Public Register (~12,960, HTML table). Own Sheet column, never a decision: a licence
means the employer CAN sponsor. Matching is conservative — registry entries are legal
names, so "Deliveroo" resolves only via "ROOFOODS LTD T/A DELIVEROO", and a company
called "Starling" matches two entries and is therefore left unmatched. Measured:
**50 jobs at licensed sponsors in one run, 49 with JDs that never mention visas.**

**Adzuna** — one JSON API over 10 of the 12 countries (Ireland isn't hosted; Sweden
and Cyprus have no site). **+531 jobs in one run** on top of LinkedIn. Use
`title_only`, not `what`: free-text search dragged in 1,297 non-PM titles. Its
descriptions are truncated (~200 chars), so it contributes discovery and the licence
column rather than JD-based proof. Adzuna reports region-level locations
("London, UK"), which silently bypassed the register lookup until the country was
attached at the source — see `location_country`'s alias map.

## Questions worth a second opinion
- Given a hard ~1,000-result ceiling per query and no working time-slicing, what's the
  best axis to shard searches on for full coverage?
- Is there a dedup key better than `company|title|country` that survives re-posts without
  merging genuinely distinct openings?
- Would per-country IP diversity (proxies) be worth it, or is ~1 req/s adequate now that
  a full global sweep is 22 minutes?
- Is the Maybe-heavy distribution a prompt problem or a profile-definition problem?
