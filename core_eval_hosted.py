"""
core_eval_hosted.py — scraping + Claude eval engine (hosted/GitHub Actions version)
=====================================================================================
Sources: LinkedIn · Naukri · Hirist · IIMJobs
Output:  Google Sheets (Apply / Maybe / Skip tabs)
Notify:  ntfy push notification after each run

Differences from local core_eval.py:
- No openpyxl / Excel output
- No plyer desktop notifications
- Expanded LinkedIn keyword list (Associate PM → VP of Product)
- Playwright for LinkedIn JD fetch (rate-limit resilient)
- Selenium stays for Naukri/Hirist/IIMJobs (works headless on Ubuntu CI)
"""

import re
import requests
import urllib.parse
import time
import os
import sys

# curl_cffi for LinkedIn search page fetches (TLS fingerprint)
try:
    from curl_cffi import requests as curl_requests
    _LI_GET = lambda url, **kw: curl_requests.get(url, impersonate="chrome", **kw)
except ImportError:
    _LI_GET = requests.get

# Windows console can't print emoji with the default codepage — force UTF-8
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
TOP_N                  = 100
SEARCH_KEYWORD         = "Product Manager"
SEARCH_LOCATION        = "India"
BASE_DIR               = os.path.dirname(os.path.abspath(__file__))
CHECK_INTERVAL_MINUTES = 30   # unused on hosted but kept for import compat

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ─────────────────────────────────────────────
# PM ROLE FILTER
# ─────────────────────────────────────────────
PM_KEYWORDS = [
    "product manager", "product management",
    "senior pm", "associate pm", "apm", "group pm", "principal pm",
    "staff pm", "lead pm", "director of product", "vp of product",
    "head of product", "chief product", "product lead", "product owner",
    "growth pm", "technical pm", "platform pm", " pm -", " pm,", " pm ", "(pm)",
    "d2c manager", "growth manager", "head of growth", "growth marketing",
    "head of e-commerce", "ecommerce manager", "e-commerce manager",
    "category manager", "category head",
    "business development manager", "bdm",
    "product marketing manager", "pmm",
    "strategy manager", "head of strategy",
]

def is_pm_role(title: str) -> bool:
    t = title.lower().strip()
    if any(kw in t for kw in PM_KEYWORDS): return True
    if t.startswith(("pm ", "pm-", "pm,", "pmm ", "bdm ")): return True
    if t in ("pm", "pmm", "bdm"): return True
    return False

# ─────────────────────────────────────────────
# TIME HELPERS
# ─────────────────────────────────────────────
def posted_to_minutes(posted: str) -> int:
    s = posted.lower().strip()
    if not s or s in ("n/a", "recent", "just now", "today", "within 24h"): return 0
    m = re.search(r"(\d+)\s*(min|hour|day|week|month)", s)
    if not m: return 9999
    n, unit = int(m.group(1)), m.group(2)
    return {"min": 1, "hour": 60, "day": 1440, "week": 10080, "month": 43200}[unit] * n

def _within_minutes(job: dict, minutes: int) -> bool:
    CUTOFF = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    dt_str = job.get("posted_dt", "")
    if dt_str:
        try:
            dt = datetime.fromisoformat(dt_str)
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            return dt >= CUTOFF
        except: pass
    posted = re.sub(r"^posted\s*:?\s*", "", job.get("posted", "").lower().strip())
    if not posted or posted in ("n/a", "recent", "just now", "today", "within 24h"): return True
    m = re.search(r"(\d+)\s*(min|hour|day|week|month)", posted)
    if not m: return True
    n, unit = int(m.group(1)), m.group(2)
    return {"min": 1, "hour": 60, "day": 1440, "week": 10080, "month": 43200}[unit] * n <= minutes

def within_24hrs(job: dict) -> bool:
    return _within_minutes(job, 1440)

def within_week(job: dict) -> bool:
    return _within_minutes(job, 10080)

def sort_newest_first(jobs: list) -> list:
    def key(j):
        dt = j.get("posted_dt", "")
        if dt: return dt
        return str(9999 - posted_to_minutes(j.get("posted", ""))).zfill(6)
    return sorted(jobs, key=key, reverse=True)

def save_jobs_to_excel(jobs: list, sheet_override: str = None):
    """
    Stub — in the hosted version, saving is done by pm_eval_hosted.py
    calling sheets_writer.save_eval_jobs() directly after evaluation.
    This stub keeps import compatibility with pm_eval code unchanged.
    """
    pass

