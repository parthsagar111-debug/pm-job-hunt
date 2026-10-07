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


# ── rows are positioned by header name, not by offset
def test_rows_follow_the_sheets_own_header_order():
    """The Sheet has columns this module doesn't write. On 2026-09-30 a 12-value row
    was appended positionally to a 13-column sheet and 166 hashes overwrote Agent 1's
    "Resume Match Score". Values must land under their own header or nowhere."""
    import sheets_writer as sw
    live = sw.HEADERS_EVAL + ["Resume Match Score", "Resume Decision"]
    rows = sw._align_rows([{"Title": "PM", "URL": "u", "Decision": "Apply"}], live, "Apply")
    assert len(rows[0]) == len(live)
    assert rows[0][live.index("Title")] == "PM"
    assert rows[0][live.index("Decision")] == "Apply"
    assert rows[0][live.index("Resume Match Score")] == ""
    assert rows[0][live.index("Resume Decision")] == ""


def test_a_reordered_sheet_still_gets_correct_columns():
    import sheets_writer as sw
    live = ["URL", "Title", "Decision"]
    out = sw._align_rows([{"Title": "PM", "URL": "u", "Decision": "Skip"}], live, "Skip")
    assert out == [["u", "PM", "Skip"]]


def test_a_value_with_no_column_is_reported_not_misplaced(capsys):
    import sheets_writer as sw
    rows = sw._align_rows([{"Title": "PM", "Nonesuch": "x"}], ["Title", "URL"], "Apply")
    assert rows == [["PM", ""]]
    assert "Nonesuch" in capsys.readouterr().out


def test_empty_header_row_falls_back_to_this_modules_order():
    import sheets_writer as sw

    class _WS:
        title = "Apply"
        def row_values(self, n):
            return []

    assert sw._live_headers(_WS(), sw.HEADERS_EVAL) == sw.HEADERS_EVAL


def test_job_key_column_is_gone():
    """Nothing writes a content hash any more — dedup is URL-only again."""
    import sheets_writer as sw
    assert "Job Key" not in sw.HEADERS_EVAL
    assert not hasattr(core, "job_content_key")


