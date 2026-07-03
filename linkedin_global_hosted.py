"""
linkedin_global_hosted.py — LinkedIn Global PM Jobs (hosted / GitHub Actions)
==============================================================================
Runs ONCE and exits. GitHub Actions handles scheduling (every 2 hours).

Output:   Google Sheet (Apply / Maybe / Skip tabs)
Notify:   ntfy push notification after run

Required environment variables (set as GitHub Actions secrets):
  ANTHROPIC_API_KEY             — Claude API key
  GOOGLE_SERVICE_ACCOUNT_JSON   — full JSON contents of service account key
  GLOBAL_SPREADSHEET_ID         — Google Sheet ID for this script's output
  NTFY_TOPIC                    — your ntfy topic name
"""

import os, sys, re, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# curl_cffi impersonates Chrome's TLS fingerprint — prevents LinkedIn 429s
try:
    from curl_cffi import requests as curl_requests
    _LI_GET = lambda url, **kw: curl_requests.get(url, impersonate="chrome", **kw)
except ImportError:
    _LI_GET = requests.get

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

from bs4 import BeautifulSoup
from datetime import datetime

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOP_N    = 50
SPREADSHEET_ID = os.environ.get("GLOBAL_SPREADSHEET_ID", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

PM_KEYWORDS = [
    "product manager", "product management",
    "senior pm", "group pm", "principal pm", "staff pm",
    "lead pm", "growth pm", "technical pm", " pm ", "(pm)",
]

# Titles that should be excluded even if they contain PM keywords
PM_EXCLUDE = [
    "director of product", "head of product", "vp of product",
    "chief product", "product lead", "product director",
    "vp product", "vice president product",
    "intern", "internship", "graduate", "entry level",
]

def is_pm_role(title: str) -> bool:
    t = title.lower().strip()
    if any(ex in t for ex in PM_EXCLUDE):
        return False
    return any(kw in t for kw in PM_KEYWORDS)

# LinkedIn searches with country-specific geoIds
# geoId reference:
#   103644278 = United States
#   101165590 = United Kingdom
#   101282230 = Germany
#   106057199 = UAE
#   102454443 = Singapore
#   101452733 = Canada
#   101620260 = Australia
#   90009987  = Netherlands
#   100025096 = Sweden
#   100565514 = Switzerland
# f_TPR: r86400=24h, r604800=week


# ─────────────────────────────────────────────
# SELENIUM DRIVER
# ─────────────────────────────────────────────
def make_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument(f"user-agent={HEADERS['User-Agent']}")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    svc    = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=svc, options=opts)
    driver.execute_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    return driver


# ─────────────────────────────────────────────
# RELOCATE.ME SCRAPER
# ─────────────────────────────────────────────
RELOCATE_URLS = [
    "https://relocate.me/international-jobs/product-manager",
    "https://relocate.me/search/product-manager",
]

