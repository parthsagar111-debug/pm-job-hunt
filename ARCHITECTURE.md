# pm-job-hunt — architecture, data flow and cost

A job-discovery pipeline that scrapes product-management roles from Indian and
international job boards, classifies each one against a fixed candidate profile, and
writes the result to Google Sheets. It runs entirely on free infrastructure; the only
money spent is LLM inference.

Written for external review. Current as of 2026-10-27. Every number in the cost and
rate-limit sections is measured from production runs, not estimated — the measurement
is named next to each figure.

---

## 1. What problem it solves

Manually scanning job boards for PM roles is high-volume, low-yield work. The pipeline
collects roughly **170 new postings a day** across four sources and sorts them into
three buckets — Apply / Maybe / Skip — so only ~10% need a human look.

Scale, measured over a 7-day window (1,191 classified jobs, 2026-09-30 → 10-07):

| | |
|---|---|
| New postings classified per day | ~170 |
| Reaching **Apply** | ~11% |
| Reaching **Maybe** | ~51% |
| Reaching **Skip** | ~38% |
| Cost per job | **$0.0029** |
| Cost per month | **~$15** |

---

## 2. Constraints that shaped the design

These are the non-obvious ones. Several design choices look wrong without them.

1. **No server, no persistent host.** Everything runs in GitHub Actions on ephemeral
   runners. Nothing can be cached on disk between runs; there is no local database.
2. **Google Sheets is the only state store.** Chosen because the user reads and edits
   the output there daily. SQLite was rejected: there is nowhere to keep the file.
   This single decision drives the dedup design (§5.2) and the write path (§7).
3. **The repository is public.** The candidate's career history cannot be committed.
   It reaches the runtime as a GitHub secret (§8).
4. **Scheduling is external.** GitHub's own `schedule:` is not used; cron-job.org calls
   the `workflow_dispatch` API. (Historical reason: GitHub's scheduler is best-effort
   and was skipping runs.)
5. **LinkedIn rate-limits aggressively and is the primary source.** Measured ceiling is
   ~1 request/second per IP, and it is a *rate* limit, not a concurrency limit — see
   §9. An async rewrite was scoped and rejected on this evidence.
6. **The LLM is the only paid component.** Scraping, storage, scheduling, compute and
   notifications are all free tiers.

---

## 3. System overview

```
cron-job.org (every 30 min)          manual dispatch           manual dispatch
        │                                   │                         │
        ▼                                   ▼                         ▼
┌───────────────────┐          ┌──────────────────────┐   ┌──────────────────────┐
│  PM Eval (India)  │          │   Gulf PM Eval       │   │   Global Visa PM     │
│  pm_eval_hosted   │          │   gulf_eval_hosted   │   │   global_visa_hosted │
│  window: 24h      │          │   window: 7 days     │   │   window: 7 days     │
└─────────┬─────────┘          └──────────┬───────────┘   └──────────┬───────────┘
          │                               │                          │
          └───────────────┬───────────────┴──────────────────────────┘
                          ▼
         ┌────────────────────────────────────────┐
         │  core_eval_hosted.py  (1,719 lines)    │
         │  scrapers · JD fetch · rate limiter    │
         │  JobCollector · LLM feature extraction │
         └───────┬────────────────────────┬───────┘
                 ▼                        ▼
      ┌────────────────────┐   ┌──────────────────────┐
      │ decision_rules.py  │   │ sheets_writer.py     │
      │ pure policy, no I/O│   │ Google Sheets writes │
      └────────────────────┘   └──────────────────────┘
                                          │
                                          ▼
                              Google Sheets (3 workbooks)
                                          │
                                          ▼
                                   ntfy push notification
```

### Repository inventory

| File | Lines | Role |
|---|---|---|
| `core_eval_hosted.py` | 1,719 | Shared engine: scrapers, JD fetch, rate limiter, LLM calls, collector |
| `global_visa_hosted.py` | 385 | Global visa-sponsorship feed |
| `sheets_writer.py` | 354 | All Google Sheets reads and writes |
| `gulf_eval_hosted.py` | 291 | Gulf feed |
| `decision_rules.py` | 243 | **Pure** Apply/Maybe/Skip policy — no I/O, no API |
| `pm_eval_hosted.py` | 190 | India feed |
| `sponsor_registry.py` | 178 | UK/NL government sponsor registers |
| `scripts/eval_regression.py` | 177 | Re-runs the classifier on stored JDs; writes nothing |
| `scripts/reclassify_existing.py` | 144 | Re-classifies existing sheet rows, moves them between tabs |
| `candidate_profile.py` | 67 | Loads the profile from a secret; contains no personal data |
| `ntfy_notify.py` | 58 | Push notification after a run |
| `tests/` (18 files) | — | **397 tests**, all offline, no network or API |

