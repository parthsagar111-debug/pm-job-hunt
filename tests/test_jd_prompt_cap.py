"""How much of a JD reaches the model, and which parts.

The cap came down from 10,000 to 4,000 characters on 2026-10-27 to cut input cost
(fresh input is 34% of inference spend). At 10,000 only 10 of 512 JDs were truncated,
so a head-only slice was harmless. At 4,000 it fires on most of them — the measured
average is 4,549 and p90 is 7,706 — so the tail has to survive: requirements, the
stated experience range and education lines live at the end of most JDs, and they are
what years_min/years_max and hard_blockers are read from.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core_eval_hosted as core


def test_the_cap_is_four_thousand():
    assert core.JD_PROMPT_MAX_CHARS == 4_000


@pytest.mark.parametrize("length", [0, 1, 500, 3_999, 4_000])
def test_a_jd_within_the_cap_is_sent_whole(length):
    jd = "x" * length
    assert core.jd_for_prompt(jd) == jd


@pytest.mark.parametrize("length", [4_001, 7_706, 12_315, 45_000])
def test_a_long_jd_never_exceeds_the_cap(length):
    assert len(core.jd_for_prompt("x" * length)) == core.JD_PROMPT_MAX_CHARS


def test_both_ends_of_a_long_jd_survive():
    """A head-only slice would drop the requirements block on any JD over the cap."""
    jd = ("ROLE: Senior Product Manager. Own the checkout roadmap. "
          + "filler. " * 1_000
          + "REQUIREMENTS: 6-9 years in product management. "
            "Bachelor's degree in engineering or a related field.")
    out = core.jd_for_prompt(jd)
    assert "Own the checkout roadmap" in out          # head kept
    assert "6-9 years in product management" in out   # tail kept
    assert "Bachelor's degree" in out                 # the education line survives
    assert len(out) == core.JD_PROMPT_MAX_CHARS


def test_the_elision_is_visible_to_the_model():
    """It must be able to tell that something was removed, so it doesn't read the
    join as continuous prose."""
    out = core.jd_for_prompt("a" * 9_000)
    assert "omitted" in out


def test_the_tail_is_a_meaningful_slice_not_a_token():
    assert 500 <= core.JD_PROMPT_TAIL_CHARS <= core.JD_PROMPT_MAX_CHARS // 2


def test_a_none_jd_does_not_crash():
    assert core.jd_for_prompt(None) == ""


def test_the_prompt_builder_uses_the_helper(monkeypatch):
    """Guards against a future edit slicing jd_text inline again."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub")
    monkeypatch.setattr(core, "build_feature_prompt", lambda *a, **k: "prefix")
    sent = {}

    def fake(prompt, *a, **k):
        sent["prompt"] = prompt
        return ({"is_pm_role": True, "title_level": "senior", "years_min": None,
                 "years_max": None, "domain_class": "core", "hard_blockers": [],
                 "requires_managing_pms": None, "jd_quality": "ok", "fit_score": 70,
                 "reason": "r", "gap": "None"}, {})

    monkeypatch.setattr(core, "claude_structured", fake)
    jd = "HEAD content here. " + "filler " * 2_000 + " TAIL requirements here."
    core.extract_features({"title": "Senior Product Manager", "source": "LinkedIn"}, jd)

    assert "HEAD content here" in sent["prompt"]
    assert "TAIL requirements here" in sent["prompt"]
    assert len(jd) > core.JD_PROMPT_MAX_CHARS      # the fixture really is oversized