def fetch_relocateme() -> list:
    """Scrape Relocate.me for PM jobs — all have relocation/visa by definition."""
    print(f"  [Relocate.me] launching Chrome...")
    driver, hits, seen = None, [], set()
    try:
        driver = make_driver()
        for url in RELOCATE_URLS:
            print(f"  [Relocate.me] {url.split('/')[-1]}...")
            driver.get(url)
            time.sleep(5)
            # Scroll to load all jobs
            for _ in range(6):
                driver.execute_script("window.scrollBy(0, 800)")
                time.sleep(1)
            time.sleep(2)

            soup = BeautifulSoup(driver.page_source, "html.parser")

            # Find job links — format: /country/city/company/job-slug
            job_links = [
                a for a in soup.find_all("a", href=True)
                if re.match(r"^/[a-z]{2,}/", a.get("href", ""))
                and a.get("href", "").count("/") >= 4
                and "relocate.me" not in a.get("href", "")
            ]

            print(f"    Found {len(job_links)} job links")

            for link in job_links:
                href = link.get("href", "")
                if not href: continue
                full_url = "https://relocate.me" + href if href.startswith("/") else href
                clean_url = full_url.split("?")[0].rstrip("/")
                if clean_url in seen: continue
                seen.add(clean_url)

                # Extract title
                heading = link.find(["h2", "h3", "h4", "h1"])
                title = heading.get_text(strip=True) if heading else link.get_text(strip=True)
                title = " ".join(title.split())
                if not title or len(title) < 3: continue
                if not is_pm_role(title): continue

                # Extract company and location from card
                card = (link.find_parent("article") or
                        link.find_parent("li") or
                        link.find_parent("div"))
                company = "—"
                location = "—"
                if card:
                    # Company usually in a span or p near the title
                    for tag in card.find_all(["span", "p", "div"]):
                        txt = tag.get_text(strip=True)
                        if 2 < len(txt) < 50 and txt != title:
                            company = txt; break
                    # Location from URL slug: /country/city/company/job
                    parts = href.strip("/").split("/")
                    if len(parts) >= 2:
                        location = parts[1].replace("-", " ").title()

                job_id = "rm_" + clean_url.split("/")[-1][:40]

                hits.append({
                    "job_id":             job_id,
                    "title":              title,
                    "company":            company,
                    "location":           location,
                    "url":                clean_url,
                    "posted":             "Recent",
                    "source":             "Relocate.me",
                    "search_label":       "Relocate.me",
                    "relocation_confirmed": True,  # all relocate.me jobs have this
                    "jd_text":            "",
                })

    except Exception as e:
        print(f"  [Relocate.me] ERROR: {e}")
    finally:
        if driver:
            try: driver.quit()
            except: pass

    print(f"  [Relocate.me] {len(hits)} PM jobs found")
    return hits

# LinkedIn Worldwide searches — geoId=92000000 covers all countries
# Multiple keyword variants to pull different slices of the job pool
# LinkedIn caps at ~75 results per search — varied keywords = broader coverage
SEARCHES = [
    {"label": "Worldwide — Product Manager",        "keywords": "product manager",        "icon": "🌍", "geo": "92000000", "location": "Worldwide"},
    {"label": "Worldwide — Senior Product Manager", "keywords": "senior product manager", "icon": "🌍", "geo": "92000000", "location": "Worldwide"},
]


_LI_TPR = {"24h": "r86400", "week": "r604800"}

def fetch_linkedin_global(keywords: str, time_range: str = "week", geo: str = "", location: str = "", session=None) -> list:
    """Search LinkedIn by country using both geoId and location name."""
    import urllib.parse
    tpr = _LI_TPR.get(time_range, "r604800")
    url = (
        "https://www.linkedin.com/jobs/search/"
        f"?keywords={urllib.parse.quote(keywords)}"
        f"&f_TPR={tpr}&sortBy=DD"
    )
    if geo:
        url += f"&geoId={geo}"
    # Note: location text param not used — geoId alone is more reliable
    try:
        r = _LI_GET(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"    ⚠️  Fetch error: {e}")
        return []

    soup  = BeautifulSoup(r.text, "html.parser")
    cards = soup.find_all("div", class_="base-card", limit=200)
    hits  = []
    for card in cards:
        try:
            title    = card.find("h3", class_="base-search-card__title").get_text(strip=True)
            company  = card.find("h4", class_="base-search-card__subtitle").get_text(strip=True)
            location = card.find("span", class_="job-search-card__location").get_text(strip=True)
            time_tag = card.find("time")
            posted   = time_tag.get_text(strip=True) if time_tag else "N/A"
            posted_dt= time_tag.get("datetime", "") if time_tag else ""
            raw_url  = card.find("a", class_="base-card__full-link")["href"]
            id_match = re.search(r"(\d{8,})", raw_url)
            if not id_match: continue
            num_id   = id_match.group(1)
            # Filter non-PM roles
            if not is_pm_role(title): continue
            hits.append({
                "title":     title,
                "company":   company,
                "location":  location,
                "posted":    posted,
                "posted_dt": posted_dt,
                "url":       f"https://www.linkedin.com/jobs/view/{num_id}",
                "job_id":    "lig_" + num_id,
            })
        except:
            continue

    return hits[:TOP_N]

