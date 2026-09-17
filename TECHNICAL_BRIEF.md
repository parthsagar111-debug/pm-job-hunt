# PM Job Hunt — technical brief

Written for external review (2026-09-18). Measured numbers come from the probe and
live runs of 2026-09-17; anything not measured is called out as such.

## Purpose
Three scheduled scrapers that find product-manager jobs for one candidate (India-based,
9+ years, B2C / e-commerce / fintech), judge or filter them with the Claude API, and
append them to Google Sheets, with a phone push after each run.

Repo: `github.com/parthsagar111-debug/pm-job-hunt` (public, Python 3.11, single
`master` branch, no test suite).

## Execution model
- **GitHub Actions only.** Every workflow is `workflow_dispatch`-only; GitHub's own
  `schedule:` cron is deliberately unused.
- **cron-job.org** holds the schedules and triggers runs by POSTing to the GitHub REST
  API with a PAT (`{"ref":"master"}`).
- Each run is a **stateless one-shot process**: start, scrape, judge, append, notify,
  exit. No queue, no database, no retry store.
- **All state lives in the Google Sheet.** The de-dup set is the URL column, read at
  process start. A job written to the sheet will never be looked at again.

| Feed | Schedule | Runtime | Per-run volume / cost |
|---|---|---|---|
| PM Eval (India) | every 30 min | 4–5 min | ~3 jobs evaluated |
| Gulf PM Eval | 12:10 and 21:10 IST | ~15 min (24h mode) | ~10 jobs |
| Global Visa | 03:00 IST daily | ~70 min | ~$0.18 Claude |

## Files
- `core_eval_hosted.py` — shared engine: LinkedIn search/parse/pagination, Selenium
  scrapers (Naukri, Hirist, IIMJobs), JD fetch dispatch, Claude call, `evaluate_batch`.
- `pm_eval_hosted.py` — India feed: 9 LinkedIn keyword searches + 3 Selenium sites,
  24h window, Apply/Maybe/Skip.
- `gulf_eval_hosted.py` — GCC feed: 6 countries, deterministic Arabic-requirement
  override, cross-country dedup.
- `global_visa_hosted.py` — 10-country feed: visa-sponsorship filter, no fit judgment.
- `sheets_writer.py` — gspread I/O, dedup reads, row formatting.
- `playwright_browser.py` — shared Playwright session (LinkedIn JD fetch, India/Gulf feeds).
- `ntfy_notify.py` — push notification.

## Pipeline detail

**1. Search.** LinkedIn's logged-out endpoints, via `curl_cffi` with
`impersonate="chrome"` for the TLS fingerprint:
- `www.linkedin.com/jobs/search/?keywords=…&location=…&f_TPR=r86400&sortBy=DD`
  returns ~60 cards.
- `jobs-guest/jobs/api/seeMoreJobPostings/search?…&start=N` pages 10 at a time, to a
  hard ceiling near 1,000 results. Pagination stops when a page yields no unseen IDs.
- 429 handling: 3 attempts, 45–95 s backoff. Measured rate: **1 block in 4,147
  requests** with 5–9 s between searches, 1.5 s between pages.
- Naukri / Hirist / IIMJobs use headless Selenium with `webdriver-manager`,
  CSS-class selectors, scroll-to-load.

**2. Filter.** Title regexes (each feed has its own), 24h recency, location regexes
(India and Gulf are mutually exclusive across feeds), per-company caps, then the URL
dedup set.

**3. Job descriptions.** Two mechanisms, and this is the biggest inconsistency in the
codebase:
- India/Gulf: Playwright for LinkedIn (one shared browser), **a new Selenium Chrome
  per job** for the other three sites.
- Global Visa: `jobs-guest/jobs/api/jobPosting/{id}` parsed with BeautifulSoup — no
  browser, ~1.8 s/job, **2,570 / 2,575 succeeded**. The India/Gulf feeds still don't
  use this.