---

## 4. The three feeds

| | **PM Eval (India)** | **Gulf PM Eval** | **Global Visa PM** |
|---|---|---|---|
| Trigger | cron-job.org, every 30 min | manual | manual |
| Window | 24 hours | 7 days | 7 days |
| Sources | LinkedIn (9 keyword searches), Naukri, Hirist, IIMJobs | LinkedIn, 6 Gulf countries | LinkedIn, 12 countries |
| Classifier | full feature extraction + rule engine | prompt returns a decision directly | visa-sponsorship check only |
| Output tabs | Apply / Maybe / Skip | Apply / Maybe / Skip | Listings / No Sponsorship |
| Volume | ~170 new jobs/day | ~77 per weekly run | ~1,080 per weekly run |
| Runtime | 3–18 min | ~73 min | 5 h (hits its own cap) |

The three share `core_eval_hosted.py` but have deliberately different classifiers.
**Only PM Eval uses the features-plus-rules design** described in §6; Gulf still asks
the model for a decision directly, and Global Visa only asks "does this JD offer visa
sponsorship, yes/conditional/no, and quote the evidence".

*Reviewer note:* this inconsistency is intentional but unresolved — see §13.

---

## 5. Pipeline, step by step

### 5.1 Collect — input: a keyword + a location + a time window; output: job stubs

LinkedIn is read through two logged-out guest endpoints, with no browser:

```
https://www.linkedin.com/jobs/search/?keywords=…&location=…&f_TPR=r86400&sortBy=DD
https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?…&start=N
```

The search page returns ~60 results; pagination through the second endpoint is used
only for 7-day windows. Requests go through `curl_cffi` with `impersonate="chrome"`
for TLS fingerprinting — plain `requests`/`httpx` are blocked.

Naukri, Hirist and IIMJobs listings need Selenium (headless Chrome, installed per run).

A job stub is `{job_id, title, company, location, posted, source, url}`.

### 5.2 Filter and dedup — `JobCollector`

Every job passes through one shared gate, which counts *why* anything is dropped (a
silent filter is how you end up wondering where the listings went):

| Check | Basis |
|---|---|
| Seen this run | in-memory job id set |
| Off-region | location predicate (Global Visa excludes India + Gulf) |
| Wrong title | `is_pm_eval_role()` — word-boundary PM title match |
| Duplicate | fingerprint of company + title + location |
| Already in the Sheet | **URL**, read from the output tabs at startup |
| Company cap | max 2 roles per company **per run** |

**Dedup is URL-based and reads the Sheet.** This is the load-bearing decision of the
whole design: it happens *before* any JD fetch or LLM call, so already-seen jobs cost
nothing. On a typical run 169 of 187 collected jobs are dropped here.

A content-hash dedup key was built and reverted — see §13.

### 5.3 Fetch the JD — input: a job stub; output: plain text

| Source | Method |
|---|---|
| LinkedIn | guest endpoint `jobs-guest/jobs/api/jobPosting/{id}`, no browser |
| IIMJobs / Hirist | `gladiator.{iimjobs.com,hirist.tech}/job/detail?jobcode=N` — the JSON API the page itself calls |
| Naukri | Selenium + CSS selectors |

The Info Edge (IIMJobs/Hirist) path is worth a reviewer's attention. Those pages are
Next.js shells: the description is **not in the served HTML at all**. Scraping the
rendered DOM returned the category navigation menu, and on 2026-09-30 all 39
IIMJobs/Hirist JDs in one run were garbage as a result. The fix calls the same endpoint
the page calls — no auth, no cookie, no browser — and also recovers the real employer
name. Those two sites share a backend but have **separate id spaces**: the same code on
the wrong host returns a different job with HTTP 200.

`is_garbage_jd()` then applies a local check that overrides anything the model says:
under 300 characters, or matching a known category-menu signature, means we read
nothing.

### 5.4 Extract features — input: profile + JD; output: a 12-field JSON object

One Anthropic Messages API call per job, using a **forced tool call**
(`tool_choice: {"type": "tool"}`) so the reply must match the schema. Model is
`claude-haiku-4-5`, `temperature=0`.

