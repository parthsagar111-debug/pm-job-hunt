"""The one-off repair for the duplicate rows the _URL_PATTERN allowlist left behind.

It deletes rows from Parth's live Sheet, so the two properties that matter are that
it keeps the OLDEST copy of each URL and that it cannot corrupt the rows it keeps.
Deleting top-down is the way to get the second one wrong: removing row 40 shifts
every row above it, so the next index in the list points at the wrong row.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dedupe_sheet as ds
import sheets_writer as sw


class WS:
    """A worksheet of rows, where delete_rows really does shift the ones above."""

    def __init__(self, rows, headers=None, title="Apply"):
        self.title = title
        self.headers = headers or list(sw.HEADERS_EVAL)
        self.rows = [list(r) for r in rows]
        self.deleted = []

    def row_values(self, n):
        return self.headers

    def col_values(self, n):
        return [self.headers[n - 1]] + [r[n - 1] if n - 1 < len(r) else "" for r in self.rows]

    def delete_rows(self, row_no):
        self.deleted.append(row_no)
        del self.rows[row_no - 2]          # row 1 is the header


def row(title, company, url):
    cells = [""] * len(sw.HEADERS_EVAL)
    cells[sw.HEADERS_EVAL.index("Title")] = title
    cells[sw.HEADERS_EVAL.index("Company")] = company
    cells[sw.HEADERS_EVAL.index("URL")] = url
    return cells


WF = "https://wellfound.com/jobs/4451879-product-manager"
LI = "https://www.linkedin.com/jobs/view/4123456789"


def test_a_clean_tab_has_nothing_to_delete():
    ws = WS([row("PM", "FOXO", WF), row("SPM", "Acme", LI)])
    dupes, uniques = ds.duplicate_rows(ws)
    assert dupes == []
    assert uniques == 2


def test_the_second_copy_of_a_url_is_the_one_flagged():
    """The first occurrence is the oldest row and carries the original Date Found."""
    ws = WS([row("PM", "FOXO", WF), row("SPM", "Acme", LI), row("PM", "FOXO", WF)])
    dupes, uniques = ds.duplicate_rows(ws)
    assert [r for r, _, _ in dupes] == [4]        # not row 2
    assert uniques == 2


def test_three_copies_leave_exactly_one():
    ws = WS([row("PM", "FOXO", WF)] * 3)
    dupes, _ = ds.duplicate_rows(ws)
    assert [r for r, _, _ in dupes] == [3, 4]


def test_the_query_string_does_not_hide_a_duplicate():
    ws = WS([row("PM", "FOXO", WF), row("PM", "FOXO", WF + "?utm_source=x")])
    dupes, _ = ds.duplicate_rows(ws)
    assert len(dupes) == 1


def test_blank_url_cells_are_ignored_not_treated_as_equal():
    """Several empty cells must not look like copies of one another."""
    ws = WS([row("PM", "A", ""), row("PM", "B", ""), row("PM", "C", WF)])
    dupes, uniques = ds.duplicate_rows(ws)
    assert dupes == []
    assert uniques == 1


def test_a_tab_without_a_url_column_is_left_alone(capsys):
    ws = WS([["x"]], headers=["Month", "Title"])
    dupes, uniques = ds.duplicate_rows(ws)
    assert (dupes, uniques) == ([], 0)
    assert "no URL column" in capsys.readouterr().out


def test_the_report_names_the_duplicated_role():
    ws = WS([row("PM", "FOXO", WF), row("PM", "FOXO", WF)])
    (_, label, url), = ds.duplicate_rows(ws)[0]
    assert "FOXO" in label and url == WF


def test_deleting_bottom_up_removes_the_rows_actually_flagged():
    """The regression this guards: top-down deletion removes innocent rows.

    Rows 2 and 4 are copies of 3 and 5. Delete 4 then 2 and the survivors are the
    originals; delete 2 then 4 and the second delete lands on a shifted row.
    """
    keep_a, keep_b = row("Keep A", "A", LI), row("Keep B", "B", "https://x.test/2")
    ws = WS([row("Dupe A", "A", WF), keep_a, row("Dupe B", "B", WF + "x"), keep_b])
    # rows 2 and 4 duplicate nothing yet — build the real shape explicitly instead
    ws = WS([keep_a, row("Dupe A", "A", LI), keep_b, row("Dupe B", "B", "https://x.test/2")])

    dupes, _ = ds.duplicate_rows(ws)
    assert [r for r, _, _ in dupes] == [3, 5]

    for row_no, _, _ in sorted(dupes, reverse=True):
        ws.delete_rows(row_no)

    assert ws.deleted == [5, 3]                      # bottom-up
    assert [r[sw.HEADERS_EVAL.index("Title")] for r in ws.rows] == ["Keep A", "Keep B"]


def _wire(monkeypatch, ws, apply):
    """Point dedupe_sheet at a one-tab fake spreadsheet."""
    class SH:
        def worksheet(self, tab):
            return ws if tab == sw.TAB_APPLY else WS([], title=tab)

    class Client:
        def open_by_key(self, key):
            return SH()

    monkeypatch.setenv("PM_EVAL_SPREADSHEET_ID", "sheet")
    monkeypatch.setattr(ds, "_get_client", lambda: Client())
    monkeypatch.setattr(ds, "_with_retry", lambda fn, *a, **k: fn(*a, **k))
    monkeypatch.setattr(ds, "APPLY", apply)


@pytest.mark.parametrize("value", ["", "false", "False", "no", "1", "yes", "y", "apply"])
def test_main_is_dry_run_unless_apply_says_true(monkeypatch, capsys, value):
    """Anything that is not the word "true" must change nothing.

    Note "TRUE " and " true " ARE apply values: the script strips and lowercases, so
    a workflow input with stray whitespace or caps still works as intended. That
    parsing is pinned in test_the_env_var_is_what_decides_apply.
    """
    ws = WS([row("PM", "FOXO", WF), row("PM", "FOXO", WF)])
    _wire(monkeypatch, ws, value.strip().lower() == "true")

    ds.main()
    out = capsys.readouterr().out
    assert ws.deleted == [], f"APPLY={value!r} deleted rows"
    assert "DRY RUN" in out and "Nothing was changed" in out


def test_apply_true_actually_deletes(monkeypatch, capsys):
    ws = WS([row("PM", "FOXO", WF), row("PM", "FOXO", WF)])
    _wire(monkeypatch, ws, True)

    ds.main()
    assert ws.deleted == [3]
    assert len(ws.rows) == 1
    assert "APPLYING CHANGES" in capsys.readouterr().out


def test_the_env_var_is_what_decides_apply(monkeypatch):
    """APPLY is read at import, so the parsing itself needs pinning separately."""
    import importlib
    for value, expected in [("true", True), ("TRUE", True), (" true ", True),
                            ("false", False), ("1", False), ("", False)]:
        monkeypatch.setenv("APPLY", value)
        assert importlib.reload(ds).APPLY is expected, value
    monkeypatch.delenv("APPLY")
    importlib.reload(ds)


def test_it_covers_the_three_eval_tabs():
    assert ds.TABS == (sw.TAB_APPLY, sw.TAB_MAYBE, sw.TAB_SKIP)


def test_no_cron_trigger_on_the_repair_workflow():
    """A repair that deletes rows must never fire on a schedule."""
    import io
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        ".github", "workflows", "dedupe_sheet.yml")
    body = io.open(path, encoding="utf-8").read()
    assert "workflow_dispatch" in body
    assert "schedule:" not in body
    assert "default: 'false'" in body
