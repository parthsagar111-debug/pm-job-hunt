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

import random
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

def _li_get_with_retry(url: str):
    """GET a LinkedIn search URL, retrying on 429/errors. Returns the response, or None."""
    import random
    import urllib3; urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    for attempt in range(3):
        try:
            r = _LI_GET(url, headers=HEADERS, timeout=15)
            if r.status_code == 429:
                wait = 45 + random.uniform(0, 20) + (attempt * 30)
                print(f"  [LinkedIn] 429 rate-limited — waiting {wait:.0f}s (attempt {attempt+1}/3)...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r
        except Exception as e:
            if attempt == 2:
                print(f"  [LinkedIn] ERROR: {e}"); return None
            wait = 45 + random.uniform(0, 20)
            print(f"  [LinkedIn] error, retrying in {wait:.0f}s...")
            time.sleep(wait)
    print(f"  [LinkedIn] ERROR: gave up after 3 attempts (429)")
    return None

def _parse_li_cards(html: str) -> tuple[list, set]:
    """Returns (PM-role jobs on the page, every job id on the page — PM or not)."""
    soup = BeautifulSoup(html, "html.parser")
    hits, page_ids = [], set()
    for card in soup.find_all("div", class_="base-card", limit=200):
        try:
            raw_url  = card.find("a", class_="base-card__full-link")["href"]
            id_match = re.search(r"(\d{8,})", raw_url)
            num_id   = id_match.group(1) if id_match else raw_url.split("/")[-1]
            page_ids.add(num_id)
            title    = card.find("h3", class_="base-search-card__title").get_text(strip=True)
            if not is_pm_role(title): continue
            company  = card.find("h4", class_="base-search-card__subtitle").get_text(strip=True)
            location = card.find("span", class_="job-search-card__location").get_text(strip=True)
            time_tag = card.find("time")
            posted   = time_tag.get_text(strip=True) if time_tag else "N/A"
            posted_dt= time_tag.get("datetime", "") if time_tag else ""
            hits.append({
                "source": "LinkedIn", "job_id": "li_" + num_id,
                "title": title, "company": company, "location": location,
                "posted": posted, "posted_dt": posted_dt, "experience": "—",
                "url": f"https://www.linkedin.com/jobs/view/{num_id}",
            })
        except: continue
    return hits, page_ids

LI_MAX_EXTRA_PAGES = 25   # paginate=True only — LinkedIn serves 10 results per extra page

# ── Pacing. Measured 2026-09-17 across a full global sweep: 2 rate-limit blocks in
# ~4,100 requests at 1.5s/page + 5-9s between searches, and >60% of that run's 115
# minutes was these sleeps. Halved on that evidence; _li_get_with_retry still backs
# off 45-95s on a 429, so the downside of being slightly too fast is a pause, not a
# failed run. Tune here, not at the call sites.
LI_PAGE_SLEEP  = 0.6          # between pagination pages
LI_SEARCH_GAP  = (2.5, 4.0)   # between one search/keyword and the next
LI_JD_SLEEP    = 0.3          # between guest-endpoint JD fetches

def li_search_gap() -> float:
    """Randomised so the request pattern isn't a metronome."""
    return random.uniform(*LI_SEARCH_GAP)

def fetch_linkedin(keyword=SEARCH_KEYWORD, time_range="24h", location=SEARCH_LOCATION,
                   paginate=False, limit=TOP_N):
    """
    The main search page returns ~60 results max. With paginate=True, keeps
    pulling LinkedIn's guest "see more" endpoint until it runs dry — needed for
    week-long windows, where 60 is nowhere near everything.
    """
    tpr   = _LI_TPR.get(time_range, "r86400")
    query = (
        f"keywords={urllib.parse.quote(keyword)}"
        f"&location={urllib.parse.quote(location)}"
        f"&f_TPR={tpr}&sortBy=DD"
    )
    print(f"  [LinkedIn] fetching ({time_range})...")
    r = _li_get_with_retry(f"https://www.linkedin.com/jobs/search/?{query}")
    if r is None:
        return []
    hits, all_ids = _parse_li_cards(r.text)
    pages = 1

    if paginate:
        for _ in range(LI_MAX_EXTRA_PAGES):
            time.sleep(LI_PAGE_SLEEP)
            r = _li_get_with_retry(
                "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
                f"?{query}&start={len(all_ids)}")
            if r is None:
                break
            page_hits, page_ids = _parse_li_cards(r.text)
            if not page_ids - all_ids:   # empty page, or LinkedIn looping back
                break
            all_ids |= page_ids
            hits.extend(page_hits)
            pages += 1

    unique = list({j["job_id"]: j for j in hits}.values())   # pages can overlap
    jobs   = sort_newest_first(unique)[:limit]
    scanned = f" (scanned {len(all_ids)} listings over {pages} pages)" if paginate else ""
    print(f"  [LinkedIn] {len(unique)} found{scanned} → top {len(jobs)}")
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
        delay = li_search_gap()
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
    "LinkedIn Gulf":  "🌴",
}

