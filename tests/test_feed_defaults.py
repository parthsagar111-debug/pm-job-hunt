"""Each feed's default scrape window, and which feeds are on a schedule.

PM Eval runs every 30 minutes from cron-job.org, so its window is the last 24 hours:
a wider one would re-fetch the same jobs 48 times a day. Gulf and Global Visa are run
by hand whenever wanted, so between two runs there is no "since yesterday" to speak
of and a 7-day window is the right default.

Getting this backwards is silent and expensive in both directions — a 24h manual run
quietly misses six days, and a week-wide scheduled run burns the API on repeats.
"""

import io
import os
import sys

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")


def workflow(name):
    with io.open(os.path.join(WORKFLOWS, name), encoding="utf-8") as fh:
        d = yaml.safe_load(fh)
    # PyYAML reads the bare key `on:` as the boolean True
    return d, (d.get("on") or d.get(True))


@pytest.mark.parametrize("name,expected", [
    ("pm_eval.yml", "24h"),
    ("gulf_eval.yml", "week"),
    ("global_visa.yml", "week"),
])
def test_the_workflow_default_window(name, expected):
    d, triggers = workflow(name)
    assert triggers["workflow_dispatch"]["inputs"]["time_range"]["default"] == expected
    # cron-job.org calls the API with no inputs at all, so the fallback matters as much
    # as the dropdown default.
    assert f"|| '{expected}'" in d["jobs"]["run"]["steps"][-1]["env"]["TIME_RANGE"]


@pytest.mark.parametrize("name", ["pm_eval.yml", "gulf_eval.yml", "global_visa.yml"])
def test_no_feed_uses_githubs_own_scheduler(name):
    """Every trigger is external: GitHub's `schedule:` was never wanted here."""
    _, triggers = workflow(name)
    assert list(triggers) == ["workflow_dispatch"]


@pytest.mark.parametrize("name", ["gulf_eval.yml", "global_visa.yml"])
def test_both_windows_stay_offered(name):
    """Defaulting to week must not remove the ability to ask for 24h."""
    _, triggers = workflow(name)
    assert set(triggers["workflow_dispatch"]["inputs"]["time_range"]["options"]) == {"week", "24h"}


@pytest.mark.parametrize("module,expected", [
    ("pm_eval_hosted", "24h"),
    ("gulf_eval_hosted", "week"),
    ("global_visa_hosted", "week"),
])
def test_the_script_default_matches_its_workflow(module, expected, monkeypatch):
    """A mismatch here is how a feed silently runs the wrong window locally, or if the
    env var is ever dropped from the workflow."""
    monkeypatch.delenv("TIME_RANGE", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub")
    import importlib
    mod = importlib.import_module(module)
    importlib.reload(mod)
    assert mod.TIME_RANGE == expected