# ─────────────────────────────────────────────
# FETCH FULL JD
# ─────────────────────────────────────────────
def fetch_jd(url: str) -> str:
    """Fetch LinkedIn job description via shared Playwright browser session."""
    try:
        from playwright_browser import fetch_jd_playwright
        return fetch_jd_playwright(url)
    except Exception:
        return ""

# ─────────────────────────────────────────────
# VERIFY RELOCATION/VISA IN JD
# ─────────────────────────────────────────────
RELOCATION_SIGNALS = [
    "relocation assistance", "relocation package", "relocation support",
    "relocation bonus", "relocation reimbursement",
    "moving allowance", "moving expenses", "moving assistance",
    "we'll help you move", "we will help you move",
    "we offer relocation", "we provide relocation",
    "visa sponsorship", "visa support", "we sponsor",
    "we will sponsor", "we are able to sponsor",
    "immigration support", "immigration assistance",
    "h1b", "h-1b", "tier 2", "skilled worker visa",
    "work visa provided", "sponsorship available",
]

VISA_SIGNALS = [
    "visa sponsorship", "visa support", "we sponsor",
    "we will sponsor", "we are able to sponsor",
    "immigration support", "immigration assistance",
    "h1b", "h-1b", "tier 2", "skilled worker visa",
    "work visa provided", "sponsorship available",
    "work authorization provided", "we provide visa",
]

# Phrases that NEGATE a relocation/visa signal — check these first
RELOCATION_DENY_SIGNALS = [
    "do not offer visa", "does not offer visa",
    "no visa sponsorship", "not offer visa sponsorship",
    "unable to sponsor", "cannot sponsor", "will not sponsor",
    "not able to sponsor", "does not sponsor",
    "no relocation assistance", "not offer relocation",
    "do not offer relocation", "does not provide relocation",
    "relocation assistance is not", "relocation is not",
    "no sponsorship", "sponsorship is not available",
    "not provide sponsorship", "cannot provide sponsorship",
    "within the u.s. only", "within the us only",
    "u.s. residents only", "us residents only",
    "must be authorized to work in the us without",
    "authorized to work without sponsorship",
]

def has_relocation_signal(jd: str, title: str, company: str) -> bool:
    """Return True only if JD confirms relocation/visa AND does not deny it."""
    text = (jd + " " + title).lower()
    # First check for explicit denial — if denied, return False immediately
    if any(sig in text for sig in RELOCATION_DENY_SIGNALS):
        return False
    # Then check for positive signals
    return any(sig in text for sig in RELOCATION_SIGNALS)

# ─────────────────────────────────────────────
# CLAUDE EVALUATOR
# ─────────────────────────────────────────────
def _load_api_key() -> str:
    import importlib, sys as _sys
    base_dir = os.path.dirname(os.path.abspath(__file__))
    if base_dir not in _sys.path:
        _sys.path.insert(0, base_dir)
    cfg = importlib.import_module("config")
    key = getattr(cfg, "ANTHROPIC_API_KEY", "")
    if not key or key == "PASTE_YOUR_ANTHROPIC_KEY_HERE":
        raise RuntimeError("Add ANTHROPIC_API_KEY to config.py")
    return key