| Field | Type |
|---|---|
| `is_pm_role` | boolean |
| `title_level` | enum: intern / apm / pm / senior / lead_principal / vp_plus |
| `years_min`, `years_max` | int or null — **only** from numbers the JD states |
| `domain_class` | enum: core / adjacent / non_core |
| `hard_blockers` | array of 9 enum values |
| `requires_managing_pms` | int or null |
| `stated_max_ctc_lpa` | int or null |
| `jd_quality` | enum: ok / thin / garbage |
| `fit_score` | 0–100 |
| `reason`, `gap` | short free text, for the sheet |

The prompt carries the candidate profile, domain definitions, the blocker enum and
three few-shot examples. It contains **no distribution quota and no "when in doubt"
guidance** — both were removed as causes of misclassification.

One feature is computed in code, never asked of the model: `title_is_junior`, a regex
over the scraped title. See §6.

### 5.5 Decide — input: the feature dict; output: (decision, reason prefix)

`decision_rules.decide()` is a pure function. Rules are ordered; first match wins.

| # | Rule | Outcome |
|---|---|---|
| 0 | Published title says associate/assistant/APM/junior/trainee/intern | **Skip** |
| 1 | `jd_quality == "garbage"` | **Maybe** + `UNVERIFIED JD` |
| 2 | `is_pm_role` false | **Skip** |
| 3 | A *categorical* blocker present | **Skip** |
| 4 | `title_level == "intern"` | **Skip** |
| 5 | Stated max CTC below ₹15 LPA | **Skip** |
| 6 | Stated years ceiling ≤ 2; or == 3 and not core; or 4–5 and not core | **Skip** / Maybe |
| 7 | Over-senior (VP+, 13+ years, or managing 4+ PMs) in a non-core domain | **Skip**, else Maybe |
| 8 | Fit: core ≥ 65 → Apply; adjacent ≥ 80 → Apply; non-core < 35 → Skip | Apply / Maybe / Skip |

Then caps and notes are applied: `apm` caps at Maybe, a thin JD and advisory blockers
add visible prefixes (`THIN JD —`, `JUNIOR TITLE —`, `CHECK REQS —`).

**Blockers are split by how reliable they proved to be:**

```python
CATEGORICAL_BLOCKERS = {          # reject on these
    "deep_infra_security_networking", "clinical_or_payer_healthcare_ops",
    "manufacturing_erp_industrial", "specialist_hardware",
}
ADVISORY_BLOCKERS = {             # note only, never a rejection
    "mandatory_cs_or_engineering_degree", "other_mandatory_degree",
    "mandatory_platform_certification", "pure_supply_chain_logistics_saas",
    "post_trade_aml_compliance_accounting",
}
```

The split is empirical. Across 1,705 classified jobs the three categorical domain
blockers fired 228 times and **every single one was also judged non-core** — when the
model sees that depth it sees an unrelated job too, and that pairing is what makes the
rejection safe. The advisory ones failed that test (§6).

### 5.6 Write — `sheets_writer.save_eval_jobs`

Rows are built as `{header: value}` and positioned against the tab's **live row 1**:

```python
_align_rows(rows, _live_headers(ws, HEADERS_EVAL), tab)
```

This is not defensive style for its own sake. `append_rows` writes by position; a
12-value row was once appended to a 13-column sheet and 166 content hashes overwrote a
column owned by a different tool. Columns this module does not own are left blank, and
a value with no matching column is reported rather than shifted.

### 5.7 Notify

An ntfy push with the Apply/Maybe/Skip counts. Free, no account.

---

## 6. The central design decision: the model extracts, the code decides

This is the part most worth reviewing, because it was arrived at by measurement rather
than preference.

**Original design:** one LLM call per job returned the decision itself.

**What an audit of 461 classified jobs found:** 81 of them — **17.6%** — were in the
wrong tab. Five causes: a hardcoded profile omitting most of the candidate's
experience (so the model invented gaps), a distribution quota in the prompt (~20/35/45)
plus two "when in doubt pick Maybe" lines, garbage JDs classified as if real, parse
failures silently becoming Skip, and no temperature pinning (identical postings landed
in different tabs on different runs).

**The reframing:** the model is good at reading a JD and inconsistent at applying a
policy. So it now only extracts features, and `decide()` applies the policy. Every
threshold lives in one `CONFIG` dict, the function is pure, and 397 tests pin the
branches — changing a number shows up as a test diff instead of jobs quietly moving.

### Evidence that model labels cannot be trusted as decisions

