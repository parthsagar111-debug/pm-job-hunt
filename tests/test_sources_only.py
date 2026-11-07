"""SOURCES_ONLY — restricting a manual run to named sources.

Added 2026-11-07 for a one-off Wellfound-only run. The property that matters most
is the negative one: the crons send no inputs, so SOURCES_ONLY arrives empty and
must mean "every source", never "none".
"""

import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core_eval_hosted as core


def reload_pm(monkeypatch, value=None):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub")
    if value is None:
        monkeypatch.delenv("SOURCES_ONLY", raising=False)
    else:
        monkeypatch.setenv("SOURCES_ONLY", value)
    import pm_eval_hosted as pm
    return importlib.reload(pm)


@pytest.mark.parametrize("value", [None, "", "   ", ",", " , ,"])
def test_an_unset_or_empty_value_means_every_source(monkeypatch, value):
    """What the crons send. Reading this as "no sources" would silence the feed."""
    pm = reload_pm(monkeypatch, value)
    assert pm.SOURCES_ONLY == []
    assert [n for n, _ in pm.selected_sources()] == [n for n, _ in core.SOURCES]


def test_one_source_is_selected(monkeypatch):
    pm = reload_pm(monkeypatch, "Wellfound")
    assert [n for n, _ in pm.selected_sources()] == ["Wellfound"]


def test_the_fetcher_selected_is_the_real_one(monkeypatch):
    """Selecting by name must not hand back some other source's function."""
    pm = reload_pm(monkeypatch, "Wellfound")
    (name, fn), = pm.selected_sources()
    assert fn is core.fetch_wellfound


def test_several_sources_keep_registry_order(monkeypatch):
    """Not the order they were typed — the order the feed scrapes in."""
    pm = reload_pm(monkeypatch, "Wellfound, LinkedIn")
    assert [n for n, _ in pm.selected_sources()] == ["LinkedIn", "Wellfound"]


@pytest.mark.parametrize("value", ["wellfound", "WELLFOUND", " WellFound "])
def test_the_name_is_case_and_space_insensitive(monkeypatch, value):
    pm = reload_pm(monkeypatch, value)
    assert [n for n, _ in pm.selected_sources()] == ["Wellfound"]


def test_an_unknown_name_aborts_instead_of_scraping_nothing(monkeypatch):
    """A typo would otherwise be indistinguishable from a quiet day."""
    pm = reload_pm(monkeypatch, "Welfound")
    with pytest.raises(SystemExit) as e:
        pm.selected_sources()
    assert "Welfound" in str(e.value)
    assert "Wellfound" in str(e.value)       # the message lists what IS valid


def test_one_bad_name_among_good_ones_still_aborts(monkeypatch):
    pm = reload_pm(monkeypatch, "Wellfound,Nonsense")
    with pytest.raises(SystemExit):
        pm.selected_sources()


def test_the_workflow_passes_it_through_and_defaults_to_blank():
    import io
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        ".github", "workflows", "pm_eval.yml")
    body = io.open(path, encoding="utf-8").read()
    assert "sources_only:" in body
    assert "SOURCES_ONLY:" in body
    # No `|| 'something'` fallback: blank must stay blank so the crons get all sources.
    assert "${{ inputs.sources_only }}" in body
