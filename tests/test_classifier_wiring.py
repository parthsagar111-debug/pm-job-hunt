"""Wiring around the classifier: profile loading, feature validation, the title
gates, the Naukri company/experience fix, and the content dedup key.

All offline — no network, no API key.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import candidate_profile
import core_eval_hosted as core


# ── CANDIDATE_PROFILE secret
@pytest.fixture(autouse=True)
def _clear_profile_cache():
    candidate_profile.reset_cache()
    yield
    candidate_profile.reset_cache()


def test_missing_secret_raises_naming_the_variable(monkeypatch):
    monkeypatch.delenv(candidate_profile.ENV_VAR, raising=False)
    with pytest.raises(candidate_profile.CandidateProfileMissing) as excinfo:
        candidate_profile.load_candidate_profile()
    assert candidate_profile.ENV_VAR in str(excinfo.value)


def test_truncated_secret_raises(monkeypatch):
    """A placeholder would let the model invent gaps from nothing — the exact bug."""
    monkeypatch.setenv(candidate_profile.ENV_VAR, "Senior PM")
    with pytest.raises(candidate_profile.CandidateProfileMissing):
        candidate_profile.load_candidate_profile()


def test_valid_secret_loads_and_caches(monkeypatch):
    profile = "P" * 400
    monkeypatch.setenv(candidate_profile.ENV_VAR, profile)
    assert candidate_profile.load_candidate_profile() == profile
    monkeypatch.delenv(candidate_profile.ENV_VAR)
    assert candidate_profile.load_candidate_profile() == profile   # cached


def test_prompt_embeds_the_profile(monkeypatch):
    monkeypatch.setenv(candidate_profile.ENV_VAR, "MARKER" + "x" * 400)
    prompt = core.build_feature_prompt()
    assert "MARKER" in prompt
    assert "record_features" in prompt
    assert "years_min" in prompt


def test_no_profile_text_is_committed_in_the_repo():
    """This repo is public: the profile must only ever arrive via the secret."""
    for module in (core, candidate_profile):
        source = open(module.__file__, encoding="utf-8").read()
        assert "Chetana" not in source
        assert "JioMart" not in source
        assert "Hyugalife" not in source


# ── feature validation
def _valid_payload(**overrides):
    payload = {
        "is_pm_role": True, "title_level": "senior", "years_min": 5, "years_max": 8,
        "domain_class": "core", "hard_blockers": [], "requires_managing_pms": None,
        "stated_max_ctc_lpa": None, "jd_quality": "ok", "fit_score": 80,
        "reason": "r", "gap": "None",
    }
    payload.update(overrides)
    return payload


def test_validator_passes_a_good_payload():
    assert core._validate_features(_valid_payload())["fit_score"] == 80


@pytest.mark.parametrize("field", ["years_min", "years_max", "requires_managing_pms"])
def test_null_like_strings_become_none(field):
    assert core._validate_features(_valid_payload(**{field: "null"}))[field] is None


def test_out_of_range_values_raise():
    with pytest.raises(core.ClaudeSchemaError):
        core._validate_features(_valid_payload(fit_score=150))
    with pytest.raises(core.ClaudeSchemaError):
        core._validate_features(_valid_payload(years_min=99))
    with pytest.raises(core.ClaudeSchemaError):
        core._validate_features(_valid_payload(is_pm_role="yes"))


def test_invented_blockers_are_dropped_not_trusted(capsys):
    """An unknown blocker would silently Skip a good job."""
    out = core._validate_features(_valid_payload(
        hard_blockers=["deep_infra_security_networking", "vibes_mismatch"]))
    assert out["hard_blockers"] == ["deep_infra_security_networking"]
    assert "vibes_mismatch" in capsys.readouterr().out


# ── title gates
@pytest.mark.parametrize("title,expected", [
    ("Senior Product Manager", True),
    ("Product Owner", True),
    ("Group PM, Growth", True),
    ("Associate Product Manager", True),      # kept; decide() rule 4 skips it
    ("Business Development Manager", False),  # dropped from the PM Eval gate
    ("BDM - North", False),
    ("Category Manager", False),
    ("Category Head", False),
    ("Software Engineer", False),
])
def test_pm_eval_gate(title, expected):
    assert core.is_pm_eval_role(title) is expected


def test_pm_eval_gate_uses_word_boundaries():
    """The substring gate matched 'apm' inside ordinary words."""
    assert core.is_pm_eval_role("Shipment Handler") is False
    assert core.is_pm_eval_role("Kapmar Operations Lead") is False
    assert core.is_pm_eval_role("APM - Payments") is True


@pytest.mark.parametrize("title", [
    "Business Development Manager", "Category Manager", "BDM", "Category Head",
])
def test_gulf_and_global_gate_is_unchanged(title):
    """Pinned per Parth: only PM Eval got the narrower list. _parse_li_cards — shared
    by Gulf and Global — still calls is_pm_role."""
    assert core.is_pm_role(title) is True
    assert core.is_pm_eval_role(title) is False


def test_shared_card_parser_still_uses_the_old_gate():
    html = """
    <div class="base-card">
      <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/4123456789"></a>
      <h3 class="base-search-card__title">Category Manager</h3>
      <h4 class="base-search-card__subtitle">ACME</h4>
      <span class="job-search-card__location">Dubai, United Arab Emirates</span>
      <time datetime="2026-09-30">2 hours ago</time>
    </div>"""
    jobs, _ids = core._parse_li_cards(html)
    assert len(jobs) == 1   # Gulf/Global keep seeing it


# ── Naukri company/experience fix
class _FakeCard:
    def __init__(self, texts):
        self._texts = texts
    def find_all(self, names):
        class El:
            def __init__(self, text): self._t = text
            def get(self, _attr): return ["comp-name"]
            def get_text(self, strip=False): return self._t
        return [El(t) for t in self._texts]


@pytest.mark.parametrize("value", ["6 - 12 yrs", "3-6 yrs", "10 - 15 yrs."])
def test_experience_in_the_company_field_is_moved(value):
    company, experience = core._split_naukri_company(value, _FakeCard(["Acme Retail Pvt Ltd"]))
    assert company == "Acme Retail Pvt Ltd"
    assert experience == value


def test_unrecoverable_company_becomes_unknown():
    company, experience = core._split_naukri_company("6 - 12 yrs", _FakeCard(["4 - 9 yrs"]))
    assert company == "Unknown"
    assert experience == "6 - 12 yrs"


def test_a_real_company_name_is_left_alone():
    assert core._split_naukri_company("Flipkart", _FakeCard([])) == ("Flipkart", "")


def test_company_named_like_a_range_is_not_confused():
    assert core._split_naukri_company("12 Yards Media", _FakeCard([]))[0] == "12 Yards Media"


# ── content dedup key
def test_key_needs_the_jd():
    assert core.job_content_key("PM", "ACME", "") == ""
    assert core.job_content_key("PM", "ACME", "   ") == ""


def test_same_posting_same_key_despite_formatting():
    jd = "Own the roadmap for checkout. " * 40
    a = core.job_content_key("Senior Product Manager", "ACME", jd)
    b = core.job_content_key("  senior   product manager ", "acme", jd.replace(" ", "  "))
    assert a == b and len(a) == 40


def test_same_title_and_company_but_different_jd_are_different_jobs():
    """The company+title-only key dropped 168 real jobs on 2026-09-18."""
    a = core.job_content_key("Product Manager", "Flipkart", "Own search ranking. " * 40)
    b = core.job_content_key("Product Manager", "Flipkart", "Own seller payments. " * 40)
    assert a != b


def test_only_the_first_1500_chars_matter():
    head = "Identical opening. " * 90          # > 1500 chars
    a = core.job_content_key("PM", "ACME", head + "tail A")
    b = core.job_content_key("PM", "ACME", head + "tail B")
    assert a == b
