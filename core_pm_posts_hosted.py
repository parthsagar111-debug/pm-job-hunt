"""
core_pm_posts_hosted.py — PM hiring-announcement LinkedIn post feed (core logic)
=================================================================================
Scrapes LinkedIn organic FEED POSTS (not the formal Jobs section) where someone
announces they're hiring a Product Manager — via Apify's hosted scraping API,
not a logged-in Selenium session. No LinkedIn cookie, no account-ban risk.

Actor used: harvestapi/linkedin-post-search ("No Cookies" variant)
  - $1.50-2.00 per 1,000 posts scraped, no monthly subscription
  - Input:  searchQueries (list of phrases), postedLimit ('24h'), sortBy ('date')
  - Output: one JSON object per post — content, author{name,info,linkedinUrl},
            linkedinUrl, postedAt{date,timestamp}, engagement{...}
  - Docs:   https://apify.com/harvestapi/linkedin-post-search

No AI evaluation — like Chief of Staff/EIR, this is a raw deduped feed for
manual review, not an Apply/Maybe/Skip judgment against a candidate profile.

Required environment variable:
  APIFY_API_TOKEN   — from https://console.apify.com/settings/integrations
"""

import os
import re
from datetime import timedelta

from apify_client import ApifyClient

APIFY_ACTOR_ID = "harvestapi/linkedin-post-search"

# The four phrases agreed on — each is a literal LinkedIn search query (LinkedIn
# caps each query at 85 characters; all of these are well under that).
SEARCH_QUERIES = [
    "hiring a product manager",
    "looking for a product manager",
    "product manager role",
    "product manager opening",
]

# Light relevance filter — the search queries are already targeted, but this
# catches queries LinkedIn's search matched loosely (e.g. stemming/synonyms).
_PM_PATTERN = re.compile(r'product\s+manager', re.I)

# Cap per-author, so one prolific recruiter/agency doesn't fill the whole run.
MAX_PER_AUTHOR = 3


def is_relevant_post(content: str) -> bool:
    """True if the post text actually mentions 'product manager'."""
    return bool(content) and bool(_PM_PATTERN.search(content))


def _clean_snippet(text: str, max_len: int = 180) -> str:
    if not text:
        return ""
    snippet = " ".join(text.split())  # collapse newlines/whitespace
    return snippet[:max_len] + ("…" if len(snippet) > max_len else "")


def fetch_pm_posts(api_token: str, posted_limit: str = "24h", max_posts_per_query: int = 40) -> list:
    """
    Run the Apify actor once (covers all SEARCH_QUERIES in a single run) and
    return a list of job-shaped dicts compatible with sheets_writer.save_raw_jobs:
      { "title": <post snippet>, "company": <author name>,
        "location": <author headline>, "source": "LinkedIn Post", "url": <post url> }

    Raises on Apify client/actor errors — caller decides how to handle (this
    mirrors the "don't silently write nothing" philosophy used elsewhere: a
    failed run should show up as a failed GitHub Actions run, not a quiet 0).
    """
    client = ApifyClient(api_token)

    run_input = {
        "searchQueries": SEARCH_QUERIES,
        "postedLimit": posted_limit,
        "sortBy": "date",
        "maxPosts": max_posts_per_query,
        "scrapeReactions": False,
        "scrapeComments": False,
    }

    print(f"  [apify] Running {APIFY_ACTOR_ID} — {len(SEARCH_QUERIES)} quer(ies), postedLimit={posted_limit}")
    run = client.actor(APIFY_ACTOR_ID).call(run_input=run_input, wait_duration=timedelta(seconds=300))

    dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        raise RuntimeError("Apify run finished but returned no defaultDatasetId")

    author_counts = {}
    jobs = []

    for item in client.dataset(dataset_id).iterate_items():
        if item.get("type") != "post":
            continue

        content = item.get("content", "") or ""
        if not is_relevant_post(content):
            continue

        url = item.get("linkedinUrl", "")
        if not url:
            continue

        author = item.get("author") or {}
        author_name = author.get("name", "") or "Unknown"
        author_info = author.get("info", "") or ""

        if author_counts.get(author_name, 0) >= MAX_PER_AUTHOR:
            continue
        author_counts[author_name] = author_counts.get(author_name, 0) + 1

        jobs.append({
            "title": _clean_snippet(content),
            "company": author_name,
            "location": author_info,
            "source": "LinkedIn Post",
            "url": url,
        })

    print(f"  [apify] {len(jobs)} relevant post(s) after filtering/author-cap")
    return jobs
