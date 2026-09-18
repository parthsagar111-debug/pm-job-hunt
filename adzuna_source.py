"""
adzuna_source.py — a second job source for the global feed, via Adzuna's public API.

Dormant until ADZUNA_APP_ID and ADZUNA_APP_KEY exist: with no credentials it logs
one line and returns nothing, so the feed runs exactly as before.

Why it's worth having: LinkedIn caps every search near 1,000 results, so busy
countries are truncated. Adzuna aggregates other boards and returns clean JSON.

Its limitation, which matters here: the API returns a TRUNCATED description
(~200 chars), not the full JD. So an Adzuna job can rarely prove sponsorship
from its own text — its value is discovery plus the sponsor-licence column,
which is company-level and doesn't need the JD at all.

Free key: https://developer.adzuna.com/ — register, then add the two secrets to
the repo. Nothing else changes.
"""

import os
import time

import requests

# Adzuna is per-country. Codes for the feed's countries; anything it doesn't host
# simply 404s and is skipped with a note rather than failing the run.
COUNTRY_CODES = {
    "United States":  "us",
    "United Kingdom": "gb",
    "Germany":        "de",
    "Canada":         "ca",
    "Spain":          "es",
    "Netherlands":    "nl",
    "Australia":      "au",
    "France":         "fr",
    "Poland":         "pl",
    "Ireland":        "ie",
    # Sweden and Cyprus have no Adzuna site as far as the docs show — left out
    # deliberately so the run doesn't spend requests discovering that every day.
}

API = "https://api.adzuna.com/v1/api/jobs/{cc}/search/{page}"
RESULTS_PER_PAGE = 50
MAX_PAGES        = 5     # 250 jobs per country per run is plenty for a 24h window


def credentials() -> tuple[str, str]:
    return os.environ.get("ADZUNA_APP_ID", ""), os.environ.get("ADZUNA_APP_KEY", "")


def is_configured() -> bool:
    app_id, app_key = credentials()
    return bool(app_id and app_key)


def fetch(country: str, keyword: str = "product manager", days_old: int = 1) -> list[dict]:
    """Jobs for one country, mapped into the same dict shape the LinkedIn path produces.

    Returns [] on any failure — Adzuna is an extra source, never a reason for the
    run to fail.
    """
    app_id, app_key = credentials()
    if not (app_id and app_key):
        return []
    cc = COUNTRY_CODES.get(country)
    if not cc:
        return []

    out = []
    for page in range(1, MAX_PAGES + 1):
        params = {
            "app_id": app_id, "app_key": app_key,
            "results_per_page": RESULTS_PER_PAGE,
            "title_only": keyword,
            "max_days_old": days_old,
            "sort_by": "date",
            "content-type": "application/json",
        }
        try:
            r = requests.get(API.format(cc=cc, page=page), params=params, timeout=25)
            if r.status_code == 404:
                print(f"  [adzuna] {country}: not hosted by Adzuna — skipping")
                return []
            if r.status_code in (401, 403):
                print(f"  [adzuna] {country}: credentials rejected ({r.status_code}) — skipping Adzuna")
                return []
            r.raise_for_status()
            results = r.json().get("results", [])
        except Exception as e:
            print(f"  [adzuna] {country} page {page}: {type(e).__name__}: {e}")
            break

        for item in results:
            job_id = str(item.get("id", "")).strip()
            url    = item.get("redirect_url", "")
            if not (job_id and url):
                continue
            # Adzuna returns region-level strings ("London, UK", "Amsterdam,
            # Noord-Holland"). The country is appended because the sponsor-register
            # lookup and the India/Gulf exclusions both key on it — we know the
            # country here for certain, since it's the one we queried.
            where = (item.get("location") or {}).get("display_name", "")
            if country.lower() not in where.lower():
                where = f"{where}, {country}" if where else country
            out.append({
                "source":    "Adzuna",
                "job_id":    "az_" + job_id,
                "title":     (item.get("title") or "").replace("<strong>", "").replace("</strong>", ""),
                "company":   (item.get("company") or {}).get("display_name", ""),
                "location":  where,
                "posted":    (item.get("created") or "")[:10],
                "posted_dt": item.get("created", ""),
                "url":       url,
                # Truncated by the API — flagged so the caller doesn't mistake it for a full JD.
                "jd_text":   item.get("description", ""),
                "jd_is_snippet": True,
            })
        if len(results) < RESULTS_PER_PAGE:
            break
        time.sleep(0.5)   # Adzuna's own courtesy limit, unrelated to LinkedIn's
    return out
