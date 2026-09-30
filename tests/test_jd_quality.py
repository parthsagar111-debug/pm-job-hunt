"""JD quality: what counts as garbage, and the removal of the largest-<div> fallback.

The fallback used to return the biggest text block on the page when no JD selector
matched. On IIMJobs/Hirist that is the category navigation, which was then classified
as though it were the job description.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core_eval_hosted as core


IIMJOBS_MENU = (
    "Banking & FinanceFinance & AccountsBanking, Insurance & Financial Services"
    "IT & SystemsSales & MarketingHuman ResourcesConsultingLegalOperations"
    "Supply Chain & LogisticsMarketing & CommunicationsStrategy & Planning"
    "Data Science & AnalyticsProduct ManagementGeneral Management"
)

# A real Naukri posting: bullet list, no full stops anywhere.
BULLET_ONLY_JD = "\n".join([
    "Roles and Responsibilities",
    "- Own the product roadmap for the consumer checkout experience",
    "- Partner with engineering and design to ship A/B tested funnel improvements",
    "- Define success metrics and instrument them in Mixpanel and GA4",
    "- Run discovery interviews with kirana merchants and synthesise findings",
    "- Drive weekly prioritisation with RICE and communicate trade-offs to leadership",
    "Desired Candidate Profile",
    "- 6 to 10 years of product management experience in consumer internet",
    "- Hands-on SQL and cohort analysis",
    "- Experience with payments or checkout is a plus",
    "Perks and Benefits",
    "- Health insurance, flexible working, annual learning budget",
])

REAL_JD = (
    "About the role. We are looking for a Senior Product Manager to own our search and "
    "discovery experience. You will work with engineering, design and data science to "
    "improve relevance, ranking and personalisation across web and app. "
) * 12  # ~2,000 chars


def test_category_menu_is_garbage():
    assert core.is_garbage_jd(IIMJOBS_MENU) is True


def test_real_jd_is_not_garbage():
    assert len(REAL_JD) > 2000
    assert core.is_garbage_jd(REAL_JD) is False


def test_bullet_only_jd_is_not_garbage():
    """No sentence punctuation, but a perfectly real posting — the punctuation rule was
    dropped for exactly this case (Parth's call, 2026-09-30)."""
    assert "." not in BULLET_ONLY_JD.replace("...", "")
    assert core.is_garbage_jd(BULLET_ONLY_JD) is False


@pytest.mark.parametrize("text", ["", "   ", "Apply now", "Senior Product Manager - Mumbai"])
def test_short_or_empty_is_garbage(text):
    assert core.is_garbage_jd(text) is True


def test_boundary_at_min_chars():
    assert core.is_garbage_jd("x" * (core.MIN_JD_CHARS - 1)) is True
    assert core.is_garbage_jd("x" * core.MIN_JD_CHARS) is False


def test_menu_detected_regardless_of_whitespace():
    spaced = IIMJOBS_MENU.replace("&", " & ").replace("Finance", " Finance ")
    assert core.is_garbage_jd(spaced) is True


# ── the removed fallback
def test_no_selector_match_returns_empty_not_page_text(monkeypatch):
    """Previously this returned the largest <div> — i.e. the nav menu.

    Naukri is the last source that reads a rendered page; IIMJobs and Hirist now go
    through the Info Edge API instead (tests/test_infoedge_jd.py).
    """
    page = (
        "<html><title>Product Manager Jobs</title><body>"
        f"<div class='nav'>{IIMJOBS_MENU}</div>"
        "<div class='footer'>About us. Contact. Careers.</div>"
        "</body></html>"
    )

    class FakeDriver:
        page_source = page
        current_url = "https://www.naukri.com/job-listings-product-manager-123456"
        def get(self, url): pass
        def quit(self): pass

    monkeypatch.setattr(core, "make_driver", lambda: FakeDriver())
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    out = core.fetch_jd_text({"url": FakeDriver.current_url, "source": "Naukri"})
    assert out == ""
    assert "Banking" not in out


def test_matching_selector_still_returns_the_description(monkeypatch):
    page = f"<html><body><div class='job-description-text'>{REAL_JD}</div></body></html>"

    class FakeDriver:
        page_source = page
        current_url = "https://www.naukri.com/job-listings-product-manager-123456"
        def get(self, url): pass
        def quit(self): pass

    monkeypatch.setattr(core, "make_driver", lambda: FakeDriver())
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    out = core.fetch_jd_text({"url": FakeDriver.current_url, "source": "Naukri"})
    assert "Senior Product Manager" in out
    assert core.is_garbage_jd(out) is False
