"""Claude schema enforcement, the Skip-blackhole regression, and the rate limiter."""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core_eval_hosted as core
import global_visa_hosted as gv


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _tool_use(name, tool_input):
    return {"content": [{"type": "tool_use", "name": name, "input": tool_input}],
            "stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 5}}


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")


# ── claude_structured
def test_returns_tool_input(monkeypatch):
    monkeypatch.setattr(core.requests, "post", lambda *a, **k: FakeResponse(
        _tool_use("record_decision", {"decision": "Apply", "reason": "fits", "gap": "None"})))
    payload, usage = core.claude_structured("p", "record_decision", "d", core.EVAL_TOOL_SCHEMA)
    assert payload["decision"] == "Apply"
    assert usage["input_tokens"] == 10


def test_raises_when_model_answers_in_prose(monkeypatch):
    """The old string-prefix parser read prose as Skip. Now it must raise."""
    monkeypatch.setattr(core.requests, "post", lambda *a, **k: FakeResponse(
        {"content": [{"type": "text", "text": "Decision: Apply\nReason: good"}],
         "stop_reason": "end_turn"}))
    with pytest.raises(core.ClaudeSchemaError):
        core.claude_structured("p", "record_decision", "d", core.EVAL_TOOL_SCHEMA)


def test_raises_on_value_outside_enum(monkeypatch):
    monkeypatch.setattr(core.requests, "post", lambda *a, **k: FakeResponse(
        _tool_use("record_decision", {"decision": "Probably", "reason": "r", "gap": "g"})))
    with pytest.raises(core.ClaudeSchemaError):
        core.claude_structured("p", "record_decision", "d", core.EVAL_TOOL_SCHEMA)


def test_raises_on_missing_required_field(monkeypatch):
    monkeypatch.setattr(core.requests, "post", lambda *a, **k: FakeResponse(
        _tool_use("record_decision", {"decision": "Skip", "reason": "r"})))
    with pytest.raises(core.ClaudeSchemaError):
        core.claude_structured("p", "record_decision", "d", core.EVAL_TOOL_SCHEMA)


# ── evaluate_job: the regression that mattered
def test_malformed_reply_becomes_error_not_skip(monkeypatch):
    """A Skip is written to the Sheet and deduped forever, so a parse failure that
    reads as Skip blackholes the job permanently. It must come back as Error."""
    monkeypatch.setattr(core, "fetch_jd_text", lambda job: "some jd text")
    monkeypatch.setattr(core.requests, "post", lambda *a, **k: FakeResponse(
        {"content": [{"type": "text", "text": "I think you should apply!"}],
         "stop_reason": "end_turn"}))
    result = core.evaluate_job({"title": "PM", "company": "X", "location": "Mumbai", "source": "LinkedIn"})
    assert result["decision"] == "Error"
    assert result["jd"] == "some jd text"   # JD preserved for the retry


def test_api_failure_becomes_error(monkeypatch):
    monkeypatch.setattr(core, "fetch_jd_text", lambda job: "")
    def boom(*a, **k):
        raise ConnectionError("network down")
    monkeypatch.setattr(core.requests, "post", boom)
    assert core.evaluate_job({"title": "PM", "company": "X", "location": "", "source": "LinkedIn"})["decision"] == "Error"


def test_good_reply_passes_through(monkeypatch):
    monkeypatch.setattr(core, "fetch_jd_text", lambda job: "jd")
    monkeypatch.setattr(core.requests, "post", lambda *a, **k: FakeResponse(
        _tool_use("record_decision", {"decision": "Maybe", "reason": "domain gap", "gap": "fintech"})))
    result = core.evaluate_job({"title": "PM", "company": "X", "location": "", "source": "LinkedIn"})
    assert (result["decision"], result["reason"], result["gap"]) == ("Maybe", "domain gap", "fintech")


# ── evaluate_batch keeps Errors out of the Sheet
def test_batch_drops_errors_and_aborts_after_three(monkeypatch):
    calls = {"n": 0}
    def fake_eval(job, prompt=None):
        calls["n"] += 1
        return {"decision": "Error", "reason": "boom", "gap": "-", "jd": ""}
    monkeypatch.setattr(core, "evaluate_job", fake_eval)
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    ok, aborted = core.evaluate_batch([{"title": f"j{i}", "company": "c"} for i in range(10)])
    assert ok == [] and aborted is True
    assert calls["n"] == core.CONSECUTIVE_ERROR_LIMIT   # stopped early, didn't burn the batch


# ── visa classifier
def test_visa_classifier_returns_verdict_and_evidence(monkeypatch):
    monkeypatch.setattr(core.requests, "post", lambda *a, **k: FakeResponse(
        _tool_use("record_visa", {"visa": "YES", "evidence": "We do sponsor visas!"})))
    verdict, evidence = gv.classify({"title": "PM", "company": "C", "location": "Seattle, WA"}, "excerpt")
    assert verdict == "YES" and "sponsor" in evidence


def test_visa_classifier_error_is_not_a_verdict(monkeypatch):
    monkeypatch.setattr(core.requests, "post", lambda *a, **k: FakeResponse({"content": []}))
    monkeypatch.setattr(gv.time, "sleep", lambda s: None)
    verdict, _ = gv.classify({"title": "PM", "company": "C", "location": "Seattle, WA"}, "excerpt")
    assert verdict == "ERROR"   # never written to the Sheet, retried next run


# ── rate limiter
def test_limiter_paces_to_target_rate():
    lim = core.RateLimiter(rate=10.0)
    start = time.time()
    for _ in range(10):
        lim.acquire()
    elapsed = time.time() - start
    assert 0.7 < elapsed < 1.3          # 9 gaps of ~0.1s, ±15% jitter
    assert lim.requests == 10


def test_limiter_halves_on_block_and_respects_floor():
    lim = core.RateLimiter(rate=1.0)
    lim.penalize()
    assert lim.rate == 0.5
    for _ in range(10):
        lim.penalize()
    assert lim.rate == core.LI_MIN_RATE
    assert lim.blocks == 11


def test_limiter_recovers_only_after_sustained_success():
    lim = core.RateLimiter(rate=1.0)
    lim.rate = 0.5
    for _ in range(19):
        lim.reward()
    assert lim.rate == 0.5              # not yet
    lim.reward()
    assert lim.rate > 0.5               # 20 clean requests -> ease up
    for _ in range(500):
        lim.reward()
    assert lim.rate == lim.target       # never exceeds the measured ceiling
