"""
core_cos_eir_hosted.py — scraper for Chief of Staff / EIR roles (no AI evaluation)
====================================================================================
Sources: LinkedIn · Naukri · Hirist · IIMJobs
Output:  Google Sheets, single "Listings" tab — dedicated COS_EIR_SPREADSHEET_ID sheet
Notify:  ntfy push notification after each run

Adapted from core_eval_hosted.py (the PM Eval scraper), with the Claude evaluation
step removed entirely: Apply/Maybe/Skip judgment against a candidate profile doesn't
map well onto Chief of Staff / EIR roles, so this just scrapes, dedupes, and writes a
plain feed of matching listings for manual review. No ANTHROPIC_API_KEY needed, no
per-job JD fetch needed (that was only done to feed the evaluator), no API cost.

Target titles: "Chief of Staff" and "Entrepreneur in Residence" (EIR).

NOTE on source coverage: LinkedIn and Naukri are keyword-search based, so they search
these exact titles directly and are reliable regardless of niche title. Hirist and
IIMJobs are category-browse based — the category slugs below are a best-effort guess
("chief-of-staff-jobs") since neither platform is confirmed to have a dedicated
category for these titles. If the category doesn't exist, those sources will simply
return 0 hits (existing timeout/empty handling already tolerates this) — it will not
break the run. LinkedIn + Naukri carry the real coverage for this search.
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

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
TOP_N                  = 100
SEARCH_KEYWORD         = "Chief of Staff"   # primary/default keyword (Naukri slug, etc.)
SEARCH_LOCATION        = "India"
BASE_DIR               = os.path.dirname(os.path.abspath(__file__))
CHECK_INTERVAL_MINUTES = 30   # unused on hosted but kept for import compat

# Both target titles — LinkedIn and Naukri are searched with each of these separately
# since those platforms search literal keyword phrases, not browsable categories.
TARGET_KEYWORDS = [
    "Chief of Staff",
    "Entrepreneur in Residence",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ─────────────────────────────────────────────
# ROLE FILTER
# ─────────────────────────────────────────────
ROLE_KEYWORDS = [
    "chief of staff",
    "entrepreneur in residence",
    "chief-of-staff",
]

# "eir" is a short acronym — require it as a standalone token to avoid false positives
# (e.g. matching inside unrelated words) rather than a plain substring check.
_EIR_PATTERN = re.compile(r'(?<![a-z])eir(?![a-z])', re.I)

def is_target_role(title: str) -> bool:
    t = title.lower().strip()
    if any(kw in t for kw in ROLE_KEYWORDS):
        return True
    if _EIR_PATTERN.search(t):
        return True
    return False

# Backwards-compat alias in case anything imports the old PM-style name
is_pm_role = is_target_role

# ─────────────────────────────────────────────
# TIME HELPERS  (identical to core_eval_hosted.py — role-agnostic)
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

# ─────────────────────────────────────────────
# SELENIUM DRIVER  (identical to core_eval_hosted.py — role-agnostic)
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
NAUKRI_DEBUG = os.path.join(BASE_DIR, "cos_eir_naukri_debug.html")
HIRIST_DEBUG = os.path.join(BASE_DIR, "cos_eir_hirist_debug.html")

# Best-effort guess — verify on hirist.tech; if wrong, this source just returns 0 hits.
HIRIST_CATEGORY_MAP = {
    "chief of staff":            "chief-of-staff-jobs",
    "entrepreneur in residence": "chief-of-staff-jobs",  # no dedicated category likely; closest guess
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
            if not is_target_role(title): continue
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

def fetch_linkedin_multi(keyword=SEARCH_KEYWORD, time_range="24h") -> list:
    """keyword param ignored — uses TARGET_KEYWORDS list internally."""
    import random
    all_hits = []
    seen_ids = set()
    for kw in TARGET_KEYWORDS:
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
        delay = random.uniform(8, 14)
        print(f"    ⏳ waiting {delay:.1f}s before next keyword...")
        time.sleep(delay)
    return all_hits

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
                if not is_target_role(title): continue
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

def fetch_naukri_multi(keyword=SEARCH_KEYWORD, time_range="24h") -> list:
    """keyword param ignored — Naukri is keyword-search based, so loop TARGET_KEYWORDS."""
    all_hits = []
    seen_ids = set()
    for kw in TARGET_KEYWORDS:
        try:
            jobs = fetch_naukri(kw, time_range)
            for job in jobs:
                if job["job_id"] not in seen_ids:
                    seen_ids.add(job["job_id"])
                    all_hits.append(job)
        except Exception as e:
            print(f"  [Naukri] error for '{kw}': {e}")
        time.sleep(2)
    return all_hits

def fetch_hirist(keyword=SEARCH_KEYWORD, time_range="24h"):
    kw_lo    = keyword.lower()
    category = HIRIST_CATEGORY_MAP.get(kw_lo, "chief-of-staff-jobs")
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
            print(f"  [Hirist] Timeout (category may not exist) — debug saved to {HIRIST_DEBUG}"); return []
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
                if not title or not is_target_role(title): continue
                card = link.find_parent("li") or link.find_parent("div")
                company, location, posted = "N/A", "India", "Recent"
                if card:
                    dm = re.match(r"^(.+?)\s+-\s+", title)
                    if dm:
                        cand = dm.group(1).strip()
                        if cand.lower().split()[0] not in {"senior", "associate", "principal",
                                "group", "lead", "chief", "entrepreneur"}:
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
    if time_range == "24h":
        hits = [j for j in hits if within_24hrs(j)]
    jobs = sort_newest_first(hits)[:TOP_N]
    print(f"  [Hirist] {len(hits)} found → top {len(jobs)}")
    return jobs

def fetch_iimjobs(keyword=SEARCH_KEYWORD, time_range="24h"):
    """Scrape IIMJobs for Chief of Staff / EIR roles — best-effort category guess."""
    urls_to_try = [
        "https://www.iimjobs.com/k/chief-of-staff-jobs",
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
                    if not is_target_role(title): continue

                    card = (link.find_parent("article") or
                            link.find_parent("li") or
                            link.find_parent("div"))
                    company, location, posted = "N/A", "India", "Recent"

                    if " - " in title:
                        parts = title.split(" - ", 1)
                        if len(parts[0]) < 40 and not is_target_role(parts[0]):
                            company = parts[0].strip()
                            title   = parts[1].strip()

                    if card:
                        ct = card.get_text(" ", strip=True)
                        if company == "N/A":
                            for tag in card.find_all(["strong", "b", "span", "p"]):
                                txt = tag.get_text(strip=True)
                                if 2 < len(txt) < 50 and txt not in (title,) and not is_target_role(txt):
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
            debug_path = os.path.join(BASE_DIR, "cos_eir_iimjobs_debug.html")
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(driver.page_source)
            print(f"  [IIMJobs] 0 hits (category may not exist) — debug saved to {debug_path}")

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
SOURCES = [
    ("LinkedIn",       fetch_linkedin_multi),
    ("Naukri",         fetch_naukri_multi),
    ("Hirist/IIMJobs", fetch_hirist),
    ("IIMJobs",        fetch_iimjobs),
]

SOURCE_ICONS = {
    "LinkedIn":       "🔵",
    "Naukri":         "🟠",
    "Hirist/IIMJobs": "🟣",
    "IIMJobs":        "🟤",
}

# No evaluation step by design — see module docstring. Scraping ends at SOURCES above;
# cos_eir_hosted.py dedupes and writes matching listings straight to the sheet.