# ─────────────────────────────────────────────
# FINGERPRINT — second dedup key, alongside URL
# ─────────────────────────────────────────────
# A re-posted role gets a fresh LinkedIn id and a fresh URL, so URL-only dedup
# pays Claude for it again. company|title|country catches that. Country (not
# city) because the same role is often posted per-city: Anthropic's PM Growth
# appeared in Seattle, SF and NY on one run.
_US_STATE_TOKENS = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "dc", "fl", "ga", "hi", "id", "il", "in",
    "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh",
    "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut",
    "vt", "va", "wa", "wv", "wi", "wy", "united states", "usa",
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "district of columbia", "florida", "georgia", "hawaii", "idaho", "illinois",
    "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts",
    "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york", "north carolina", "north dakota",
    "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming",
}

def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", (s or "").lower()).split())

def location_country(location: str) -> str:
    """Coarse country token from a LinkedIn location string. US listings carry a
    state rather than a country ("Seattle, WA"), so those collapse to one token."""
    first = (location or "").split("/")[0]            # merged multi-city rows
    last  = _norm(first.split(",")[-1])
    if last in _US_STATE_TOKENS:
        return "united states"
    return last

def job_fingerprint(company: str, title: str, location: str) -> str:
    return f"{_norm(company)}|{_norm(title)}|{location_country(location)}"


