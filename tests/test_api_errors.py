"""An API refusal has to say what it was.

On 2026-10-09 a 519-job backfill aborted at job 340 after three consecutive
"400 Client Error: Bad Request for url: https://api.anthropic.com/v1/messages".
That is everything requests' raise_for_status() knows, and it is not enough to tell
an exhausted credit balance from an over-long prompt from a malformed request. The
API returns a reason in the body; these tests pin that we read it.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core_eval_hosted as core

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


class _Resp:
    def __init__(self, status, payload=None, text=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else (json.dumps(payload) if payload else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub")


def _call():
    return core.claude_structured("prompt", "t", "d", SCHEMA)


def test_an_exhausted_balance_says_so(monkeypatch):
    monkeypatch.setattr(core.requests, "post", lambda *a, **k: _Resp(400, {
        "error": {"type": "invalid_request_error",
                  "message": "Your credit balance is too low to access the Anthropic API."}}))
    with pytest.raises(core.ClaudeAPIError) as e:
        _call()
    assert "credit balance is too low" in str(e.value)
    assert "invalid_request_error" in str(e.value)
    assert "400" in str(e.value)


def test_an_over_long_prompt_says_so(monkeypatch):
    monkeypatch.setattr(core.requests, "post", lambda *a, **k: _Resp(400, {
        "error": {"type": "invalid_request_error",
                  "message": "prompt is too long: 243000 tokens > 200000 maximum"}}))
    with pytest.raises(core.ClaudeAPIError) as e:
        _call()
    assert "prompt is too long" in str(e.value)


def test_a_body_that_is_not_json_is_still_reported(monkeypatch):
    monkeypatch.setattr(core.requests, "post",
                        lambda *a, **k: _Resp(502, text="<html>upstream timeout</html>"))
    with pytest.raises(core.ClaudeAPIError) as e:
        _call()
    assert "502" in str(e.value)
    assert "upstream timeout" in str(e.value)


def test_an_empty_body_still_names_the_status(monkeypatch):
    monkeypatch.setattr(core.requests, "post", lambda *a, **k: _Resp(529, text=""))
    with pytest.raises(core.ClaudeAPIError) as e:
        _call()
    assert "529" in str(e.value)


def test_the_error_never_echoes_the_prompt(monkeypatch):
    """This repo is public and its Actions logs are too."""
    monkeypatch.setattr(core.requests, "post", lambda *a, **k: _Resp(400, {
        "error": {"type": "invalid_request_error", "message": "bad request"}}))
    with pytest.raises(core.ClaudeAPIError) as e:
        core.claude_structured("SECRET-PROFILE-TEXT", "t", "d", SCHEMA,
                               cached_prefix="SECRET-PREFIX-TEXT")
    assert "SECRET" not in str(e.value)


def test_a_refused_cache_ttl_still_retries_before_giving_up(monkeypatch):
    """The existing 1h-TTL fallback must survive the stricter error handling."""
    calls = []

    def post(*a, **k):
        calls.append(k["json"]["messages"][0]["content"][0].get("cache_control"))
        if len(calls) == 1:
            return _Resp(400, text='{"error":{"message":"ttl is not supported"}}')
        return _Resp(200, {"content": [{"type": "tool_use", "name": "t", "input": {"ok": True}}],
                           "usage": {"input_tokens": 1, "output_tokens": 1}})

    monkeypatch.setattr(core.requests, "post", post)
    out, _ = core.claude_structured("p", "t", "d", SCHEMA, cached_prefix="x" * 100)
    assert out == {"ok": True}
    assert calls[0]["ttl"] == core.CACHE_TTL      # first try asked for the long TTL
    assert "ttl" not in calls[1]                  # retry fell back to the default


def test_a_good_reply_is_unaffected(monkeypatch):
    monkeypatch.setattr(core.requests, "post", lambda *a, **k: _Resp(200, {
        "content": [{"type": "tool_use", "name": "t", "input": {"ok": True}}],
        "usage": {"input_tokens": 10, "output_tokens": 2}}))
    out, usage = _call()
    assert out == {"ok": True}
    assert usage["input_tokens"] == 10
