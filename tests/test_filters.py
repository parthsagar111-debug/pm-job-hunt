"""Pure-function tests: title filters, locations, fingerprints, time windows.

These are the rules that decide what gets evaluated and what gets deduped, so a
silent regression here changes what lands in the Sheet without anything failing.
Cases marked with a company name are real listings from the 2026-09-17 runs.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core_eval_hosted as core
import global_visa_hosted as gv
import gulf_eval_hosted as gulf


# ── core.is_pm_role — India feed, deliberately broad (admits growth/category/BDM)
@pytest.mark.parametrize("title,expected", [
    ("Senior Product Manager", True),
    ("Product Owner", True),
    ("Head of Product", True),
    ("Growth Manager", True),
    ("Category Manager", True),
    ("Software Engineer", False),
    ("Data Analyst", False),
])
def test_is_pm_role(title, expected):
    assert core.is_pm_role(title) is expected


# ── global.is_product_role — product roles only, no junior, no design/engineering
@pytest.mark.parametrize("title,expected", [
    ("Senior Product Manager", True),
    ("Product Owner - Payments", True),
    ("VP, Product Management", True),
    ("Chief Product Officer", True),
    ("Group PM, Growth", True),
    ("Product Marketing Manager", True),
    ("Director of Product Design", False),   # Hiive: slipped through the probe
    ("Product Designer", False),
    ("Senior Product Engineer", False),
    ("Associate Product Manager", False),
    ("Product Management Intern", False),
    ("Junior Product Owner", False),
    ("Business Development Manager", False),
    ("Category Manager", False),             # in scope for India, not for global
    ("UX Design Lead", False),
])
def test_is_product_role(title, expected):
    assert gv.is_product_role(title) is expected


# ── location handling
@pytest.mark.parametrize("location,expected", [
    ("Dubai, United Arab Emirates", True),
    ("Riyadh Region", True),
    ("Doha Metropolitan Area", True),
    ("Muscat, Oman", True),
    ("Bucharest, Romania", False),   # "Romania" contains "oman"
    ("Mumbai, India", False),
    ("Remote", False),
])
def test_is_gulf_location(location, expected):
    assert gulf.is_gulf_location(location) is expected


@pytest.mark.parametrize("location,expected", [
    ("Seattle, WA", "united states"),
    ("New York, NY", "united states"),
    ("Austin, Texas", "united states"),
    ("Dubai, United Arab Emirates", "united arab emirates"),
    ("Berlin, Germany", "germany"),
    ("Riyadh Region", "riyadh region"),
    ("Seattle, WA / Toronto, Ontario, Canada", "united states"),   # merged row: first wins
    ("", ""),
])
def test_location_country(location, expected):
    assert core.location_country(location) == expected


def test_fingerprint_merges_same_role_across_cities_in_one_country():
    # Anthropic's PM Growth was posted in Seattle, SF and NY on the same day.
    a = core.job_fingerprint("Anthropic", "Product Manager, Growth", "Seattle, WA")
    b = core.job_fingerprint("anthropic", "Product   Manager,  Growth", "New York, NY")
    assert a == b


def test_fingerprint_keeps_countries_apart():
    # N26 posted the same title in Berlin and Barcelona — different work locations.
    de = core.job_fingerprint("N26", "Product Manager", "Berlin, Germany")
    es = core.job_fingerprint("N26", "Product Manager", "Barcelona, Catalonia, Spain")
    assert de != es


def test_fingerprint_ignores_punctuation_and_case():
    assert (core.job_fingerprint("Stryker", "Product Manager, Medical Devices - META", "Dubai, UAE")
            == core.job_fingerprint("STRYKER", "Product Manager  Medical Devices – META", "Dubai, UAE"))


# ── time windows
def test_within_24hrs_uses_iso_timestamp_when_present():
    from datetime import datetime, timedelta, timezone
    fresh = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    stale = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    assert core.within_24hrs({"posted_dt": fresh}) is True
    assert core.within_24hrs({"posted_dt": stale}) is False


@pytest.mark.parametrize("posted,expected", [
    ("2 hours ago", True),
    ("23 hours ago", True),
    ("3 days ago", False),
    ("1 week ago", False),
])
def test_within_24hrs_parses_relative_strings(posted, expected):
    assert core.within_24hrs({"posted": posted}) is expected


def test_undated_jobs_pass_the_window():
    # Documents current (lenient) behaviour: Hirist/IIMJobs rows often have no date,
    # and they are let through rather than dropped.
    assert core.within_24hrs({"posted": "Recent"}) is True
    assert core.within_24hrs({}) is True


def test_sort_newest_first_prefers_timestamps():
    jobs = [
        {"title": "old", "posted_dt": "2026-09-01T10:00:00+00:00"},
        {"title": "new", "posted_dt": "2026-09-17T10:00:00+00:00"},
    ]
    assert [j["title"] for j in core.sort_newest_first(jobs)] == ["new", "old"]
