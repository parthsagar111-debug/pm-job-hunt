# PM Job Hunt — Hosted Setup (GitHub Actions + Google Sheets + ntfy)

## What this runs
| Workflow | Script | Searches | Sheet secret |
|---|---|---|---|
| **PM Eval — India Jobs** | `pm_eval_hosted.py` | India PM jobs — LinkedIn + Naukri + Hirist + IIMJobs, last 24h | `PM_EVAL_SPREADSHEET_ID` |
| **Gulf PM Eval** | `gulf_eval_hosted.py` | PM jobs in UAE, Saudi Arabia, Qatar, Bahrain, Oman, Kuwait — LinkedIn, last 24h or last week | `GULF_SPREADSHEET_ID` |
| **Global Visa PM** | `global_visa_hosted.py` | PM jobs worldwide (except India + Gulf) whose JD offers visa sponsorship — LinkedIn, last 24h | `GLOBAL_VISA_SPREADSHEET_ID` |

Both are triggered externally by cron-job.org calling GitHub's `workflow_dispatch`
API (not GitHub's own built-in cron). PM Eval currently runs every 30 minutes.
Results → Google Sheets. Summary → ntfy push notification on your phone.

The Gulf feed replaces the old worldwide "LinkedIn Global" feed. GCC employers
sponsor the work visa as standard, so it doesn't check each job for visa
support — instead it skips roles restricted to local nationals
(Emiratisation/Saudization) or requiring Arabic.

**Global Visa PM** gives no Apply/Maybe/Skip judgment. It searches ~128 countries
(US split by state when LinkedIn's ~1000-result cap bites), reads each JD from
LinkedIn's guest endpoint, and keeps a job only when Claude Haiku confirms the
employer offers visa sponsorship — `YES`, or `CONDITIONAL` for "may be available".
Relocation support alone doesn't qualify. Only the visa-related lines of the JD
are sent to Claude, which keeps a full run at roughly $0.08. A 24h sweep takes
~2h15m and yields ~10–15 sponsoring roles a day.

---

## One-time setup

### Step 1 — ntfy app
1. Install **ntfy** on your phone (free, iOS + Android)
2. Subscribe to any topic name you choose — e.g. `parth-pm-jobs`
3. Save the topic name for the `NTFY_TOPIC` secret

### Step 2 — Google Cloud service account
1. Go to https://console.cloud.google.com/ → create or select a project
2. Search for **Google Sheets API** → Enable
3. **APIs & Services → Credentials → Create Credentials → Service Account** (no role needed)
4. Open the service account → **Keys** tab → **Add Key → JSON** — keep the file safe
5. Copy the `client_email` from the JSON (looks like `pm-job-bot@project.iam.gserviceaccount.com`)

### Step 3 — Google Sheets
1. Create one Google Sheet per feed, e.g. "PM Job Eval" and "Gulf PM Eval"
2. Share each with the service account email → Editor access
3. Copy each Sheet's ID from its URL: `https://docs.google.com/spreadsheets/d/THIS_IS_THE_ID/edit`

The Apply / Maybe / Skip tabs and their headers are created automatically on the first run.

### Step 4 — GitHub secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret name                   | Value                                       |
|-------------------------------|---------------------------------------------|
| `ANTHROPIC_API_KEY`           | Your Claude API key                         |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full contents of the downloaded JSON file   |
| `PM_EVAL_SPREADSHEET_ID`      | Sheet ID for "PM Job Eval"                  |
| `GULF_SPREADSHEET_ID`         | Sheet ID for "Gulf PM Eval"                 |
| `GLOBAL_VISA_SPREADSHEET_ID`  | Sheet ID for "Global Visa PM"               |
| `NTFY_TOPIC`                  | Your ntfy topic name (e.g. `parth-pm-jobs`) |

---

## Running
- Repo → **Actions** → pick a workflow → **Run workflow**
- **Gulf PM Eval** has a **time_range** dropdown:
  - `24h` (default) — what scheduled runs use
  - `week` — one-off 7-day backfill; takes much longer (up to ~2 hours) because it
    evaluates every new listing from the past week
- Scheduled calls from cron-job.org don't pass inputs, so they get `24h`.

### Scheduling Gulf PM Eval on cron-job.org
Same as the PM Eval job, but POST to the `gulf_eval.yml` workflow:
```
POST https://api.github.com/repos/parthsagar111-debug/pm-job-hunt/actions/workflows/gulf_eval.yml/dispatches
Body: {"ref": "master"}
```
(To schedule a week run instead, the body would be `{"ref": "master", "inputs": {"time_range": "week"}}`.)
Every few hours is plenty — the Gulf market posts far fewer PM roles than India.

---

## What you'll see
**ntfy** (after each run):
```
Gulf PM Eval — run complete
Apply: 3  |  Maybe: 11  |  Skip: 8
Check your Google Sheet for details.
```
High priority if there are any Apply results.

**Google Sheets** (3 tabs): Apply · Maybe · Skip.
Each row: Month, Date Found, Title, Company, Location, Source, Decision, Reason, Gap, URL, JD.

---

## GitHub Actions minutes
The repo is public, so standard GitHub-hosted runners are free with no monthly
minute cap. If the repo is ever made private, the free tier is 2,000 min/month —
PM Eval at every 30 minutes (~4–5 min per run) alone uses ~7,000, so the
cron-job.org schedules would need widening.
