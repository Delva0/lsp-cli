"""The text listing obeys the same kind selection as structured results."""

import pytest

from .test_actions import invoke, query

SOURCE = """from external import sentinel
VALUE = 3
class Worker:
    def run(self, arg):
        local = arg
        return local
def prepare(): return 1
"""


@pytest.mark.parametrize("selector", [("*",), ("Worker",), ("-e", "Worker|prepare")])
def test_text_list_contains_only_selected_kinds(selector):
    args = ("list", *selector, "-r", "--kinds", "function", "method")
    result = query(*args, source=SOURCE)
    code, out, err = invoke(*args, source=SOURCE)
    assert code == 0, err
    rows = [line.split("\t") for line in out.splitlines() if line]
    assert rows and all(len(row) == 3 for row in rows), out
    assert {(row[0], row[1]) for row in rows} == {
        (row["symbol"], row["kind"]) for row in result["results"]
    }
    assert all(row[1] in {"function", "method"} for row in rows)


def test_kind_filter_without_matches_has_no_text_rows():
    code, out, err = invoke("list", "VALUE", "--kinds", "method", source=SOURCE)
    assert code == 0, err
    assert not out.strip()
