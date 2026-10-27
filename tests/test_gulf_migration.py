"""The Gulf feed on the shared feature prompt and the pure rule engine.

Until 2026-10-27 it asked the model for the decision directly, from a prompt carrying
all three causes of the 17.6% misclassification rate the India feed was overhauled to
fix: a hardcoded candidate profile ("9+ years", a short domain list) that drifted from
the resume, a distribution quota (~20/35/45), and two "when in doubt pick Maybe" lines.

Migrating it means the Gulf-only policy has to survive, which is what these pin.
"""

import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core_eval_hosted as core
import gulf_eval_hosted as gulf
from decision_rules import ADVISORY_BLOCKERS, CATEGORICAL_BLOCKERS, decide


def features(**overrides):
    base = {
        "is_pm_role": True, "title_level": "senior", "title_is_junior": False,
        "years_min": None, "years_max": None, "domain_class": "core",
        "hard_blockers": [], "requires_managing_pms": None, "jd_quality": "ok",
        "fit_score": 70, "reason": "reason", "gap": "None",
    }
    base.update(overrides)
    return base


def test_the_legacy_decision_prompt_is_gone():
    assert not hasattr(gulf, "GULF_EVAL_PROMPT")


def test_the_quota_and_the_when_in_doubt_lines_are_gone():
    """Both drove the India misclassification; neither may survive as prompt text.

    Comment lines are stripped first — the commit that removed them quotes them in a
    comment explaining why, and that should not trip the check.
    """
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "gulf_eval_hosted.py")
    lines = io.open(path, encoding="utf-8").read().splitlines()
    code = " ".join(ln for ln in lines if not ln.lstrip().startswith("#")).lower()
    for banned in ("~20% apply", "when in doubt", "9+ years experience", "rough guide"):
        assert banned not in code, banned


def test_gulf_uses_the_shared_rule_engine():
    assert gulf.decide is decide


def test_the_gulf_note_is_appended_not_interpolated(monkeypatch):
    """The cached prefix must stay byte-identical within a feed, so the note is a
    suffix. A per-job difference would defeat the cache entirely."""
    monkeypatch.setattr("candidate_profile.load_candidate_profile", lambda: "PROFILE " * 40)
    plain = core.build_feature_prompt()
    gulfed = core.build_feature_prompt(core.GULF_FEATURE_NOTE)
    assert gulfed.startswith(plain)
    assert gulfed.endswith(core.GULF_FEATURE_NOTE)


def test_the_gulf_note_covers_the_policy_the_india_rules_do_not():
    note = core.GULF_FEATURE_NOTE
    assert "nationals_only_or_localisation" in note
    assert "mandatory_arabic" in note
    # Needing a work visa is the normal condition of the GCC market, not a gap.
    assert "NEVER a gap" in note
    # Arabic as a preference, and being outside the country, are explicitly not bars.
    assert "preferred" in note and "not a blocker" in note.lower()


@pytest.mark.parametrize("blocker", ["nationals_only_or_localisation", "mandatory_arabic"])
def test_the_gulf_blockers_reject(blocker):
    """A non-citizen cannot become one, and the candidate does not speak Arabic —
    these are absolute, unlike the 'preferred, written like a rule' advisory ones."""
    assert blocker in CATEGORICAL_BLOCKERS
    assert blocker not in ADVISORY_BLOCKERS
    assert decide(features(fit_score=95, hard_blockers=[blocker])) == ("Skip", "")


def test_the_new_blockers_are_accepted_by_the_validator():
    """An enum the validator drops would be silently ignored."""
    assert "nationals_only_or_localisation" in core.VALID_BLOCKERS
    assert "mandatory_arabic" in core.VALID_BLOCKERS
    assert core.VALID_BLOCKERS == CATEGORICAL_BLOCKERS | ADVISORY_BLOCKERS


def test_the_arabic_regex_override_is_retained():
    """The model's new mandatory_arabic enum is unproven; the regex that was tuned
    over 18 cases stays as a second line of defence."""
    assert callable(gulf.apply_arabic_override)
    jobs = [{"evaluation": {"decision": "Apply", "reason": "r", "gap": "None",
                            "jd": "Requirements:\nFluency in English and Arabic is required\n"}}]
    moved = gulf.apply_arabic_override(jobs)
    assert moved == 1
    assert jobs[0]["evaluation"]["decision"] == "Skip"


def test_a_relocation_role_is_not_penalised_for_needing_a_visa():
    """The old prompt had to be told this; the rules never looked at visas at all, so
    a Gulf role now rests on domain and fit like any other."""
    assert decide(features(domain_class="core", fit_score=72))[0] == "Apply"
