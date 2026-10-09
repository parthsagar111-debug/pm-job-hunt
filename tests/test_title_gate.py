"""What the PM Eval feed is willing to collect.

The gate used to admit growth, strategy, e-commerce and product-marketing titles on
the theory that the classifier would reject them. It did reject them — correctly,
with is_pm_role=false — but each one still cost a JD fetch and an API call, and one
reached Maybe when a later rule second-guessed that rejection ("Growth Manager @
FRND", core, fit 72). Across 1,705 classified jobs, tightening the gate removes 15
rows, all 15 already called non-PM by the model and none of them an Apply.

Gulf and Global are unaffected: they gate on is_pm_role, which is untouched.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core_eval_hosted as core

# Real titles from the 2026-10-09 runs that should no longer be collected.
NOT_COLLECTED = [
    "Growth Manager",
    "Growth Manager - FinTech",
    "Marketplace Growth Manager",
    "Ecommerce Manager",
    "Product Marketing Manager",
    "Sr Product Marketing Manager, Agentic AI",
    "Group Product Marketing Manager",
    "Product Marketing Manager  - (BESS / Energy Storage / Solar Inverters)",
    "Info Edge India is Hiring Product Marketing Manager || Noida",
    "Head of Growth",
    "D2C Manager",
    "Strategy Manager",
    "Head of Strategy",
    "Head of E-commerce",
]

# Real titles that must keep coming through.
STILL_COLLECTED = [
    "Product Manager",
    "Senior Product Manager",
    "Group Product Manager - Supply Chain & Operations Domain",
    "Principal Product Manager",
    "Staff Product Manager",
    "Product Owner",
    "Product Lead - Savings Bank Account - FinTech",
    "Head of Product",
    "Director of Product",
    "VP of Product",
    "Chief Product Officer",
    "Product Growth Manager",            # Apply, fit 92 — the reason "product growth" stays
    "Product Strategy Manager",
    "Product Management & Strategy - Manufacturing",
    "Head of AI Strategy & Product Management",
    "APM - toolkits",
    "Associate Product Manager",
    "Senior PM, Payments",
    "Technical PM",
]


@pytest.mark.parametrize("title", NOT_COLLECTED)
def test_a_non_product_title_is_not_collected(title):
    assert core.is_pm_eval_role(title) is False


@pytest.mark.parametrize("title", STILL_COLLECTED)
def test_a_product_title_is_collected(title):
    assert core.is_pm_eval_role(title) is True


def test_product_marketing_is_out_but_product_growth_is_in():
    """The distinction that cost the most to get right."""
    assert core.is_pm_eval_role("Product Marketing Manager") is False
    assert core.is_pm_eval_role("Product Growth Manager") is True


def test_every_keyword_names_a_product_role():
    """A keyword that doesn't mention product (or a pm abbreviation) is how growth and
    strategy titles got in last time."""
    for kw in core.PM_EVAL_TITLE_KEYWORDS:
        assert "product" in kw or kw.split()[-1] in ("pm", "apm"), kw


def test_the_gulf_and_global_gate_is_untouched():
    """Those feeds use is_pm_role, and pinning it was an explicit requirement."""
    assert core.is_pm_role("Growth Manager") is True
    assert core.is_pm_eval_role("Growth Manager") is False
