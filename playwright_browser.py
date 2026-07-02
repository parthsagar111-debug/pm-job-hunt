"""
playwright_browser.py — shared Playwright browser session for JD fetching
==========================================================================
All three scripts (core_eval.py, core_eval_linkedin.py, linkedin_global.py)
import this module to fetch LinkedIn job description pages.

Why Playwright instead of curl_cffi for JD fetching:
- curl_cffi handles the TLS fingerprint but LinkedIn still rate-limits
  when it sees many individual JD page requests in quick succession.
- Playwright runs one real persistent browser session reused across all
  JD fetches in a run — LinkedIn sees one human browsing, not N separate
  HTTP connections. Much harder to rate-limit.
- Playwright is headless (no visible window) but indistinguishable from
  a real Chrome session at the network level.

Usage (imported by other scripts — don't run directly):
    from playwright_browser import fetch_jd_playwright

    text = fetch_jd_playwright("https://www.linkedin.com/jobs/view/...")
"""

import time
import random
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────
# LAZY BROWSER SINGLETON
# One browser instance is created on first call and reused for the
# entire run — avoids the startup overhead of launching a new browser
# for every single JD fetch.
# ─────────────────────────────────────────────
_playwright   = None
_browser      = None
_context      = None

def _get_context():
    """Return the shared browser context, creating it on first call."""
    global _playwright, _browser, _context
    if _context is not None:
        return _context

    from playwright.sync_api import sync_playwright
    _playwright = sync_playwright().start()
    _browser    = _playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ]
    )
    _context = _browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
        locale="en-US",
    )
    # Mask webdriver flag — same trick as the old Selenium setup
    _context.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    )
    return _context


def fetch_jd_playwright(url: str, retries: int = 2) -> str:
    """
    Fetch a LinkedIn job description page via the shared Playwright browser.
    Returns extracted description text (up to 4000 chars), or "" on failure.
    """
    for attempt in range(retries + 1):
        page = None
        try:
            ctx  = _get_context()
            page = ctx.new_page()

            # Random human-ish delay before each page load
            time.sleep(random.uniform(1.5, 3.5))

            page.goto(url, timeout=20000, wait_until="domcontentloaded")

            # Wait for the job description container to appear
            try:
                page.wait_for_selector(
                    "div.description__text, div.show-more-less-html__markup, section.description",
                    timeout=8000
                )
            except Exception:
                pass  # selector might not exist — still try to parse what loaded

            html = page.content()
            soup = BeautifulSoup(html, "html.parser")

            for sel in [
                "div.description__text",
                "div.show-more-less-html__markup",
                "section.description",
                "div.job-description",
            ]:
                el = soup.select_one(sel)
                if el:
                    txt = el.get_text(separator="\n", strip=True)
                    if len(txt) > 100:
                        return txt[:4000]

        except Exception as e:
            if attempt < retries:
                wait = 8 * (attempt + 1) + random.uniform(0, 4)
                print(f"  ⚠️  JD fetch retry {attempt + 1} in {wait:.0f}s... ", end="", flush=True)
                time.sleep(wait)
            # else fall through and return ""
        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass

    return ""


def close_browser():
    """
    Call this at the end of a run to cleanly shut down the browser.
    Not strictly required (process exit cleans up) but good practice.
    """
    global _playwright, _browser, _context
    try:
        if _context:
            _context.close()
        if _browser:
            _browser.close()
        if _playwright:
            _playwright.stop()
    except Exception:
        pass
    finally:
        _playwright = _browser = _context = None
