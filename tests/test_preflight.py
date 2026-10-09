"""The API check that runs before any scraping.

When the Anthropic credit balance ran out mid-backfill on 2026-10-09, each later
scheduled run still searched nine LinkedIn keywords and fetched JDs at 1 req/s for
minutes before its first evaluation call failed. One request answers the question,
so it is asked first.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core_eval_hosted as core


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
def _key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub")


def test_an_exhausted_balance_is_caught_before_any_scraping(monkeypatch):
    monkeypatch.setattr(core.requests, "post", lambda *a, **k: _Resp(400, {
        "error": {"type": "invalid_request_error",
                  "message": "Your credit balance is too low to access the Anthropic API."}}))
    with pytest.raises(core.ClaudeAPIError) as e:
        core.preflight_api_check()
    assert "credit balance is too low" in str(e.value)


def test_a_bad_key_is_caught(monkeypatch):
    monkeypatch.setattr(core.requests, "post", lambda *a, **k: _Resp(401, {
        "error": {"type": "authentication_error", "message": "invalid x-api-key"}}))
    with pytest.raises(core.ClaudeAPIError) as e:
        core.preflight_api_check()
    assert "401" in str(e.value) and "invalid x-api-key" in str(e.value)


def test_a_healthy_api_passes_quietly(monkeypatch):
    monkeypatch.setattr(core.requests, "post", lambda *a, **k: _Resp(200, {
        "content": [{"type": "text", "text": ""}], "stop_reason": "max_tokens"}))
    assert core.preflight_api_check() is None


def test_the_check_is_as_cheap_as_it_can_be(monkeypatch):
    """It must not carry the profile, the prompt, a tool schema or a JD."""
    sent = {}
    monkeypatch.setattr(core.requests, "post",
                        lambda *a, **k: (sent.update(k["json"]), _Resp(200, {"content": []}))[1])
    core.preflight_api_check()
    assert sent["max_tokens"] == 1
    assert sent["messages"] == [{"role": "user", "content": "ping"}]
    assert "tools" not in sent
    assert len(json.dumps(sent)) < 300


def test_a_missing_key_fails_here_too(monkeypatch):
    """No key at all must not reach the scrapers either."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(core, "_load_api_key",
                        lambda: (_ for _ in ()).throw(RuntimeError("Set ANTHROPIC_API_KEY")))
    with pytest.raises(RuntimeError):
        core.preflight_api_check()


def test_pm_eval_runs_the_check_before_it_collects_anything(monkeypatch):
    """Order is the whole point: preflight, then scrape."""
    import pm_eval_hosted as pm

    order = []

    def dead_api():
        order.append("preflight")
        raise core.ClaudeAPIError("credit balance is too low")

    def no_scraping(*a, **k):
        order.append("scrape")
        raise AssertionError("must not be reached once preflight fails")

    # SPREADSHEET_ID is read at import time, so set the attribute, not the env var.
    monkeypatch.setattr(pm, "SPREADSHEET_ID", "sheet")
    monkeypatch.setattr(pm, "load_candidate_profile", lambda: order.append("profile") or "x" * 300)
    monkeypatch.setattr(pm, "preflight_api_check", dead_api)
    monkeypatch.setattr(pm, "SOURCES", [("LinkedIn", no_scraping)])
    monkeypatch.setattr(pm, "load_seen_urls", lambda *a, **k: order.append("dedup") or set())

    with pytest.raises(SystemExit) as exit_info:
        pm.main()
    assert exit_info.value.code == 1
    assert order == ["profile", "preflight"]   # never got as far as dedup or a scrape
