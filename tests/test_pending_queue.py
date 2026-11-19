"""Global Visa: jobs whose JD LinkedIn refused are queued, not dropped.

On 16-19 Nov 2026 three Global Visa runs in a row wrote 0 rows. Each found 300-650
roles, then LinkedIn throttled the runner past the 30-minute backoff budget and the
breaker skipped every remaining JD fetch: 267, 567 and 330 JDs never read. Those jobs
were counted as "failed" and forgotten, and since each run only searches the last
24h, most were never seen again. The runs still finished green.
"""

import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core_eval_hosted as core
import global_visa_hosted as gv


def job(n: int, **kw) -> dict:
    url = f"https://www.linkedin.com/jobs/view/{40000000 + n}"
    return {"source": "LinkedIn Global", "job_id": f"li_{40000000 + n}", "url": url,
            "title": f"Product Manager {n}", "company": f"Co{n}", "location": "London", **kw}


TODAY = datetime(2026, 11, 19)


# ── merge / prune / defer ──────────────────────────────────────────────────

def test_queued_jobs_come_first_and_skip_what_the_sheet_already_has():
    found   = [job(1), job(2)]
    pending = [job(3, attempts=1, deferred="2026-11-18"), job(4, attempts=1, deferred="2026-11-18")]
    merged  = gv.merge_pending(found, pending, seen={job(4)["url"]})
    assert [j["url"] for j in merged] == [job(3)["url"], job(1)["url"], job(2)["url"]]


def test_a_queued_job_found_again_keeps_its_history_and_is_not_doubled():
    found  = [job(1)]
    merged = gv.merge_pending(found, [job(1, attempts=2, deferred="2026-11-16")], seen=set())
    assert len(merged) == 1
    assert (merged[0]["attempts"], merged[0]["deferred"]) == (2, "2026-11-16")


def test_defer_counts_attempts_and_keeps_the_first_date():
    j = gv.defer(job(1), TODAY)
    assert (j["attempts"], j["deferred"]) == (1, "2026-11-19")
    j = gv.defer(dict(j), datetime(2026, 11, 20))
    assert (j["attempts"], j["deferred"]) == (2, "2026-11-19")


def test_prune_drops_jobs_tried_too_often_or_queued_too_long():
    keep, dropped = gv.prune_pending([
        job(1, attempts=1, deferred="2026-11-19"),
        job(2, attempts=gv.PENDING_MAX_ATTEMPTS, deferred="2026-11-19"),
        job(3, attempts=1, deferred="2026-11-01"),
    ], TODAY)
    assert [j["url"] for j in keep] == [job(1)["url"]]
    assert dropped == 2


# ── main(): what gets queued, and the exit code ────────────────────────────

@pytest.fixture
def run(monkeypatch):
    """Runs gv.main() against fakes. `fetch` decides each JD; returns what was saved."""
    lim = core.RateLimiter()
    monkeypatch.setattr(gv, "LI_LIMITER", lim)
    monkeypatch.setattr(gv, "SPREADSHEET_ID", "sheet")
    monkeypatch.setattr(gv, "TIME_RANGE", "24h")
    monkeypatch.setattr(gv, "_load_api_key", lambda: "k")
    monkeypatch.setattr(gv, "load_seen_urls", lambda *a, **k: set())
    monkeypatch.setattr(gv, "classify", lambda j, e: ("YES", "we sponsor visas"))
    monkeypatch.setattr(gv, "print_token_report", lambda n: None)

    class Reg:
        errors = []
        def lookup(self, *a): return ""
    monkeypatch.setattr(gv, "SponsorRegistry", Reg)

    out = {"saved": None, "queue": None, "replace": None, "push": None}
    monkeypatch.setattr(gv, "save_visa_jobs",
                        lambda sid, k, r: (out.__setitem__("saved", [j["url"] for j in k]), (len(k), len(r)))[1])

    def save_q(sid, jobs, replace=True):
        out["queue"], out["replace"] = [dict(j) for j in jobs], replace
        return len(jobs)
    monkeypatch.setattr(gv, "save_pending_jobs", save_q)
    monkeypatch.setattr(gv, "push", lambda t, m, p="default": out.__setitem__("push", t))

    def go(found, fetch, pending=(), pending_error=None):
        monkeypatch.setattr(gv, "collect_jobs", lambda seen: [dict(j) for j in found])
        def load(sid):
            if pending_error:
                raise pending_error
            return [dict(j) for j in pending]
        monkeypatch.setattr(gv, "load_pending_jobs", load)
        monkeypatch.setattr(gv, "fetch_jd_guest", lambda jid: fetch(jid, lim))
        try:
            gv.main()
            out["exit"] = 0
        except SystemExit as e:
            out["exit"] = e.code
        return out
    return go


JD = "We sponsor visas for this role.\n" + "x" * 200


def test_a_block_mid_run_queues_the_rest_and_fails_the_run(run):
    def fetch(jid, lim):
        if jid.endswith("2"):                 # 2nd job: the give-up that trips the breaker
            lim.giveups += 1
            lim.backoff_s = core.LI_BACKOFF_BUDGET_S
            return ""
        return JD
    out = run([job(1), job(2), job(3), job(4)], fetch)
    assert out["saved"] == [job(1)["url"]]
    assert [j["url"] for j in out["queue"]] == [job(2)["url"], job(3)["url"], job(4)["url"]]
    assert all(j["attempts"] == 1 for j in out["queue"])
    assert out["exit"] == 1
    assert "blocked" in out["push"]


def test_a_single_429_give_up_is_queued_but_a_404_is_not(run):
    def fetch(jid, lim):
        if jid.endswith("1"):
            lim.giveups += 1                  # refused, breaker still closed
            return ""
        if jid.endswith("2"):
            return ""                         # 404 / no description: a real answer
        lim.reward()
        return JD
    out = run([job(1), job(2), job(3)], fetch)
    assert [j["url"] for j in out["queue"]] == [job(1)["url"]]
    assert out["exit"] == 0


def test_a_healthy_run_drains_the_queue(run):
    out = run([job(1)], lambda jid, lim: JD, pending=[job(9, attempts=1, deferred="2026-11-18")])
    assert out["saved"] == [job(9)["url"], job(1)["url"]]
    assert out["queue"] == [] and out["replace"] is True
    assert out["exit"] == 0


def test_a_healthy_run_with_no_queue_does_not_touch_the_tab(run):
    out = run([job(1)], lambda jid, lim: JD)
    assert out["queue"] is None


def test_an_unreadable_queue_is_appended_to_not_overwritten(run):
    def fetch(jid, lim):
        lim.giveups += 1
        return ""
    out = run([job(1)], fetch, pending_error=RuntimeError("503"))
    assert out["replace"] is False
    assert [j["url"] for j in out["queue"]] == [job(1)["url"]]