def load_seen_jobs() -> set:
    """
    UNUSED / DEPRECATED — do not call this.

    This used to return an empty set, on the assumption dedup only needed to
    happen at write time. That was wrong: it let evaluate_batch() send already-seen
    jobs to the Claude API every run, burning tokens on duplicates. pm_eval_hosted.py
    now calls sheets_writer.load_seen_urls(spreadsheet_id) directly, BEFORE
    evaluation, instead of this function. Kept only for backwards compatibility;
    left unused everywhere.
    """
    return set()

# ─────────────────────────────────────────────
# NOTIFICATIONS
# ─────────────────────────────────────────────
def notify(title: str, message: str, urgency: str = "normal"):
    """Stub — actual notifications sent via ntfy_notify.run_summary() in pm_eval_hosted.py"""
    print(f"  [notify] {title}: {message}")

def summary_notify(keyword: str, n_apply: int, n_maybe: int, n_skip: int):
    """Stub — actual notifications sent via ntfy_notify.run_summary() in pm_eval_hosted.py"""
    pass

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
# SCRAPERS
# ─────────────────────────────────────────────
NAUKRI_DEBUG = os.path.join(BASE_DIR, "naukri_debug.html")
HIRIST_DEBUG  = os.path.join(BASE_DIR, "hirist_debug.html")

HIRIST_CATEGORY_MAP = {
    "product manager":              "product-management-jobs",
    "product marketing manager":    "product-management-jobs",
    "growth manager":               "product-management-jobs",
    "d2c manager":                  "sales-jobs",
    "head of e-commerce":           "sales-jobs",
    "category manager":             "sales-jobs",
    "business development manager": "business-development-jobs",
    "strategy manager":             "product-management-jobs",
}

_LI_TPR = {
    "24h":  "r86400",
    "week": "r604800",
}

