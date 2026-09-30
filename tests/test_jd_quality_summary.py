"""The per-source JD-quality flag, which is the alarm that says a scraper broke.

It has to stay quiet on a small sample. Incremental runs routinely bring in one
job from a source, and on 2026-09-30 a single thin Hirist JD read as 100% unusable
and flagged the selectors one run after they had in fact been fixed.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pm_eval_hosted as pm

FLAG = "check this source's selectors"


def jobs(source: str, *qualities):
    return [{"source": source, "evaluation": {"features": {"jd_quality": q}}} for q in qualities]


def summary(capsys, job_list):
    pm._print_jd_quality_summary(job_list)
    return capsys.readouterr().out


def test_one_thin_job_is_not_a_broken_scraper(capsys):
    assert FLAG not in summary(capsys, jobs("Hirist/IIMJobs", "thin"))


def test_a_small_all_garbage_sample_stays_quiet(capsys):
    few = ["garbage"] * (pm.MIN_JOBS_TO_FLAG_SOURCE - 1)
    assert FLAG not in summary(capsys, jobs("IIMJobs", *few))


def test_a_real_sample_of_garbage_still_flags(capsys):
    """The 12:35 run: 29 IIMJobs JDs, every one a category menu."""
    out = summary(capsys, jobs("IIMJobs", *(["garbage"] * 29)))
    assert FLAG in out


def test_the_flag_fires_exactly_at_the_threshold(capsys):
    n = pm.MIN_JOBS_TO_FLAG_SOURCE
    assert FLAG in summary(capsys, jobs("IIMJobs", *(["garbage"] * n)))
    assert FLAG not in summary(capsys, jobs("IIMJobs", *(["garbage"] * (n - 1))))


def test_half_unusable_is_the_bar(capsys):
    assert FLAG in summary(capsys, jobs("Naukri", "ok", "ok", "ok", "thin", "thin", "garbage"))
    assert FLAG not in summary(capsys, jobs("Naukri", "ok", "ok", "ok", "ok", "thin", "garbage"))


def test_a_healthy_source_is_never_flagged(capsys):
    out = summary(capsys, jobs("LinkedIn", *(["ok"] * 108 + ["thin"] * 8)))
    assert FLAG not in out
    assert "108 ok" in out and "8 thin" in out


def test_each_source_is_judged_on_its_own_jobs(capsys):
    out = summary(capsys, jobs("LinkedIn", *(["ok"] * 20)) + jobs("IIMJobs", *(["garbage"] * 10)))
    lines = out.splitlines()
    linkedin = next(l for l in lines if "LinkedIn" in l)
    iimjobs  = next(l for l in lines if "IIMJobs" in l)
    assert FLAG not in linkedin
    assert FLAG in iimjobs


def test_no_jobs_prints_nothing(capsys):
    assert summary(capsys, []) == ""