**4. Claude.** Raw `requests.post` to `/v1/messages`, `claude-haiku-4-5-20251001`, no
SDK, no structured outputs. Responses are parsed by string-prefix matching on
`Decision:` / `Reason:` / `Gap:` (or `VISA:` / `EVIDENCE:`).
- India/Gulf: fit judgment against a profile in the prompt, ~20/35/45 target split for
  Apply/Maybe/Skip. Whole JD sent (4,000-char cap).
- Global Visa: a yes/no classifier, not a fit judgment. Only the visa-matching lines
  ±2 lines of context go to the model (3,000-char cap), which took cost from $0.75 to
  $0.18 a run at 7–23% of the JD text.
- `decision == "Error"` is excluded from the sheet so the job is retried next run;
  3 consecutive failures abort the batch.

**5. Deterministic overrides.** The Gulf feed post-processes Haiku: if the JD makes
Arabic mandatory (skill phrase, not NLP/RTL/localisation, and either "required/must" on
the line or a requirements-type section heading above it), a non-Skip decision is forced
to Skip. This exists because Haiku wrote "Arabic fluency is required" as its reason and
still answered Maybe.

**6. Write.** `append_rows` per tab; a second dedup pass at write time; then one
`batch_update` sets CLIP wrap and 21 px row height across all data rows so the
multi-line JD cell doesn't inflate them. Sheets API calls are wrapped in exponential
backoff for 429/5xx.

## Known weaknesses — the interesting ones for a reviewer
1. **Everything is sequential I/O with fixed sleeps.** No concurrency anywhere. The
   Global Visa run is ~70 min, overwhelmingly single-threaded HTTP waiting. The
   measured 429 rate suggests the sleeps are far more conservative than needed.
2. **JD fetch is inconsistent.** The India/Gulf feeds spawn a Chrome process per
   non-LinkedIn job while a proven browserless endpoint exists for LinkedIn.
   Naukri/Hirist/IIMJobs may have equivalents.
3. **Unparseable Claude output silently becomes Skip** (`result` is initialised to
   Skip), and Skip is written and permanently deduped. A malformed response therefore
   blackholes a job forever — contrast the deliberate `Error` path, designed to avoid
   exactly this.
4. **Dedup is URL-only.** The same role re-posted under a new LinkedIn ID is
   re-evaluated and re-paid for. Title+company dedup exists only within a run, in two
   feeds, implemented twice.
5. **Recency filtering is lenient by default.** Unparseable "posted" strings
   ("Recent", "N/A") return `True` from the window check, so undated scrapes always pass.
6. **Scraper fragility.** Selenium selectors are hardcoded CSS class fragments
   (`srp-jobtuple`, `dang-inner-html`); LinkedIn card parsing is a bare
   `except: continue`, so a markup change degrades to a silent zero rather than an error.
7. **Search coverage is capped.** LinkedIn stops near 1,000 results per query; the US
   needs a per-state split. Shorter `f_TPR` windows (r3600, r21600) appear not to be
   honoured, so there's no reliable way to slice by time.
8. **Sheet-as-database.** Dedup means reading one column of every tab on every run,
   forever. Growth is unbounded; the Skip tab also stores up to 45,000 chars of JD per row.
9. **Duplication across feeds.** Country lists, dedup keys, title filters and
   Claude-call plumbing are copy-pasted between three modules, now diverging.
10. **No tests in the repo.** Everything was verified ad hoc in a scratch directory and
    thrown away.
11. **Public repo.** The candidate profile is in the prompt in plain sight; secrets are
    Actions secrets, so not exposed, but run logs are public.
12. **Prompt/judgment quality.** ~60% of Gulf results land in Maybe against a 35%
    target, so the judgment carries little signal at the margin.

## Questions worth a second opinion
- Is a bounded thread pool (4–8 workers) for JD fetches safe against LinkedIn's rate
  limits, and what's the right backoff design if so?
- Should the deterministic Arabic-style override generalise into a rules layer that runs
  before the model, to cut cost and remove a class of model error?
- Is there a better dedup key than URL that survives re-posts without merging genuinely
  distinct openings?
- Is the Sheet the right store at this growth rate, or should state move to SQLite in the
  repo, or Actions cache, with the Sheet as presentation only?
- Given a 1,000-result ceiling per query, what's the best axis to shard searches on for
  full coverage?