def fetch_linkedin(keyword=SEARCH_KEYWORD, time_range="24h"):
    import random
    tpr = _LI_TPR.get(time_range, "r86400")
    url = (
        f"https://www.linkedin.com/jobs/search/"
        f"?keywords={urllib.parse.quote(keyword)}"
        f"&location={urllib.parse.quote(SEARCH_LOCATION)}"
        f"&f_TPR={tpr}&sortBy=DD"
    )
    print(f"  [LinkedIn] fetching ({time_range})...")

    # Retry up to 2 times on 429 with exponential backoff
    for attempt in range(3):
        try:
            import urllib3; urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            r = _LI_GET(url, headers=HEADERS, timeout=15)
            if r.status_code == 429:
                wait = 45 + random.uniform(0, 20) + (attempt * 30)
                print(f"  [LinkedIn] 429 rate-limited — waiting {wait:.0f}s (attempt {attempt+1}/3)...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            break
        except Exception as e:
            if attempt == 2:
                print(f"  [LinkedIn] ERROR: {e}"); return []
            wait = 45 + random.uniform(0, 20)
            print(f"  [LinkedIn] error, retrying in {wait:.0f}s...")
            time.sleep(wait)
    else:
        print(f"  [LinkedIn] ERROR: gave up after 3 attempts (429)"); return []
    soup  = BeautifulSoup(r.text, "html.parser")
    cards = soup.find_all("div", class_="base-card", limit=200)
    hits  = []
    for card in cards:
        try:
            title    = card.find("h3", class_="base-search-card__title").get_text(strip=True)
            if not is_pm_role(title): continue
            company  = card.find("h4", class_="base-search-card__subtitle").get_text(strip=True)
            location = card.find("span", class_="job-search-card__location").get_text(strip=True)
            time_tag = card.find("time")
            posted   = time_tag.get_text(strip=True) if time_tag else "N/A"
            posted_dt= time_tag.get("datetime", "") if time_tag else ""
            raw_url  = card.find("a", class_="base-card__full-link")["href"]
            id_match = re.search(r"(\d{8,})", raw_url)
            num_id   = id_match.group(1) if id_match else raw_url.split("/")[-1]
            hits.append({
                "source": "LinkedIn", "job_id": "li_" + num_id,
                "title": title, "company": company, "location": location,
                "posted": posted, "posted_dt": posted_dt, "experience": "—",
                "url": f"https://www.linkedin.com/jobs/view/{num_id}",
            })
        except: continue
    jobs = sort_newest_first(hits)[:TOP_N]
    print(f"  [LinkedIn] {len(hits)} found → top {len(jobs)}")
    return jobs

def fetch_naukri(keyword=SEARCH_KEYWORD, time_range="24h"):
    age_param = "1" if time_range == "24h" else "7"
    slug = keyword.lower().replace(" ", "-")
    url  = f"https://www.naukri.com/{slug}-jobs-in-india?jobAge={age_param}&sortBy=displayDate"
    print(f"  [Naukri] launching Chrome ({time_range})...")
    driver, hits = None, []
    try:
        driver = make_driver(); driver.get(url)
        try:
            WebDriverWait(driver, 20).until(EC.presence_of_element_located(
                (By.CSS_SELECTOR, "article.jobTuple,div.srp-jobtuple-wrapper")))
        except:
            with open(NAUKRI_DEBUG, "w", encoding="utf-8") as f: f.write(driver.page_source)
            print(f"  [Naukri] Timeout — debug saved to {NAUKRI_DEBUG}"); return []
        time.sleep(2)
        soup  = BeautifulSoup(driver.page_source, "html.parser")
        cards = soup.find_all("article", class_=lambda c: c and "jobTuple" in c, limit=TOP_N * 3)
        if not cards:
            cards = soup.find_all("div", class_=lambda c: c and "srp-jobtuple" in c, limit=TOP_N * 3)
        for card in cards:
            try:
                ta = (card.find("a", class_=lambda c: c and "title" in c.lower()) or
                      card.find("a", attrs={"data-ga-track": True}))
                if not ta: continue
                title = ta.get_text(strip=True)
                if not is_pm_role(title): continue
                job_url = ta.get("href", "")
                ct = (card.find("a", class_=lambda c: "comp-name" in (c or "").lower()) or
                      card.find("span", class_=lambda c: "comp-name" in (c or "").lower()))
                company = ct.get_text(strip=True) if ct else "N/A"
                lt = (card.find("span", class_=lambda c: "locWdth" in (c or "")) or
                      card.find("li", class_=lambda c: "location" in (c or "").lower()))
                location = lt.get_text(strip=True) if lt else "India"
                pt = (card.find("span", class_=lambda c: "job-post-day" in (c or "")) or
                      card.find("span", title=lambda t: t and "Posted" in (t or "")))
                posted = pt.get_text(strip=True) if pt else "Recent"
                hits.append({
                    "source": "Naukri",
                    "job_id": "nk_" + job_url.rstrip("/").split("/")[-1].split("?")[0],
                    "title": title, "company": company, "location": location,
                    "posted": posted, "posted_dt": "", "experience": "—", "url": job_url,
                })
            except: continue
    except Exception as e: print(f"  [Naukri] ERROR: {e}")
    finally:
        if driver:
            try: driver.quit()
            except: pass
    jobs = sort_newest_first(hits)[:TOP_N]
    print(f"  [Naukri] {len(hits)} found → top {len(jobs)}")
    return jobs

def fetch_hirist(keyword=SEARCH_KEYWORD, time_range="24h"):
    kw_lo    = keyword.lower()
    category = HIRIST_CATEGORY_MAP.get(kw_lo, "product-management-jobs")
    url      = f"https://www.hirist.tech/c/{category}.html"
    print(f"  [Hirist] launching Chrome → {category} ({time_range})...")
    driver, hits, seen_run = None, [], set()
    try:
        driver = make_driver(); driver.get(url)
        try:
            WebDriverWait(driver, 25).until(
                EC.presence_of_element_located((By.XPATH, "//a[contains(@href,'/j/')]")))
        except:
            with open(HIRIST_DEBUG, "w", encoding="utf-8") as f: f.write(driver.page_source)
            print(f"  [Hirist] Timeout — debug saved to {HIRIST_DEBUG}"); return []
        time.sleep(3)
        soup = BeautifulSoup(driver.page_source, "html.parser")
        for link in soup.find_all("a", href=lambda h: h and h.startswith("/j/")):
            try:
                href   = link["href"]
                job_id = "hr_" + href.rstrip("/").split("/")[-1].split("?")[0]
                if job_id in seen_run: continue
                seen_run.add(job_id)
                heading = link.find(["h1", "h2", "h3", "h4"])
                title   = (heading.get_text(strip=True) if heading
                           else next((t.strip() for t in link.strings if t.strip()), ""))
                if not title: continue
                card = link.find_parent("li") or link.find_parent("div")
                company, location, posted = "N/A", "India", "Recent"
                if card:
                    dm = re.match(r"^(.+?)\s+-\s+", title)
                    if dm:
                        cand = dm.group(1).strip()
                        if cand.lower().split()[0] not in {"senior", "associate", "principal",
                                "group", "lead", "product", "manager", "pm"}:
                            company = cand
                    ct = card.get_text(" ", strip=True)
                    for chunk in ct.split():
                        if chunk in ("Bangalore", "Mumbai", "Delhi", "Hyderabad", "Pune",
                                     "Chennai", "Noida", "Gurgaon", "Gurugram", "Kolkata",
                                     "Remote", "India", "Bengaluru"):
                            location = chunk; break
                    dm2 = re.search(r"(\d+\s*(day|hour|min|week|month)s?\s*ago|Just now|Today)", ct, re.I)
                    if dm2: posted = dm2.group(0)
                hits.append({
                    "source": "Hirist/IIMJobs", "job_id": job_id,
                    "title": title, "company": company, "location": location,
                    "posted": posted, "posted_dt": "", "experience": "—",
                    "url": "https://www.hirist.tech" + href,
                })
            except: continue
    except Exception as e: print(f"  [Hirist] ERROR: {e}")
    finally:
        if driver:
            try: driver.quit()
            except: pass
    # For week mode keep all; for 24h filter
    if time_range == "24h":
        hits = [j for j in hits if within_24hrs(j)]
    jobs = sort_newest_first(hits)[:TOP_N]
    print(f"  [Hirist] {len(hits)} found → top {len(jobs)}")
    return jobs


def fetch_iimjobs(keyword=SEARCH_KEYWORD, time_range="24h"):
    """Scrape IIMJobs for PM roles — tries multiple category URLs with scroll."""
    urls_to_try = [
        "https://www.iimjobs.com/k/product-management-jobs",
        "https://www.iimjobs.com/k/it-product-management-jobs",
    ]
    print(f"  [IIMJobs] launching Chrome ({time_range})...")
    driver, hits, seen_run = None, [], set()
    try:
        driver = make_driver()
        for url in urls_to_try:
            print(f"  [IIMJobs] {url.split('/')[-1]}...")
            driver.get(url)
            time.sleep(5)
            for _ in range(6):
                driver.execute_script("window.scrollBy(0, 800)")
                time.sleep(1.2)
            time.sleep(2)

            soup = BeautifulSoup(driver.page_source, "html.parser")
            job_links = [
                a for a in soup.find_all("a", href=True)
                if "/j/" in a.get("href", "")
                and len(a.get("href", "").split("/j/")[-1]) > 5
            ]
            print(f"    {len(job_links)} /j/ links found")

            for link in job_links:
                try:
                    href = link.get("href", "")
                    if not href: continue
                    if not href.startswith("http"):
                        href = "https://www.iimjobs.com" + href
                    clean_href = href.split("?")[0].rstrip("/")
                    job_id = "iim_" + clean_href.split("/j/")[-1][:40]
                    if job_id in seen_run: continue
                    seen_run.add(job_id)

                    heading = link.find(["h2", "h3", "h4", "h1", "span"])
                    title   = heading.get_text(strip=True) if heading else link.get_text(strip=True)
                    title   = " ".join(title.split())
                    if not title or len(title) < 3: continue
                    if not is_pm_role(title): continue

                    card = (link.find_parent("article") or
                            link.find_parent("li") or
                            link.find_parent("div"))
                    company, location, posted = "N/A", "India", "Recent"

                    # IIMJobs title format: "CompanyName - Job Title"
                    if " - " in title:
                        parts = title.split(" - ", 1)
                        # Heuristic: if first part looks like a company (shorter, no PM keywords)
                        if len(parts[0]) < 40 and not is_pm_role(parts[0]):
                            company = parts[0].strip()
                            title   = parts[1].strip()

                    if card:
                        ct = card.get_text(" ", strip=True)
                        # Try to find company name in card text if not found in title
                        if company == "N/A":
                            # Look for text in strong/b tags or specific company elements
                            for tag in card.find_all(["strong", "b", "span", "p"]):
                                txt = tag.get_text(strip=True)
                                if 2 < len(txt) < 50 and txt not in (title,) and not is_pm_role(txt):
                                    company = txt; break
                        for chunk in ct.split():
                            if chunk in ("Bangalore","Mumbai","Delhi","Hyderabad","Pune",
                                         "Chennai","Noida","Gurgaon","Gurugram","Kolkata",
                                         "Remote","Bengaluru","Ahmedabad","Jaipur"):
                                location = chunk; break
                        dm = re.search(r"(\d+\s*(day|hour|min|week|month)s?\s*ago|Just now|Today)", ct, re.I)
                        if dm: posted = dm.group(0)

                    hits.append({
                        "source": "IIMJobs", "job_id": job_id,
                        "title": title, "company": company,
                        "location": location, "posted": posted,
                        "posted_dt": "", "experience": "—",
                        "url": clean_href,
                    })
                except: continue

        if not hits:
            debug_path = os.path.join(BASE_DIR, "iimjobs_debug.html")
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(driver.page_source)
            print(f"  [IIMJobs] 0 hits — debug saved to {debug_path}")

    except Exception as e:
        print(f"  [IIMJobs] ERROR: {e}")
    finally:
        if driver:
            try: driver.quit()
            except: pass

    if time_range == "24h":
        hits = [j for j in hits if within_24hrs(j)]
    jobs = sort_newest_first(hits)[:TOP_N]
    print(f"  [IIMJobs] {len(hits)} found → top {len(jobs)}")
    return jobs
# ─────────────────────────────────────────────
# SOURCES
# ─────────────────────────────────────────────
# Multiple LinkedIn keyword searches to get broader coverage
# LinkedIn caps at ~75 results per search — using different keywords
# pulls different slices of the job pool
LINKEDIN_KEYWORDS = [
    "Product Manager",
    "Senior Product Manager",
    "Associate Product Manager",
    "Product Owner",
    "Group Product Manager",
    "Principal Product Manager",
    "Director of Product",
    "VP of Product",
    "Head of Product",
]

def fetch_linkedin_multi(keyword=SEARCH_KEYWORD, time_range="24h") -> list:
    """keyword param ignored — uses LINKEDIN_KEYWORDS list internally."""
    """Run multiple LinkedIn searches with different keywords, deduplicate."""
    import random
    all_hits = []
    seen_ids = set()
    for kw in LINKEDIN_KEYWORDS:
        print(f"    🔵 LinkedIn [{kw}]...", end=" ", flush=True)
        try:
            jobs = fetch_linkedin(kw, time_range)
            new  = 0
            for job in jobs:
                if job["job_id"] not in seen_ids:
                    seen_ids.add(job["job_id"])
                    all_hits.append(job)
                    new += 1
            print(f"{new} new")
        except Exception as e:
            print(f"error: {e}")
        # Randomised delay — reduces 429 rate-limit errors vs. a fixed interval
        delay = random.uniform(8, 14)
        print(f"    ⏳ waiting {delay:.1f}s before next keyword...")
        time.sleep(delay)
    return all_hits

SOURCES = [
    ("LinkedIn",       fetch_linkedin_multi),
    ("Naukri",         fetch_naukri),
    ("Hirist/IIMJobs", fetch_hirist),
    ("IIMJobs",        fetch_iimjobs),
]

SOURCE_ICONS = {
    "LinkedIn":       "🔵",
    "Naukri":         "🟠",
    "Hirist/IIMJobs": "🟣",
    "IIMJobs":        "🟤",
}

def fetch_jd_text(job: dict) -> str:
    """Fetch and extract the full job description text from the job URL."""
    url    = job.get("url", "")
    source = job.get("source", "")
    if not url:
        return ""
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        if source == "LinkedIn":
            # Use Playwright — reuses a single browser session across all JD fetches
            # in the run, avoiding the per-connection rate limiting curl_cffi hit.
            from playwright_browser import fetch_jd_playwright
            return fetch_jd_playwright(url)

        elif source in ("Naukri", "Hirist/IIMJobs", "IIMJobs"):
            # Both block plain requests — use Selenium
            driver = None
            try:
                driver = make_driver()
                driver.get(url)
                time.sleep(4)
                soup = BeautifulSoup(driver.page_source, "html.parser")

                if source == "Naukri":
                    selectors = [
                        "div.styles_JDC__dang-inner-html__wyFgJ",
                        "div[class*='dang-inner-html']",
                        "div[class*='job-desc']",
                        "section[class*='job-desc']",
                        "div[class*='jd-']",
                        "div[class*='description']",
                    ]
                else:  # Hirist
                    selectors = [
                        "div.job-description",
                        "div.jd-detail",
                        "div[class*='description']",
                        "div[class*='job-desc']",
                    ]

                for sel in selectors:
                    try:
                        el = soup.select_one(sel)
                        if el:
                            txt = el.get_text(separator="\n", strip=True)
                            if len(txt) > 100:
                                return txt[:4000]
                    except: pass

                # Fallback: largest meaningful text block
                candidates = []
                for tag in soup.find_all(["div", "section"]):
                    txt = tag.get_text(strip=True)
                    if 200 < len(txt) < 8000:
                        candidates.append(txt)
                if candidates:
                    return max(candidates, key=len)[:4000]

            except Exception as e:
                pass
            finally:
                if driver:
                    try: driver.quit()
                    except: pass

    except Exception as e:
        pass  # fall back to title-only evaluation silently

    return ""


# ─────────────────────────────────────────────
# CLAUDE AI EVALUATOR
# ─────────────────────────────────────────────
EVAL_PROMPT = """You are a recruiter evaluating job listings for a candidate.
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
- Location: Mumbai, open to relocation within India

Apply if: title and seniority match, domain overlaps even partially, no hard blockers.
Maybe if: title fits but domain is unfamiliar, or seniority is slightly off but role is interesting.

Hard Skip ONLY if:
1. Explicitly requires B.Tech/CS degree (not just "preferred")
2. Role is clearly junior — Associate PM, APM, or under 4 years experience stated
3. Domain is purely supply chain, warehouse ops, or clinical healthcare with no consumer product angle
4. Role title is completely unrelated — project coordinator, account manager, program manager
5. Clearly requires deep expertise in a domain with zero overlap (e.g. semiconductors, defence)

Respond ONLY in this exact format — no extra text, no preamble:
Decision: Apply / Maybe / Skip
Reason: [max 15 words]
Gap: [biggest gap or None]"""


def _load_api_key() -> str:
    """Load Anthropic API key — env var first (GitHub Actions), then config.py (local)."""
    # GitHub Actions / hosted: key is in ANTHROPIC_API_KEY environment variable
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key
    # Local fallback: read from config.py in the same folder
    import importlib, sys as _sys
    base_dir = os.path.dirname(os.path.abspath(__file__))
    if base_dir not in _sys.path:
        _sys.path.insert(0, base_dir)
    try:
        cfg = importlib.import_module("config")
        key = getattr(cfg, "ANTHROPIC_API_KEY", "")
    except ImportError:
        pass
    if not key or key == "PASTE_YOUR_ANTHROPIC_KEY_HERE":
        raise RuntimeError("Set ANTHROPIC_API_KEY env var or add key to config.py")
    return key


def evaluate_job(job: dict) -> dict:
    """Fetch full JD then call Claude API to evaluate. Returns dict with decision/reason/gap."""
    title    = job.get("title", "")
    company  = job.get("company", "")
    location = job.get("location", "")
    source   = job.get("source", "")

    # Fetch the actual job description
    jd_text = fetch_jd_text(job)
    if jd_text:
        job_context = (
            "Job Title: " + title + "\n"
            + "Company: " + company + "\n"
            + "Location: " + location + "\n"
            + "Source: " + source + "\n\n"
            + "Full Job Description:\n" + jd_text
        )
    else:
        job_context = (
            "Job Title: " + title + "\n"
            + "Company: " + company + "\n"
            + "Location: " + location + "\n"
            + "Source: " + source + "\n"
            + "(Note: Full JD could not be fetched - evaluate on title/company only)"
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
            block.get("text", "") for block in data.get("content", [])
            if block.get("type") == "text"
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
        # IMPORTANT: this must NOT be "Skip". A "Skip" decision gets written to the
        # Sheet and permanently marked as seen (dedup is URL-presence based), so a
        # billing/auth/network failure would silently and permanently blackhole every
        # job it touched — they'd never be evaluated again even after the API key
        # works again. "Error" is filtered out before writing, so these jobs get
        # retried on the next run instead.
        return {"decision": "Error", "reason": f"Evaluation failed: {e}", "gap": "—"}