EVAL_PROMPT = """You are a recruiter evaluating job listings for a candidate seeking global opportunities.
Give an Apply / Maybe / Skip decision with a one-line reason.

Use this distribution: ~20% Apply, ~35% Maybe, ~45% Skip.
When in doubt between Apply and Maybe, pick Maybe.
Only Skip when there is a clear disqualifying reason.

Candidate Profile:
- Title: Senior Product Manager, 9+ years experience
- Domain: B2C consumer internet, D2C e-commerce, health & wellness, fintech, food-tech
- Strengths: Funnel optimisation, A/B experimentation, AI-powered personalisation,
  lifecycle engagement, retention, monetisation, SQL, Mixpanel, WebEngage, MoEngage
- Education: MBA (Chetana Institute), BMS Marketing — no CS/B.Tech degree
- Location: Mumbai, India — open to relocating anywhere globally
- Visa: Will need employer visa sponsorship for all non-India roles

Apply if: title and seniority match, domain overlaps, company is known to sponsor visas, no hard blockers.
Maybe if: title fits but domain unfamiliar, or visa sponsorship status unclear.

Hard Skip ONLY if:
1. Explicitly requires B.Tech/CS degree as mandatory
2. Role is clearly junior — under 4 years experience stated
3. Domain is purely supply chain, warehouse ops, or clinical healthcare
4. Role title completely unrelated — coordinator, account manager, program manager
5. JD explicitly states no visa sponsorship / must be local citizen / security clearance required

Respond ONLY in this exact format:
Decision: Apply / Maybe / Skip
Reason: [max 15 words]
Gap: [biggest gap or None]"""


def evaluate_job(job: dict) -> dict:
    title    = job.get("title", "")
    company  = job.get("company", "")
    location = job.get("location", "")
    label    = job.get("search_label", "")
    jd_text  = job.get("jd_text", "")

    if jd_text:
        job_context = (
            "Job Title: " + title + "\n"
            + "Company: " + company + "\n"
            + "Location: " + location + "\n"
            + "Found via search: " + label + "\n\n"
            + "Full Job Description:\n" + jd_text
        )
    else:
        job_context = (
            "Job Title: " + title + "\n"
            + "Company: " + company + "\n"
            + "Location: " + location + "\n"
            + "Found via search: " + label + "\n"
            + "(Note: Full JD could not be fetched)"
        )

    prompt = EVAL_PROMPT + "\n\n" + job_context

    try:
        api_key = _load_api_key()
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model":      "claude-haiku-4-5-20251001",
                "max_tokens": 150,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=25,
        )
        resp.raise_for_status()
        data = resp.json()
        text = "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        ).strip()

        result = {"decision": "Skip", "reason": "", "gap": ""}
        for line in text.splitlines():
            line = line.strip()
            if line.lower().startswith("decision:"):
                raw = line.split(":", 1)[1].strip()
                if "apply" in raw.lower():   result["decision"] = "Apply"
                elif "maybe" in raw.lower(): result["decision"] = "Maybe"
                else:                        result["decision"] = "Skip"
            elif line.lower().startswith("reason:"):
                result["reason"] = line.split(":", 1)[1].strip()
            elif line.lower().startswith("gap:"):
                result["gap"] = line.split(":", 1)[1].strip()
        return result
    except Exception as e:
        print(f"    ⚠️  Eval error: {e}")
        return {"decision": "Skip", "reason": "Evaluation failed", "gap": "—"}

# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
# OUTPUT — handled by sheets_writer + ntfy_notify
# ─────────────────────────────────────────────
def load_seen_urls() -> set:
    """Dedup handled at write time by sheets_writer — return empty set."""
    return set()

def save_to_excel(jobs: list):
    """Stub — saving done by sheets_writer.save_global_jobs() in main()."""
    pass

def notify(title: str, message: str):
    """Stub — notifications sent via ntfy_notify.run_summary() in main()."""
    print(f"  [notify] {title}: {message}")

# ─────────────────────────────────────────────
# MAIN