def fetch_jd_guest(job_id_or_url: str) -> str:
    """LinkedIn JD via the logged-out guest endpoint — no browser, no Playwright.

    Accepts a job dict's "li_<id>" job_id, a bare numeric id, or a /jobs/view/ URL.
    Returns "" on any failure; the caller decides what to do about it.
    """
    m = re.search(r"(\d{8,})", job_id_or_url or "")
    if not m:
        return ""
    r = _li_get_with_retry(
        f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{m.group(1)}")
    if r is None:
        return ""
    el = BeautifulSoup(r.text, "html.parser").select_one(
        "div.show-more-less-html__markup, div.description__text")
    return el.get_text("\n", strip=True) if el else ""


def fetch_jd_text(job: dict) -> str:
    """Fetch and extract the full job description text from the job URL.

    Never raises — every failure path prints WHY before returning "", so a
    run's logs show real fetch outcomes instead of a silent 0-vs-100% JD
    rate that looks identical either way. Ported from job-automation
    2026-08-2x alongside the JD-capture patch — this repo (pm-job-hunt) is
    the one actually running in production, so this is where the real
    fetch failures need to be visible."""
    url    = job.get("url", "")
    source = job.get("source", "")
    if not url:
        print(f"  ⚠️  JD fetch ({source or 'unknown source'}): no URL on this row — cannot fetch.")
        return ""
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        if source.startswith("LinkedIn"):   # "LinkedIn" (India) and "LinkedIn Gulf"
            # Guest endpoint first: no browser at all, ~1.4s/job, and it fetched
            # 2570 of 2575 JDs in the 2026-09-17 global probe. Playwright stays as
            # a fallback only — it costs a chromium install per workflow run and a
            # persistent browser process for what is now a plain HTTP GET.
            jd = fetch_jd_guest(job.get("job_id", "") or url)
            if jd:
                return jd
            print(f"  ⚠️  JD fetch ({source}): guest endpoint empty — falling back to Playwright.")
            try:
                from playwright_browser import fetch_jd_playwright
                return fetch_jd_playwright(url)
            except Exception as e:
                print(f"  ⚠️  JD fetch ({source}): Playwright fallback failed — {type(e).__name__}: {e}")
                return ""

        elif source in ("Naukri", "Hirist/IIMJobs", "IIMJobs"):
            # Both block plain requests — use Selenium
            driver = None
            try:
                driver = make_driver()
            except Exception as e:
                print(f"  ⚠️  JD fetch ({source}): could not start Chrome/Selenium — {type(e).__name__}: {e}")
                return ""

            try:
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

                page_title = (soup.title.string.strip() if soup.title and soup.title.string else "")
                print(f"  ⚠️  JD fetch ({source}): page loaded but no JD text matched — "
                      f"page title: {page_title!r}, landed at: {driver.current_url!r}")

            except Exception as e:
                print(f"  ⚠️  JD fetch ({source}): {type(e).__name__}: {e}")
            finally:
                if driver:
                    try: driver.quit()
                    except: pass

        else:
            print(f"  ⚠️  JD fetch: unrecognized source {source!r} — no fetch method wired up for it.")

    except Exception as e:
        print(f"  ⚠️  JD fetch ({source}): unexpected error before dispatch — {type(e).__name__}: {e}")

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
Maybe if: title fits but domain is unfamiliar, or seniority is off but role is interesting —
this includes cases where the role's stated experience range is lower than the candidate's
9+ years (e.g. a listing wants 2-5 years) or higher. A numeric experience-range mismatch by
itself is NEVER a Skip — it belongs in Maybe.

Hard Skip ONLY if:
1. Explicitly requires B.Tech/CS degree (not just "preferred")
2. Role title itself is explicitly junior — "Associate Product Manager" or "APM" in the title
   (title-based signal only; a numeric years-of-experience range alone does not qualify, see Maybe above)
3. Domain is purely supply chain, warehouse ops, or clinical healthcare with no consumer product angle
4. Role title is completely unrelated — project coordinator, account manager, program manager
5. Clearly requires deep expertise in a domain with zero overlap (e.g. semiconductors, defence)

Record your answer with the record_decision tool: decision (Apply / Maybe / Skip),
reason (max 15 words), gap (biggest gap, or "None")."""


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


ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_URL   = "https://api.anthropic.com/v1/messages"

class ClaudeSchemaError(RuntimeError):
    """Claude replied, but not in the shape the caller demanded."""

def claude_structured(prompt: str, tool_name: str, tool_description: str,
                      input_schema: dict, max_tokens: int = 300,
                      timeout: int = 30) -> tuple[dict, dict]:
    """
    Call Claude and get back a dict matching input_schema, via a forced tool call.

    Every failure — HTTP error, no tool_use block, missing required field, value
    outside its enum — raises. Callers must translate that into a retryable
    "Error", never into a real decision: string-prefix parsing used to fall back
    to Skip on a malformed reply, and since Skip is written to the Sheet and
    deduped forever, one bad response silently blackholed a job for good.

    Returns (tool input dict, usage dict).
    """
    resp = requests.post(
        ANTHROPIC_URL,
        headers={"Content-Type": "application/json", "x-api-key": _load_api_key(),
                 "anthropic-version": "2023-06-01"},
        json={
            "model":       ANTHROPIC_MODEL,
            "max_tokens":  max_tokens,
            "messages":    [{"role": "user", "content": prompt}],
            "tools":       [{"name": tool_name, "description": tool_description,
                             "input_schema": input_schema}],
            "tool_choice": {"type": "tool", "name": tool_name},
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()

    block = next((b for b in data.get("content", [])
                  if b.get("type") == "tool_use" and b.get("name") == tool_name), None)
    if block is None:
        raise ClaudeSchemaError(f"no {tool_name} tool_use block in response "
                                f"(stop_reason={data.get('stop_reason')!r})")
    payload = block.get("input") or {}
    if not isinstance(payload, dict):
        raise ClaudeSchemaError(f"{tool_name} input was {type(payload).__name__}, not an object")

    props = input_schema.get("properties", {})
    for field in input_schema.get("required", []):
        if field not in payload:
            raise ClaudeSchemaError(f"{tool_name} input missing required field {field!r}")
        allowed = props.get(field, {}).get("enum")
        if allowed and payload[field] not in allowed:
            raise ClaudeSchemaError(f"{field}={payload[field]!r} is not one of {allowed}")
    return payload, data.get("usage", {})


EVAL_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["Apply", "Maybe", "Skip"],
                     "description": "The recommendation for this candidate."},
        "reason":   {"type": "string", "description": "One line, max 15 words."},
        "gap":      {"type": "string", "description": "Biggest gap, or 'None'."},
    },
    "required": ["decision", "reason", "gap"],
}

def evaluate_job(job: dict, prompt_template: str = None) -> dict:
    """Fetch full JD then call Claude API to evaluate. Returns dict with decision/reason/gap/jd.
    prompt_template defaults to EVAL_PROMPT (India); the Gulf feed passes its own."""
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

    prompt = (prompt_template or EVAL_PROMPT) + "\n\n" + job_context

    try:
        payload, _usage = claude_structured(
            prompt,
            tool_name="record_decision",
            tool_description="Record the Apply/Maybe/Skip decision for this job listing.",
            input_schema=EVAL_TOOL_SCHEMA,
        )
        return {"decision": payload["decision"], "reason": payload["reason"],
                "gap": payload["gap"], "jd": jd_text}

    except Exception as e:
        print(f"    ⚠️  Eval error: {type(e).__name__}: {e}")
        # IMPORTANT: this must NOT be "Skip". A "Skip" decision gets written to the
        # Sheet and permanently marked as seen (dedup is URL-presence based), so a
        # billing/auth/network/schema failure would silently and permanently blackhole
        # every job it touched — they'd never be evaluated again even after the API key
        # works again. "Error" is filtered out before writing, so these jobs get
        # retried on the next run instead. Since the decision now comes from a forced
        # tool call, a malformed reply raises instead of quietly reading as Skip.
        return {"decision": "Error", "reason": f"Evaluation failed: {e}", "gap": "—", "jd": jd_text}


CONSECUTIVE_ERROR_LIMIT = 3  # abort early if the API is clearly down (bad key, no funds, outage)

def evaluate_batch(jobs: list, prompt_template: str = None) -> tuple[list, bool]:
    """
    Returns (evaluated_jobs, aborted).
    evaluated_jobs only contains jobs that got a real decision — jobs whose API
    call failed (decision == "Error") are left out so they aren't written to the
    Sheet and aren't marked as seen; they'll simply be re-fetched and retried next
    run. If several calls in a row fail, we stop early instead of burning through
    the whole batch against a dead key/empty balance.
    """
    total = len(jobs)
    ok_jobs = []
    consecutive_errors = 0
    for i, job in enumerate(jobs, 1):
        title   = job.get("title", "")
        company = job.get("company", "")
        icon    = SOURCE_ICONS.get(job.get("source", ""), "🔔")
        print(f"  [{i}/{total}] {icon} 📄 {title} @ {company}...", end=" ", flush=True)
        ev    = evaluate_job(job, prompt_template)
        job["evaluation"] = ev
        badge = {"Apply": "✅", "Maybe": "🤔", "Skip": "❌", "Error": "⚠️"}.get(ev["decision"], "—")
        print(f"{badge} {ev['decision']}  |  {ev['reason']}")

        if ev["decision"] == "Error":
            consecutive_errors += 1
            if consecutive_errors >= CONSECUTIVE_ERROR_LIMIT:
                print(f"\n  ⚠️  {consecutive_errors} consecutive evaluation failures — "
                      f"aborting batch early ({total - i} job(s) not attempted). "
                      f"They'll be retried on the next run.")
                return ok_jobs, True
        else:
            consecutive_errors = 0
            ok_jobs.append(job)

        time.sleep(0.5)
    return ok_jobs, False
