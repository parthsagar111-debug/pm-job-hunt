"""IIMJobs / Hirist JD fetching, offline.

Both sites are Next.js shells: the description is never in the served HTML, it is
fetched afterwards by the page. Scraping the rendered DOM returned the category nav
instead, and on 2026-09-30 all 39 IIMJobs/Hirist JDs in one run came back garbage.
These tests pin the API path that replaced it.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core_eval_hosted as core

IIM_URL    = "https://www.iimjobs.com/j/bigbasket-associate-product-manager-1737122"
HIRIST_URL = "https://www.hirist.tech/j/telus-digital-product-manager-saas-domain-1676036?ref=cl_br&jobPos=1"


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload


def _stub(monkeypatch, payload, status=200):
    """Captures the request the fetcher makes and returns a canned reply."""
    seen = {}

    def fake_get(url, **kw):
        seen["url"] = url
        seen["headers"] = kw.get("headers", {})
        return _Resp(payload, status)

    monkeypatch.setattr(core, "_LI_GET", fake_get)
    return seen


def test_job_code_comes_from_the_url_and_query_strings_are_ignored(monkeypatch):
    seen = _stub(monkeypatch, {"data": {"introText": "<p>" + "Own the roadmap. " * 30 + "</p>"}})
    core.fetch_jd_infoedge(HIRIST_URL)
    assert seen["url"] == "https://gladiator.hirist.tech/job/detail?jobcode=1676036"


def test_each_site_calls_its_own_host(monkeypatch):
    seen = _stub(monkeypatch, {"data": {"introText": "<p>text</p>"}})
    core.fetch_jd_infoedge(IIM_URL)
    assert seen["url"].startswith("https://gladiator.iimjobs.com/job/detail")
    assert seen["headers"].get("Referer") == "https://www.iimjobs.com/"


def test_intro_text_is_html_and_comes_back_as_text(monkeypatch):
    _stub(monkeypatch, {"data": {"introText":
        "<p><b>Responsibilities : </b><br/><br/>- Own search ranking<br/>- Define PRDs</p>"}})
    jd = core.fetch_jd_infoedge(IIM_URL)
    assert "<" not in jd
    assert "Responsibilities" in jd and "Own search ranking" in jd


def test_a_url_with_no_job_code_is_not_fetched(monkeypatch):
    _stub(monkeypatch, {"data": {"introText": "<p>never reached</p>"}})
    assert core.fetch_jd_infoedge("https://www.iimjobs.com/k/product-management-jobs") == ""


def test_an_api_error_returns_nothing_rather_than_a_guess(monkeypatch):
    _stub(monkeypatch, {}, status=503)
    assert core.fetch_jd_infoedge(IIM_URL) == ""


def test_a_missing_description_returns_nothing(monkeypatch):
    _stub(monkeypatch, {"data": {"introText": ""}})
    assert core.fetch_jd_infoedge(IIM_URL) == ""


def test_a_jd_less_posting_is_unverified_not_skipped(monkeypatch):
    """The whole point: no JD must never turn into a confident rejection."""
    from decision_rules import UNVERIFIED_PREFIX, decide
    _stub(monkeypatch, {"data": {"introText": ""}})
    jd = core.fetch_jd_text({"source": "IIMJobs", "url": IIM_URL})
    assert jd == ""
    assert core.is_garbage_jd(jd)
    assert decide({"jd_quality": "garbage", "is_pm_role": False}) == ("Maybe", UNVERIFIED_PREFIX)


@pytest.mark.parametrize("source", ["IIMJobs", "Hirist/IIMJobs"])
def test_neither_source_starts_a_browser_any_more(source, monkeypatch):
    _stub(monkeypatch, {"data": {"introText": "<p>" + "Real JD text. " * 40 + "</p>"}})

    def explode():
        raise AssertionError("Selenium must not be used for Info Edge JDs")

    monkeypatch.setattr(core, "make_driver", explode)
    jd = core.fetch_jd_text({"source": source, "url": IIM_URL})
    assert len(jd) > core.MIN_JD_CHARS and not core.is_garbage_jd(jd)