The cleanest case. `Product Owner @ Flexiple` was evaluated ten times across ten days:

```
apm / fit 45 / yrs 3-6   ×5  → Skip
pm  / fit 62 / yrs 4-8   ×5  → Maybe
```

The same role, read two ways, with a hard rule converting that coin-flip into a tab
difference. Also observed: `Lead Product Manager @ Mastercard` landing in Skip, Skip,
Apply and Maybe across four sightings; `Senior Manager @ American Express` swinging 37
fit points between runs.

The response was not to trust the model less in general, but to stop letting any single
unreliable label reject a job outright:

| Signal | Was | Now | Why |
|---|---|---|---|
| `mandatory_cs_or_engineering_degree` | hard Skip | advisory note | 82 of 265 Skips in one run; sole cause of 60. Of 40 live PM JDs sampled, **zero** stated a CS degree as a hard requirement — all 9 that mentioned one said "or related field", "Business, Computer Science, or related", "or equivalent" or "preferred" |
| `title_level == "apm"` | hard Skip | caps at Maybe; **published title** decides rejection | Of 41 jobs labelled apm, only 29 had a junior title; the other 12 were ordinary PM titles where the model read "Associate Product Manager" out of the JD body |
| non-core fit floor | 50 | 35 | 50 sat between two adjacent steps of the model's own fit vocabulary (…35, 42, 45, 48, 50, 62…), and the same role has swung 37 points |
| `post_trade_aml_compliance_accounting` | hard Skip | advisory note | 7 of its 34 appearances were core/adjacent, and all 7 were payments roles in the candidate's own domain. Every payments JD mentions compliance |
| `jd_quality == "thin"` | treated as unread | judged normally, flagged | Thin JDs averaged 2,000–8,000 chars; a 45-job live sample had none under 1,200, and there were zero fetch failures. It also held a Meta WhatsApp PM role (core, fit 65) out of Apply |
| `is_pm_role == false` + high fit | overridden to Maybe | rejection stands | `fit_score` answers "does the profile match", not "is this product management". Both cases the override fired on were correct rejections |

Net effect, replaying logged features over 1,191 jobs: Skip 622 → 446, Apply 106 → 127,
with nothing leaving Apply.

---

## 7. Data stores and schemas

Three Google Sheets workbooks, one per feed, each shared with a service account.

**PM Eval and Gulf** — tabs `Apply`, `Maybe`, `Skip`:

```
Month | Date Found | Title | Company | Location | Source | Decision | Reason | Gap | URL | JD
```

The PM Eval workbook also carries two columns owned by a separate tool
(`Resume Match Score`, `Resume Decision`) after column K. This pipeline must not write
them — hence §5.6.

**Global Visa** — tabs `Listings`, `No Sponsorship`:

```
Month | Date Found | Visa | Evidence | Title | Company | Location | Posted | Source | URL | JD | Sponsor licence
```

`Sponsor licence` is a company-level signal from the UK Register of Licensed Sponsors
(~121,000 Skilled Worker sponsors, free daily CSV) and the Dutch IND public register.
It is deliberately a column and never part of a decision: a licence means the employer
*can* sponsor, not that it will for this role.

