# PM Eval — Hosted Setup (GitHub Actions + Google Sheets + ntfy)

## What this runs
- **pm_eval_hosted.py** — India PM jobs (LinkedIn + Naukri + Hirist + IIMJobs)

Triggered externally by a cron-job.org scheduled job calling GitHub's
`workflow_dispatch` API (not GitHub's own built-in cron), currently every 30
minutes. Results → Google Sheets. Summary → ntfy push notification on your phone.

---

## One-time setup: 4 steps

### Step 1 — ntfy app
1. Install **ntfy** on your phone (free, iOS + Android)
2. Subscribe to any topic name you choose — e.g. `parth-pm-jobs`
   (just tap the + button in the app and type the topic name)
3. Save the topic name — you'll paste it into GitHub Secrets shortly

### Step 2 — Google Cloud service account
1. Go to https://console.cloud.google.com/ → create or select a project
2. Search for **Google Sheets API** → Enable
3. Go to **APIs & Services → Credentials → Create Credentials → Service Account**
   Give it any name (e.g. `pm-job-bot`). No project role needed.
4. Open the service account → **Keys** tab → **Add Key → JSON**
   This downloads a `.json` file — keep it safe, it's your credential.
5. Copy the `client_email` from the JSON (looks like `pm-job-bot@project.iam.gserviceaccount.com`)

### Step 3 — Google Sheet
1. Create a Google Sheet: "PM Job Eval"
2. Share it with the service account email → Editor access
3. Copy the Sheet's ID from its URL:
   `https://docs.google.com/spreadsheets/d/THIS_IS_THE_ID/edit`

### Step 4 — GitHub repo + secrets
1. Create a new **private** GitHub repo (e.g. `pm-job-hunt`)
2. Push all files in this folder to it
3. Go to repo → **Settings → Secrets and variables → Actions → New repository secret**
   Add these secrets:

   | Secret name                  | Value                                      |
   |------------------------------|--------------------------------------------|
   | `ANTHROPIC_API_KEY`          | Your Claude API key                        |
   | `GOOGLE_SERVICE_ACCOUNT_JSON`| Full contents of the downloaded JSON file  |
   | `PM_EVAL_SPREADSHEET_ID`     | Sheet ID for "PM Job Eval"                 |
   | `NTFY_TOPIC`                 | Your ntfy topic name (e.g. `parth-pm-jobs`)|

---

## First run
After pushing and adding secrets:
- Go to your repo → **Actions** tab
- You'll see "PM Eval — India Jobs"
- Click it → **Run workflow** (top right) to trigger a manual test run
- Watch the logs — if it completes green, you're live

After that, it runs automatically on the cron-job.org schedule.

---

## What you'll see
**In the ntfy app** (after each run):
```
🗂 PM Eval — run complete
✅ Apply: 3  |  🤔 Maybe: 11  |  ❌ Skip: 8
Check your Google Sheet for details.
```
Notification is marked high priority if there are any Apply results.

**In Google Sheets** (3 tabs):
- Apply — jobs Claude recommends applying to
- Maybe — worth a look, some gap
- Skip — filtered out (still logged)

Each row has: Month, Date Found, Title, Company, Location, Source, Decision, Reason, Gap, URL

Filter by Month column to see only this month's results.

---

## GitHub Actions free tier limits
Free tier: 2,000 minutes/month. Each run takes roughly 4-6 minutes.
At every-30-min cadence (48 runs/day) × ~5 min avg = ~240 min/day = ~7,200 min/month.

**That exceeds the free tier.** If minutes become an issue, widen the cron-job.org
schedule (e.g. hourly instead of every 30 min) rather than editing
`pm_eval.yml` directly — the schedule lives in cron-job.org, not in the workflow file.