# ── prompt caching
def _capture_request(monkeypatch):
    """Returns a dict that fills with the JSON body of the next API call.
    Offline: the key is a stub and requests.post is replaced."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    captured = {}

    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"content": [{"type": "tool_use", "name": "record_features",
                                 "input": _valid_payload()}],
                    "usage": {"input_tokens": 120, "output_tokens": 40,
                              "cache_creation_input_tokens": 3300,
                              "cache_read_input_tokens": 0}}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(json or {})
        return R()

    monkeypatch.setattr(core.requests, "post", fake_post)
    return captured


def test_static_prefix_is_marked_for_caching(monkeypatch):
    monkeypatch.setenv(candidate_profile.ENV_VAR, "PROFILE " + "x" * 400)
    captured = _capture_request(monkeypatch)
    core.extract_features({"title": "Senior Product Manager", "company": "ACME",
                           "location": "Mumbai", "source": "Naukri"},
                          jd_text="Own the checkout funnel. " * 60)

    blocks = captured["messages"][0]["content"]
    assert isinstance(blocks, list) and len(blocks) == 2
    # 1-hour TTL: PM Eval runs every 30 minutes, so the 5-minute default expired before
    # every run and the whole static block was re-written each time.
    assert blocks[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in blocks[1]


def test_ttl_refusal_falls_back_to_the_default_cache(monkeypatch):
    """If the API ever rejects the 1h TTL, the job still gets evaluated (5-minute cache)."""
    monkeypatch.setenv(candidate_profile.ENV_VAR, "PROFILE " + "x" * 400)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    bodies = []

    class Refused:
        status_code = 400
        text = '{"error": {"message": "cache_control.ttl: not supported"}}'
        def raise_for_status(self): raise AssertionError("not raised for the retried call")

    class Ok:
        status_code = 200
        text = ""
        def raise_for_status(self): pass
        def json(self):
            return {"content": [{"type": "tool_use", "name": "record_features",
                                 "input": _valid_payload()}], "usage": {}}

    def fake_post(url, headers=None, json=None, timeout=None):
        bodies.append(dict(json["messages"][0]["content"][0]["cache_control"]))
        return Refused() if len(bodies) == 1 else Ok()

    monkeypatch.setattr(core.requests, "post", fake_post)
    features, _ = core.extract_features({"title": "PM", "company": "ACME", "location": "Mumbai",
                                         "source": "Naukri"}, jd_text="Own checkout. " * 60)
    assert bodies == [{"type": "ephemeral", "ttl": "1h"}, {"type": "ephemeral"}]
    assert features["fit_score"] == _valid_payload()["fit_score"]


def test_unusable_enum_is_filed_as_unverified_not_retried_forever(monkeypatch):
    """A reply with domain_class '<UNKNOWN>' used to raise, so the job was re-sent (and
    re-paid) every run. It now becomes a Maybe via the unreadable-JD path, never a Skip."""
    from decision_rules import decide
    monkeypatch.setenv(candidate_profile.ENV_VAR, "PROFILE " + "x" * 400)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    bad = {**_valid_payload(), "domain_class": "<UNKNOWN>"}

    class R:
        status_code = 200
        text = ""
        def raise_for_status(self): pass
        def json(self):
            return {"content": [{"type": "tool_use", "name": "record_features", "input": bad}],
                    "usage": {}}

    monkeypatch.setattr(core.requests, "post", lambda *a, **k: R())
    monkeypatch.setattr(core, "fetch_jd_text", lambda job: "Own the checkout funnel. " * 60)
    ev = core.evaluate_job_features({"title": "Product Manager", "company": "Just Dial",
                                     "location": "Mumbai", "source": "Naukri"}, decide)
    assert ev["decision"] == "Maybe"
    assert "UNVERIFIED" in ev["reason"]


def test_only_the_extreme_tail_of_a_jd_is_cut_from_the_prompt(monkeypatch):
    monkeypatch.setenv(candidate_profile.ENV_VAR, "PROFILE " + "x" * 400)
    captured = _capture_request(monkeypatch)
    jd = "Z" * (core.JD_PROMPT_MAX_CHARS + 5000)
    core.JD_SENT_CHARS.clear()
    core.extract_features({"title": "PM", "company": "ACME", "location": "Mumbai",
                           "source": "Naukri"}, jd_text=jd)
    sent = captured["messages"][0]["content"][1]["text"]
    assert sent.count("Z") == core.JD_PROMPT_MAX_CHARS
    assert core.JD_SENT_CHARS == [len(jd)]          # the run report records the FULL length


def test_free_text_fields_are_asked_to_be_short():
    props = core.FEATURES_TOOL_SCHEMA["properties"]
    assert "10 words" in props["reason"]["description"]
    assert "6 words" in props["gap"]["description"]


def test_jd_stays_outside_the_cached_block(monkeypatch):
    """A JD inside the cached prefix would change it every call and defeat the cache."""
    monkeypatch.setenv(candidate_profile.ENV_VAR, "PROFILE " + "x" * 400)
    captured = _capture_request(monkeypatch)
    jd = "UNIQUE-JD-MARKER. Own the checkout funnel. " * 60
    core.extract_features({"title": "PM", "company": "ACME", "location": "Mumbai",
                           "source": "Naukri"}, jd_text=jd)

    cached, dynamic = captured["messages"][0]["content"]
    assert "UNIQUE-JD-MARKER" not in cached["text"]
    assert "UNIQUE-JD-MARKER" in dynamic["text"]
    assert "ACME" not in cached["text"]          # nothing job-specific in the prefix
    assert "PROFILE" in cached["text"]           # the static part is what gets cached


def test_cached_prefix_is_identical_across_jobs(monkeypatch):
    monkeypatch.setenv(candidate_profile.ENV_VAR, "PROFILE " + "x" * 400)
    prefixes = []
    for company in ("ACME", "Globex"):
        captured = _capture_request(monkeypatch)
        core.extract_features({"title": "PM", "company": company, "location": "Mumbai",
                               "source": "Naukri"}, jd_text="Own checkout. " * 60)
        prefixes.append(captured["messages"][0]["content"][0]["text"])
    assert prefixes[0] == prefixes[1]


def test_usage_including_cache_tokens_is_accumulated(monkeypatch):
    monkeypatch.setenv(candidate_profile.ENV_VAR, "PROFILE " + "x" * 400)
    core.TOKEN_USAGE.clear()
    _capture_request(monkeypatch)
    core.extract_features({"title": "PM", "company": "ACME", "location": "Mumbai",
                           "source": "Naukri"}, jd_text="Own checkout. " * 60)
    assert core.TOKEN_USAGE["calls"] == 1
    assert core.TOKEN_USAGE["cache_creation_input_tokens"] == 3300
    assert core.usage_cost_usd() > 0
    core.TOKEN_USAGE.clear()


def test_callers_without_a_cached_prefix_send_a_plain_string(monkeypatch):
    """Gulf and Global keep the exact request shape they were tuned against."""
    captured = _capture_request(monkeypatch)
    core.claude_structured("plain prompt", "record_features", "d",
                           core.FEATURES_TOOL_SCHEMA)
    assert captured["messages"][0]["content"] == "plain prompt"