JD text is stored (capped at 45,000 chars/cell, Sheets' limit is 50,000). This is what
makes rows re-classifiable later without re-scraping — postings expire.

---

## 8. Secrets and security posture

The repository is public, including its Actions logs.

| Secret | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | LLM inference |
| `CANDIDATE_PROFILE` | the candidate's career history, rendered from a private repo |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Sheets access |
| `PM_EVAL_SPREADSHEET_ID`, `GULF_SPREADSHEET_ID`, `GLOBAL_VISA_SPREADSHEET_ID` | workbook ids (secret so the sheets aren't discoverable) |
| `NTFY_TOPIC` | push topic |

Rules the code follows, with tests enforcing them:

- The profile is **never** printed or logged. Per-job feature JSON is fine; the prompt
  is not. `candidate_profile.py` holds no personal data and fails fast if the secret is
  absent or under 200 characters, rather than classifying against nothing.
- API error bodies are logged (they name the cause) but tested to never echo the prompt.
- Sheet ids are secrets, so the workbooks are not referenced in committed files.

---

## 9. LinkedIn rate limiting

Measured, not assumed. A probe varying pacing and concurrency showed the constraint is
**request rate per IP, not concurrency**:

| Pacing | Blocked |
|---|---|
| 0.7 req/s | 0 of 25 |
| 1.0 req/s | 0 of 38 |
| 1.5 req/s | 27 of 60 |
| 2.5 req/s | 46 of 60 |
| 4–8 concurrent, unpaced | 25 of 40 |

So the client is a token bucket with adaptive backoff: halve on a 429, floor at 0.25
req/s, creep back by 25% after 20 clean requests.

**Pacing differs by feed, also from measurement.** A single-location feed sustains the
full rate; multi-location sweeps do not:

| Feed | Locations | Observed |
|---|---|---|
| PM Eval | 1 | 0–1 rate limits per run across 120–683 requests, at 1.0 req/s |
| Gulf | 10 | 82 of 217 requests limited, pinned at the 0.25 floor |
| Global Visa | 12 | 343 of 907 limited, pinned at the floor |

Each 429 costs a retry wait of 30s or more, so Global Visa spent roughly **2.9 of its 5
hours being punished** and still classified only 412 of 1,080 jobs. The sweeps therefore
start at `LI_SWEEP_RATE = 0.5`: those same 907 requests at a steady 0.5 req/s are about
30 minutes of pacing. PM Eval is left at 1.0.

---

## 10. Money — where it goes and how

### Pricing

`claude-haiku-4-5`, USD per million tokens:

| | |
|---|---|
| Input | $1.00 |
| Output | $5.00 |
| Cache write | $1.25 (1.25×) |
| Cache read | $0.10 (0.1×) |

### Measured breakdown

From a 515-job run: 515 calls, 504,270 fresh input tokens, 2,394,750 cache-read tokens,
147,610 output tokens, **$1.4818 total — $0.00288 per job**.

| Component | Share | Per job |
|---|---|---|
| **Output** — the features JSON | **49.8%** | 286 tokens at 5× the input price |
| **Fresh input** — the JD excerpt | 34.0% | 979 tokens |
| Cached prefix — profile, definitions, few-shots | 16.2% | 4,650 tokens at 0.1× |

Output being half the bill is counterintuitive and mostly *structure* rather than
prose: twelve field names and values on every call. `reason` is capped at 10 words and
`gap` at 6.

### Monthly total

~170 jobs/day × $0.0029 ≈ **$0.49/day, ~$15/month.** Everything else — GitHub Actions,
Google Sheets, cron-job.org, ntfy, the sponsor registers — is free.

### Savings already implemented

| Measure | Measured saving |
|---|---|
| **URL dedup before any spend** | ~90% of collected jobs are dropped before a JD fetch or an API call (169 of 187 on a typical run) |
| **Prompt caching** on the static prefix | **60%** — a 515-job run cost $1.48 against $3.64 uncached. Verified working across runs: ten consecutive runs 30 min apart all show `cache write 0`, so the 1-hour TTL holds and the prefix is written once, not per run |
| Company cap, 2 per run | 13 jobs dropped in one day |
| Title gate before collection | ~15 non-PM rows per 1,700 never reach a call |
| JD truncation at 10,000 chars | caps the long tail; p90 is 7,706 |
| Haiku rather than a larger model | ~12× cheaper than Sonnet-class for this task |
| Abort after 3 consecutive API errors | prevents a dead key burning a whole batch |

### Levers identified and not taken

| Lever | Saving | Why not |
|---|---|---|
| Skip the API call for title-rejected jobs | ~5% | **worth doing** — `title_is_junior` needs no JD, yet 5% of calls are spent then rejected on it |
| Drop `stated_max_ctc_lpa` | ~2% | **worth doing** — non-null in 0 of 39 recent jobs, ~3 of 1,191 in a week; a required output field for a rule that never fires |
| Tighten `reason` to 6 words | ~3% | 46% of replies already exceed the stated 10-word cap |
| Trim the JD cap to 4,000 chars | ~9% | requirements sometimes appear late in a JD |
| Batch several jobs per call | ~13% | the cached prefix is charged per *call*. But the schema becomes an array and one malformed reply loses a whole batch instead of one job |
| Two-stage cheap pre-filter | up to ~35% | **deliberately rejected.** This reintroduces the exact failure mode §6 removed: an unreliable signal deciding which jobs deserve a real look |

Run frequency is **not** a lever: dedup means the same ~170 new jobs get classified
whether that is across 48 runs or 4.

---

## 11. Failure modes and how they are handled

| Failure | Handling |
|---|---|
| LLM returns a malformed reply | `ClaudeSchemaError` → decision `"Error"` → row filtered out, **never written**, retried next run. A parse failure must never become a Skip, because a Skip is written and deduped forever |
| API key dead or out of credit | `preflight_api_check()` — one `max_tokens=1` call before any scraping, so a dead key costs seconds instead of a full scrape. Error bodies are surfaced verbatim |
| 3 consecutive API errors | abort the batch, save what completed, exit non-zero |
| JD fetch returns nothing | row goes down the UNVERIFIED path, never a Skip |
| A source's selectors break | per-source JD-quality summary each run, flagged when ≥50% of a source's JDs are unusable **and** the source contributed ≥5 jobs (the sample guard stops it crying wolf on a 1-job run) |
| Sheet read fails at startup | abort rather than risk re-evaluating everything at full cost |
| Global Visa runs long | stops starting new countries at 2.5 h, hard stop at 5 h, always reaches the save step |
| Rate limiting | adaptive backoff, per-feed starting rate (§9) |

---

## 12. Testing

**397 tests, all offline** — no network, no API, no Sheets. CI runs them on every push.

What they pin, beyond the usual:

- Every branch of `decide()`, using **the actual feature dicts from rows that went to
  the wrong tab** — so a regression shows up as a diff rather than as jobs quietly
  moving. Named cases include the Zepto, Ferns N Petals, Flexiple, Meta WhatsApp and
  Growth Manager rows.
- `VALID_BLOCKERS == CATEGORICAL_BLOCKERS | ADVISORY_BLOCKERS`, so a new enum cannot be
  added without being sorted into one bucket.
- Each feed's default time window in *both* the workflow YAML and the script, and that
  the two agree — a mismatch is silent in both directions.
- That the API-error message never echoes the prompt.
- That `preflight_api_check` runs before dedup or any scrape.
- That the Info Edge JD fetch starts no browser, and routes by the job's own host.

`scripts/eval_regression.py` is a separate harness: it re-runs the classifier against
JDs **already stored in the sheet** and reports agreement with the recorded decisions.
It writes nothing. This is the tool for validating a prompt, model or provider change
before trusting it.

---

## 13. Open questions for the reviewer

Listed deliberately — these are the things I would most like challenged.

1. **Three feeds, three classifier designs.** Only PM Eval uses features-plus-rules.
   Gulf still asks the model for a decision directly, which is the design that produced
   a 17.6% error rate in PM Eval. Should Gulf be migrated, or is the evidence not
   transferable?
2. **Sheets as the only state store.** It forces URL-only dedup, a full-column read at
   every run's start, and write-time header realignment. Is there a free, zero-ops
   alternative that does not reintroduce a server?
3. **Content-hash dedup was reverted.** A `sha1(title + company + first 1500 chars of
   JD)` key was added to catch reposts under new URLs. It dropped 168 genuinely new jobs
   in one run — large employers repost constantly — and its column collided with another
   tool's. Reposts therefore go undetected. Is there a sound approach here?
4. **Per-run company cap.** The cap is 2 *per run*, and there are up to 48 runs a day, so
   one persistent poster contributed 5 rows in a day and every one had an unusable JD.
   A per-day cap needs cross-run state. Worth it, or treat the symptom differently?
5. **Output is 50% of inference cost** and mostly JSON structure. Is there a materially
   cheaper encoding that keeps schema enforcement?
6. **`fit_score` is quantized and unstable** — 17 discrete values, and the same role has
   swung 37 points between runs. Thresholds on it are therefore soft. Is a score the
   right abstraction, or should domain distance and seniority alone drive the decision?
7. **Global Visa cannot complete a 7-day window** in one run: 412 of 1,080 jobs in 5
   hours, with 2 of 12 countries lost to throttling. Needs either per-location page caps
   or a resumable design — the 668 unclassified jobs were never written, so dedup does
   not remember them and the next run re-collects them.
8. **No idempotency across a crash.** Rows are written once at the end of a run. A
   crash mid-run loses the whole run's classification work, which for Global Visa is
   hours of paid inference.

---

## 14. Things a reader should not misread as accidents

- `is_pm_role` (the original title gate) is **pinned** for Gulf and Global Visa while
  PM Eval uses a tightened `is_pm_eval_role`. Both exist on purpose.
- `TECHNICAL_BRIEF.md` in this repository predates the classifier overhaul
  (2026-09-18) and is **stale**. This document supersedes it.
- The duplicate cron-job.org entry for PM Eval is intentional: weekdays every 30
  minutes, weekends twice a day.
- `temperature=0` is passed only by PM Eval. Gulf and Global Visa keep the API default
  they were tuned against.
