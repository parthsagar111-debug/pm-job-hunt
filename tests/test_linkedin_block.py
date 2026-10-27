"""Telling "LinkedIn is pacing us" apart from "LinkedIn has blocked this runner".

On 2026-10-27 a Global Visa run spent 2h45m in the second state: every request
429ing, the limiter pinned at its 0.25 floor with nothing left to give, per-request
waits escalating 39s -> 64s -> 79s, then "gave up after 3 attempts", over and over.
The day before, the same feed made 3,355 requests with 2 rate-limited — so the
difference was the runner's IP, not the pacing, and no client-side backoff recovers
from it.

Adaptive backoff is right for throttling and useless against a block. These pin that
the second case is detected and stops the run in minutes.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core_eval_hosted as core


@pytest.fixture
def limiter(monkeypatch):
    lim = core.RateLimiter()
    monkeypatch.setattr(core, "LI_LIMITER", lim)
    return lim


def test_a_fresh_limiter_is_not_blocked(limiter):
    assert limiter.is_blocked() is False
    assert limiter.giveups == 0


def test_one_give_up_is_bad_luck_not_a_block(limiter):
    limiter.record_giveup()
    assert limiter.is_blocked() is False


def test_enough_consecutive_give_ups_is_a_block(limiter):
    for _ in range(core.LI_GIVEUP_LIMIT):
        limiter.record_giveup()
    assert limiter.is_blocked() is True


def test_a_single_success_clears_the_count(limiter):
    """Give-ups have to be CONSECUTIVE — a served request means we are not blocked."""
    for _ in range(core.LI_GIVEUP_LIMIT - 1):
        limiter.record_giveup()
    limiter.reward()
    assert limiter.giveups == 0
    for _ in range(core.LI_GIVEUP_LIMIT - 1):
        limiter.record_giveup()
    assert limiter.is_blocked() is False


def test_a_blocked_limiter_short_circuits_the_request(limiter, monkeypatch):
    """The point is to stop paying 30-79s per request to rediscover the block."""
    called, slept = [], []
    monkeypatch.setattr(core, "_LI_GET", lambda *a, **k: called.append(1))
    monkeypatch.setattr(core.time, "sleep", lambda s: slept.append(s))
    for _ in range(core.LI_GIVEUP_LIMIT):
        limiter.record_giveup()

    assert core._li_get_with_retry("https://www.linkedin.com/jobs/view/1") is None
    assert called == []      # no request was made
    assert slept == []       # and nothing was waited on


class _Resp:
    status_code = 429

    def raise_for_status(self):
        raise AssertionError("not reached for a 429")


def test_a_refused_request_is_recorded_as_a_give_up(limiter, monkeypatch):
    monkeypatch.setattr(core, "_LI_GET", lambda *a, **k: _Resp())
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    assert core._li_get_with_retry("https://www.linkedin.com/jobs/view/1") is None
    assert limiter.giveups == 1


def test_repeated_refusals_reach_the_block_and_then_cost_nothing(limiter, monkeypatch):
    attempts, slept = [], []
    monkeypatch.setattr(core, "_LI_GET", lambda *a, **k: attempts.append(1) or _Resp())
    monkeypatch.setattr(core.time, "sleep", lambda s: slept.append(s))

    for _ in range(core.LI_GIVEUP_LIMIT):
        core._li_get_with_retry("https://www.linkedin.com/jobs/view/1")
    assert limiter.is_blocked()
    requests_before, sleeps_before = len(attempts), len(slept)

    for _ in range(20):
        core._li_get_with_retry("https://www.linkedin.com/jobs/view/1")
    assert len(attempts) == requests_before   # 20 further calls made no requests
    assert len(slept) == sleeps_before        # and waited on nothing


def test_the_batch_stops_instead_of_grinding(limiter, monkeypatch, capsys):
    """Without this the run keeps going to its 5-hour cap, classifying nothing."""
    for _ in range(core.LI_GIVEUP_LIMIT):
        limiter.record_giveup()

    def boom(*a, **k):
        raise AssertionError("no job should be evaluated once the IP is refused")

    monkeypatch.setattr(core, "evaluate_job_features", boom)
    jobs = [{"title": f"PM {n}", "company": "ACME", "source": "LinkedIn"} for n in range(50)]
    ok, aborted = core.evaluate_batch(jobs, decider=lambda f: ("Skip", ""))

    assert ok == []
    assert aborted is True
    assert "refusing this runner" in capsys.readouterr().out


def test_a_healthy_run_is_untouched(limiter, monkeypatch):
    """The guard must not fire on an ordinary run with the odd 429."""
    limiter.record_giveup()
    seen = []
    monkeypatch.setattr(core, "evaluate_job_features",
                        lambda job, d, r="": seen.append(job) or
                        {"decision": "Skip", "reason": "r", "gap": "None",
                         "jd": "", "features": {"jd_quality": "ok"}})
    jobs = [{"title": "PM", "company": "ACME", "source": "LinkedIn"}]
    ok, aborted = core.evaluate_batch(jobs, decider=lambda f: ("Skip", ""))
    assert len(seen) == 1
    assert aborted is False