# ─────────────────────────────────────────────
# MAIN — single 24h run, then exit
# ─────────────────────────────────────────────
def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("\n" + "="*55)
    print("  LINKEDIN GLOBAL PM JOBS (HOSTED) — 24h")
    print(f"  [{now}]")
    print("="*55)

    if not SPREADSHEET_ID:
        print("  ERROR: GLOBAL_SPREADSHEET_ID env var not set. Exiting.")
        sys.exit(1)

    from sheets_writer import save_global_jobs
    from ntfy_notify import run_summary

    seen = load_seen_urls()

    print("\n  [1/3] Searching LinkedIn worldwide (last 24 hours)...")
    all_jobs           = []
    seen_ids           = set()
    title_company_seen = set()

    for search in SEARCHES:
        icon = search["icon"]
        lbl  = search["label"]
        kw   = search["keywords"]
        print(f"\n  {icon} [{lbl}]")
        jobs = fetch_linkedin_global(
            kw, "24h",
            geo=search.get("geo", ""),
            location=search.get("location", "")
        )
        new = 0
        for job in jobs:
            if job["job_id"] in seen_ids:
                continue
            if job["url"] in seen:
                continue
            tc_key = (job["title"].lower().strip(), job["company"].lower().strip())
            if tc_key in title_company_seen:
                continue
            seen_ids.add(job["job_id"])
            title_company_seen.add(tc_key)
            job["source"]       = "LinkedIn Global"
            job["search_label"] = lbl
            all_jobs.append(job)
            new += 1
        print(f"  -> {new} new job(s) found")
        time.sleep(2)

    if not all_jobs:
        print("\n  No new jobs found.")
        run_summary("LinkedIn Global", 0, 0, 0)
        return

    print(f"\n  Total: {len(all_jobs)} unique jobs")

    NO_VISA_SIGNALS = [
        "no visa sponsorship", "not able to sponsor", "cannot sponsor",
        "will not sponsor", "does not sponsor", "unable to sponsor",
        "us citizens only", "us citizen only",
        "must be authorized to work in the us without sponsorship",
        "no sponsorship available", "sponsorship not available",
        "authorized to work in the united states without sponsorship",
        "no relocation assistance at this time",
    ]

    print("\n  [2/3] Fetching JDs + filtering...")
    kept         = []
    hard_skipped = 0
    for i, job in enumerate(all_jobs, 1):
        title_short = job["title"][:40]
        print(f"  [{i}/{len(all_jobs)}] {title_short}...", end=" ", flush=True)
        jd       = fetch_jd(job["url"])
        job["jd_text"] = jd
        jd_lower = jd.lower()
        if any(s in jd_lower for s in NO_VISA_SIGNALS):
            print("no sponsorship -- skipped")
            hard_skipped += 1
            continue
        job["relocation_confirmed"] = any(s in jd_lower for s in RELOCATION_SIGNALS)
        job["visa_confirmed"]       = any(s in jd_lower for s in RELOCATION_SIGNALS)
        kept.append(job)
        print("ok")
        time.sleep(0.5)

    all_jobs = kept
    print(f"  Hard skipped: {hard_skipped}  |  Remaining: {len(all_jobs)}")

    if not all_jobs:
        print("  All jobs filtered out.")
        run_summary("LinkedIn Global", 0, 0, 0)
        return

    print("\n  [3/3] Evaluating with Claude AI...")
    for i, job in enumerate(all_jobs, 1):
        title_short   = job["title"][:35]
        company_short = job["company"][:20]
        print(f"  [{i}/{len(all_jobs)}] {title_short} @ {company_short}...", end=" ", flush=True)
        ev = evaluate_job(job)
        job["evaluation"] = ev
        badge = {"Apply": "Apply", "Maybe": "Maybe", "Skip": "Skip"}.get(ev["decision"], "?")
        print(f"{badge} | {ev['reason'][:60]}")
        time.sleep(0.5)

    n_apply, n_maybe, n_skip = save_global_jobs(SPREADSHEET_ID, all_jobs)
    run_summary("LinkedIn Global", n_apply, n_maybe, n_skip)

    print(f"\n  Done. Apply:{n_apply}  Maybe:{n_maybe}  Skip:{n_skip}")

    try:
        from playwright_browser import close_browser
        close_browser()
    except Exception:
        pass


if __name__ == "__main__":
    main()
