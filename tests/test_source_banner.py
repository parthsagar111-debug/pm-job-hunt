"""The run banner's source list.

It read "LinkedIn · Naukri · Hirist · IIMJobs" for two days after Wellfound was
added, and printed that during a run where Wellfound was the ONLY source scraped.
Harmless on its own, but it is the same shape as the dedup allowlist that omitted
wellfound.com and cost real money: a hand-written copy of a list the registry
already holds, with nothing to notice when the two diverge.
"""

import importlib
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core_eval_hosted as core

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def banner(monkeypatch, capsys, sources_only=None):
    """Run main() far enough to print the banner, then stop."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub")
    if sources_only is None:
        monkeypatch.delenv("SOURCES_ONLY", raising=False)
    else:
        monkeypatch.setenv("SOURCES_ONLY", sources_only)
    import pm_eval_hosted as pm
    pm = importlib.reload(pm)

    monkeypatch.setattr(pm, "SPREADSHEET_ID", "sheet")
    monkeypatch.setattr(pm, "load_candidate_profile", lambda: "x" * 300)
    monkeypatch.setattr(pm, "preflight_api_check", lambda: None)
    monkeypatch.setattr(pm, "load_seen_urls",
                        lambda *a, **k: (_ for _ in ()).throw(SystemExit("stop")))
    with pytest.raises(SystemExit):
        pm.main()
    return capsys.readouterr().out


def test_the_banner_names_every_registered_source(monkeypatch, capsys):
    out = banner(monkeypatch, capsys)
    line, = [l for l in out.splitlines() if l.strip().startswith("Sources:")]
    for name, _ in core.SOURCES:
        assert name in line, f"{name} is registered but missing from the banner"


def test_wellfound_specifically_is_in_it(monkeypatch, capsys):
    """The regression by name, since this is the one that went stale."""
    assert "Wellfound" in banner(monkeypatch, capsys)


def test_the_banner_names_only_what_will_be_scraped(monkeypatch, capsys):
    out = banner(monkeypatch, capsys, "Wellfound")
    line, = [l for l in out.splitlines() if l.strip().startswith("Sources:")]
    assert "Wellfound" in line
    assert "LinkedIn" not in line and "Naukri" not in line
    assert "skipped" in line


def test_a_bad_source_name_aborts_before_any_setup_work(monkeypatch, capsys):
    """Resolving sources must come before the profile load, the API preflight and
    the dedup read — a typo should cost nothing, not a Sheets round trip."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub")
    monkeypatch.setenv("SOURCES_ONLY", "Welfound")
    import pm_eval_hosted as pm
    pm = importlib.reload(pm)

    touched = []
    monkeypatch.setattr(pm, "SPREADSHEET_ID", "sheet")
    monkeypatch.setattr(pm, "load_candidate_profile", lambda: touched.append("profile"))
    monkeypatch.setattr(pm, "preflight_api_check", lambda: touched.append("api"))
    monkeypatch.setattr(pm, "load_seen_urls", lambda *a, **k: touched.append("sheets"))

    with pytest.raises(SystemExit):
        pm.main()
    assert touched == [], f"work done before validating SOURCES_ONLY: {touched}"


@pytest.mark.parametrize("path", ["pm_eval_hosted.py", "core_eval_hosted.py"])
def test_no_print_hand_writes_the_source_list(path):
    """Prose may quote the stale list to explain the bug; a print may not emit it.

    The first version of this test excluded any line containing `Hirist · IIMJobs"`
    so it would skip the docstring. That substring also appears in the print the
    test exists to catch, so it passed against a deliberately reintroduced
    regression. Match on what a line DOES, not on text it happens to contain.
    """
    body = io.open(os.path.join(HERE, path), encoding="utf-8").read()
    offenders = [l.strip() for l in body.splitlines()
                 if "print(" in l and _names_in(l) >= 2]
    assert not offenders, f"print() hand-writes the source list in {path}: {offenders}"


def _names_in(line: str) -> int:
    """How many registered source names a line mentions.

    One is fine — every fetcher prints its own progress, e.g. "[Naukri] 12 found".
    Two or more in a single print is the banner bug: a list of sources written out
    by hand next to a registry that already knows them.
    """
    return sum(1 for name, _ in core.SOURCES if name in line)


@pytest.mark.parametrize("line,flagged", [
    ('print(f"  Sources: LinkedIn · Naukri · Hirist · IIMJobs")', True),
    ('print(f"  [Naukri] {len(hits)} found")',                    False),
    ('print(f"  [Wellfound] {n} listing(s)")',                    False),
])
def test_the_guard_separates_a_source_list_from_a_progress_line(line, flagged):
    """Pin the check itself: its first version excluded a substring that also
    appeared in the regression, so it passed against the very bug it guarded."""
    assert ("print(" in line and _names_in(line) >= 2) is flagged
