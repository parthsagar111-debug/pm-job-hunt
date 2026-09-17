"""JobCollector — the filter/dedup bookkeeping shared by the Gulf and global feeds.

This replaced two near-identical inline loops, so these tests pin the behaviour
both feeds depended on, including the subtle case: a job already in the Sheet
must still register its fingerprint, or the same role found under another
country's search looks new and gets paid for again.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core_eval_hosted as core


def job(job_id, title="Product Manager", company="ACME", location="Berlin, Germany"):
    return {"job_id": f"li_{job_id}", "title": title, "company": company,
            "location": location, "url": f"https://www.linkedin.com/jobs/view/{job_id}"}


def test_keeps_a_new_job_and_tags_it():
    c = core.JobCollector(source="LinkedIn Gulf")
    assert c.add(job("1"), label="United Arab Emirates") is True
    assert c.jobs[0]["source"] == "LinkedIn Gulf"
    assert c.jobs[0]["searched"] == "United Arab Emirates"
    assert c.counts["kept"] == 1


def test_same_id_twice_is_ignored():
    c = core.JobCollector()
    c.add(job("1"))
    assert c.add(job("1")) is False
    assert len(c.jobs) == 1


def test_location_and_title_filters():
    c = core.JobCollector(location_ok=lambda loc: "Germany" in loc,
                          title_ok=lambda t: "Product" in t)
    assert c.add(job("1", location="Mumbai, India")) is False
    assert c.add(job("2", title="Software Engineer")) is False
    assert c.add(job("3")) is True
    assert c.counts["off_region"] == 1 and c.counts["wrong_title"] == 1


def test_same_role_in_two_cities_is_merged_with_locations_joined():
    c = core.JobCollector()
    c.add(job("1", company="Anthropic", title="Product Manager, Growth", location="Seattle, WA"))
    assert c.add(job("2", company="Anthropic", title="Product Manager, Growth",
                     location="New York, NY")) is False
    assert len(c.jobs) == 1
    assert c.jobs[0]["location"] == "Seattle, WA / New York, NY"
    assert c.counts["duplicate"] == 1


def test_role_already_in_the_sheet_is_skipped_by_fingerprint():
    key = core.job_fingerprint("ACME", "Product Manager", "Berlin, Germany")
    c = core.JobCollector(seen_keys={key})
    assert c.add(job("1")) is False
    assert c.counts["duplicate"] == 1


def test_known_url_blocks_the_same_role_from_another_country_search():
    """The regression this guards: URL-seen jobs must still claim their fingerprint,
    otherwise the same role from a second country's search reads as new."""
    c = core.JobCollector(seen_urls={"https://www.linkedin.com/jobs/view/1"})
    assert c.add(job("1", location="Berlin, Germany")) is False
    assert c.add(job("2", location="Berlin, Germany")) is False      # same company+title+country
    assert c.jobs == []
    assert c.counts["already_seen"] == 1


def test_company_cap_limits_one_employer_per_run():
    c = core.JobCollector(max_per_company=2)
    for i in range(5):
        c.add(job(str(i), title=f"Product Manager {i}"))
    assert len(c.jobs) == 2
    assert c.counts["company_cap"] == 3


def test_no_cap_when_zero():
    c = core.JobCollector(max_per_company=0)
    for i in range(5):
        c.add(job(str(i), title=f"Product Manager {i}"))
    assert len(c.jobs) == 5


def test_summary_lists_only_nonzero_reasons():
    c = core.JobCollector(location_ok=lambda loc: "Germany" in loc)
    c.add(job("1"))
    c.add(job("2", location="Mumbai, India"))
    summary = c.summary()
    assert "1 kept" in summary and "1 off region" in summary
    assert "company cap" not in summary
