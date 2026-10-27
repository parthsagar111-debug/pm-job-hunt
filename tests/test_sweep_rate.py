"""Pacing, per feed.

PM Eval searches one location and sustains 1 req/s: its runs log 0-1 rate limits
across 120-683 requests. The multi-location sweeps do not. On 2026-10-14 Gulf was
throttled on 82 of 217 requests and Global Visa on 343 of 907, both pinned at the
0.25 floor without ever recovering. Each 429 costs a retry wait of 30s or more, so
Global Visa spent roughly 2.9 of its 5 hours being punished and still classified
only 412 of its 1,080 jobs, losing France and Poland to searches that never
returned a page.

Those same 907 requests at a steady 0.5 req/s are about 30 minutes of pacing. So the
sweeps start there, and PM Eval is left alone.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core_eval_hosted as core


def test_the_sweep_rate_sits_between_the_ceiling_and_the_floor():
    assert core.LI_MIN_RATE < core.LI_SWEEP_RATE < core.LI_TARGET_RATE


def test_retarget_moves_both_the_rate_and_the_ceiling():
    """Setting only the current rate would let reward() creep back to 1.0."""
    lim = core.RateLimiter()
    assert (lim.rate, lim.target) == (core.LI_TARGET_RATE, core.LI_TARGET_RATE)
    lim.retarget(core.LI_SWEEP_RATE)
    assert (lim.rate, lim.target) == (core.LI_SWEEP_RATE, core.LI_SWEEP_RATE)


def test_recovery_cannot_climb_past_the_retargeted_ceiling():
    lim = core.RateLimiter()
    lim.retarget(core.LI_SWEEP_RATE)
    lim.penalize()
    for _ in range(500):
        lim.reward()
    assert lim.rate <= core.LI_SWEEP_RATE


def test_a_retargeted_limiter_still_backs_off_to_the_floor():
    """Slower pacing is a starting point, not a replacement for backoff."""
    lim = core.RateLimiter()
    lim.retarget(core.LI_SWEEP_RATE)
    for _ in range(10):
        lim.penalize()
    assert lim.rate == core.LI_MIN_RATE


def test_pacing_halves_the_request_budget_not_the_requests():
    """0.5 req/s is twice the wall-clock per request and nothing else."""
    lim = core.RateLimiter()
    lim.retarget(0.5)
    assert 1.0 / lim.rate == pytest.approx(2.0)


@pytest.mark.parametrize("module,expected", [
    ("gulf_eval_hosted", core.LI_SWEEP_RATE),
    ("global_visa_hosted", core.LI_SWEEP_RATE),
])
def test_each_sweep_feed_paces_itself_before_scraping(module, expected, monkeypatch):
    """It must happen before the first request, or the first location pays full price."""
    import importlib
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub")
    mod = importlib.import_module(module)
    importlib.reload(mod)

    seen = []
    monkeypatch.setattr(mod.LI_LIMITER, "retarget", lambda r: seen.append(r))
    monkeypatch.setattr(mod, "SPREADSHEET_ID", "sheet")
    # Gulf gained a profile load and an API preflight when it moved to the shared
    # feature prompt; both run before the pacing call and would exit first here.
    if hasattr(mod, "load_candidate_profile"):
        monkeypatch.setattr(mod, "load_candidate_profile", lambda: "x" * 300)
    if hasattr(mod, "preflight_api_check"):
        monkeypatch.setattr(mod, "preflight_api_check", lambda: None)

    def stop(*a, **k):
        raise SystemExit("stop before any scraping")

    monkeypatch.setattr(mod, "load_seen_urls", stop)
    with pytest.raises(SystemExit):
        mod.main()
    assert seen == [expected]


def test_pm_eval_is_not_slowed_down(monkeypatch):
    """One location at 1 req/s is measured as fine; halving it would double a run."""
    import importlib
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub")
    import pm_eval_hosted as pm
    importlib.reload(pm)

    calls = []
    monkeypatch.setattr(pm.LI_LIMITER, "retarget", lambda r: calls.append(r))
    monkeypatch.setattr(pm, "SPREADSHEET_ID", "sheet")
    monkeypatch.setattr(pm, "load_candidate_profile", lambda: "x" * 300)
    monkeypatch.setattr(pm, "preflight_api_check", lambda: None)
    monkeypatch.setattr(pm, "load_seen_urls",
                        lambda *a, **k: (_ for _ in ()).throw(SystemExit("stop")))
    with pytest.raises(SystemExit):
        pm.main()
    assert calls == []


# ── the pacing is for week sweeps, not for every run (2026-10-27)
@pytest.mark.parametrize("module", ["gulf_eval_hosted", "global_visa_hosted"])
@pytest.mark.parametrize("window,paced", [("week", True), ("24h", False)])
def test_only_a_week_window_is_paced_down(module, window, paced, monkeypatch):
    """0.5 req/s was measured on week sweeps: 907 requests, 343 rate-limited, where
    the 429 penalties cost more than the pacing. Applying it to every run doubled the
    pacing on 24h windows for no benefit — a Global Visa 24h sweep that took ~40
    minutes took over 1h32m — and because retarget() lowers the ceiling too, the
    limiter could no longer climb back to 1.0."""
    import importlib
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub")
    monkeypatch.setenv("TIME_RANGE", window)
    mod = importlib.import_module(module)
    importlib.reload(mod)
    assert mod.TIME_RANGE == window

    seen = []
    monkeypatch.setattr(mod.LI_LIMITER, "retarget", lambda r: seen.append(r))
    monkeypatch.setattr(mod, "SPREADSHEET_ID", "sheet")
    if hasattr(mod, "load_candidate_profile"):
        monkeypatch.setattr(mod, "load_candidate_profile", lambda: "x" * 300)
    if hasattr(mod, "preflight_api_check"):
        monkeypatch.setattr(mod, "preflight_api_check", lambda: None)
    monkeypatch.setattr(mod, "load_seen_urls",
                        lambda *a, **k: (_ for _ in ()).throw(SystemExit("stop")))
    with pytest.raises(SystemExit):
        mod.main()

    assert seen == ([core.LI_SWEEP_RATE] if paced else [])
    monkeypatch.delenv("TIME_RANGE")
    importlib.reload(mod)
