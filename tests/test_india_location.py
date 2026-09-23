"""India-only filter for the PM Eval feed.

On 2026-09-23 23:39 LinkedIn's "location=India" search padded a thin page with worldwide
listings and the India feed (which had no location filter) saved 55 of them. These pin the
locations from that run, the ways Indian listings really spell their location, and the edge
cases where a bare place name is ambiguous.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core_eval_hosted as core
from core_eval_hosted import is_india_location


@pytest.mark.parametrize("loc", [
    "Ho Chi Minh City, Vietnam",
    "Ho Chi Minh City, Ho Chi Minh City, Vietnam",
    "Ho Chi Minh City Metropolitan Area",
    "Cologne, North Rhine-Westphalia, Germany",
    "Paris, Île-de-France, France",
    "Dubai, Dubai, United Arab Emirates",
    "Singapore, Singapore",
    "Colombo, Western Province, Sri Lanka",
    "Seoul, Seoul, South Korea",
    "Toronto, Ontario, Canada",
    "Seattle, WA / San Francisco, CA",
    "Austin, TX",
    "Portugal",
    "Argentina",
    "Helsinki Metropolitan Area",
    "Indianapolis, Indiana, United States",
    "Hyderabad, Sindh, Pakistan",          # an Indian-sounding city, but the country vetoes it
    "Dhaka, Bangladesh",
])
def test_drops_locations_outside_india(loc):
    assert is_india_location(loc) is False


@pytest.mark.parametrize("loc", [
    "Bengaluru, Karnataka, India",
    "India",
    "Hyderabad, Telangana, India",
    "Mumbai Metropolitan Region",
    "Greater Delhi Area",
    "Pune/Pimpri-Chinchwad Area",
    "Greater Hyderabad Area",
    "Gurugram",
    "Bangalore",
    "Hybrid - Hyderabad",
    "Mumbai(Andheri)",
    "Pune, Lucknow, Mumbai (All Areas)",
    "Bengaluru(Manayata Tech Park)",
    "Kharghar, Maharashtra",
    "Noida, Uttar Pradesh",
    "Pan India",
    "Remote - India",
])
def test_keeps_indian_locations_however_they_are_spelled(loc):
    assert is_india_location(loc) is True


@pytest.mark.parametrize("loc", ["", None, "Remote", "Hybrid", "Work from home", "Multiple Locations"])
def test_keeps_locations_that_name_no_country(loc):
    # unknown is kept, not guessed: dropping a real Indian job is worse than evaluating one extra
    assert is_india_location(loc) is True


def test_word_boundaries_do_not_fool_the_filter():
    assert is_india_location("Indiana, United States") is False          # 'india' inside 'Indiana'
    assert is_india_location("Dakota, United States") is False           # 'kota' inside 'Dakota'
    assert is_india_location("Angola") is False                          # 'goa' inside 'Angola'


def test_collector_drops_off_region_jobs_and_counts_them():
    c = core.JobCollector(max_per_company=2, location_ok=is_india_location)
    jobs = [
        ("1", "Bengaluru, Karnataka, India"),
        ("2", "Ho Chi Minh City, Vietnam"),
        ("3", "Mumbai Metropolitan Region"),
        ("4", "Cologne, North Rhine-Westphalia, Germany"),
    ]
    for i, (jid, loc) in enumerate(jobs):
        c.add({"job_id": f"li_{jid}", "title": "Senior Product Manager", "company": f"Co{i}",
               "location": loc, "url": f"https://www.linkedin.com/jobs/view/{jid}"})
    assert [j["job_id"] for j in c.jobs] == ["li_1", "li_3"]
    assert c.counts["off_region"] == 2
    assert "2 off region" in c.summary()
