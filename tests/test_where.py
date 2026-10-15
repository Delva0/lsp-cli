"""Position queries expose complete lexical ancestry in both output formats."""

import json

import pytest

from .test_cli import SPAN, invoke

NESTED = """class Outer:
    class Inner:
        def run(self):
            def local():
                return 1
            return local()
"""


def test_where_text_and_json_expose_all_ancestors_with_ranges():
    args = ["where", "--line", "5", "--stdin-filename", "pkg/example.py"]
    result = invoke(*args, "--json", source=NESTED)
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    assert body["position"] == {"line": 5, "column": None}
    rows = body["results"]
    assert [r["symbol"] for r in rows] == [
        "pkg.example", "pkg.example.Outer", "pkg.example.Outer.Inner",
        "pkg.example.Outer.Inner.run", "pkg.example.Outer.Inner.run.local",
    ]
    assert [r["parent"] for r in rows] == [None, *(r["symbol"] for r in rows[:-1])]
    assert [(r["line"], r["end_line"]) for r in rows] == [(1, 6), (1, 6), (2, 6), (3, 6), (4, 5)]
    assert all(r.keys() >= SPAN and r["source"] == "pkg/example.py" for r in rows)
    text = invoke(*args, source=NESTED).stdout
    assert text.startswith("pkg/example.py:5\nmodule pkg.example  1:0-6:26\n")
    lines = text.splitlines()[1:]
    assert len(lines) == len(rows)
    for row, line in zip(rows, lines, strict=True):
        assert f"{row['kind']} {row['symbol']}  " in line
        assert line.endswith(f"{row['line']}:{row['column']}-{row['end_line']}:{row['end_column']}")
    assert lines[-1].startswith("            └── ")


def test_where_sibling_lambdas_remain_siblings_and_column_disambiguates():
    code = "values = (lambda: 1, lambda: 2)\n"
    rows = json.loads(invoke("where", "--line", "1", "--json", source=code).stdout)["results"]
    assert len(rows) == 3
    assert [r["parent"] for r in rows] == [None, "__stdin__", "__stdin__"]
    text = invoke("where", "--line", "1", source=code).stdout
    assert "\n├── " in text and "\n└── " in text
    selected = json.loads(invoke("where", "--line", "1", "--column", "21", "--json", source=code).stdout)
    assert selected["results"] == [rows[0], rows[2]]
    assert selected["position"] == {"line": 1, "column": 21}


def test_where_decorator_belongs_to_decorated_method():
    code = "class C:\n    @staticmethod\n    def run():\n        return 1\n"
    result = invoke("where", "--line", "2", "--column", "4", "--json", source=code)
    rows = json.loads(result.stdout)["results"]
    assert [r["symbol"] for r in rows] == ["__stdin__", "__stdin__.C", "__stdin__.C.run"]
    assert (rows[-1]["line"], rows[-1]["column"]) == (2, 4)


def test_where_column_boundary_is_exclusive_and_unicode_based():
    code = 'def café(): return "你"\noutside = 2\n'
    end = len(code.splitlines()[0])
    before = json.loads(invoke("where", "--line", "1", "--column", str(end - 1), "--json", source=code).stdout)
    after = json.loads(invoke("where", "--line", "1", "--column", str(end), "--json", source=code).stdout)
    assert [r["symbol"] for r in before["results"]] == ["__stdin__", "__stdin__.café"]
    assert before["results"][-1]["end_column"] == end
    assert [r["symbol"] for r in after["results"]] == ["__stdin__"]


def test_where_groups_each_input_source_hierarchy_separately(tmp_path):
    for name in ("one.py", "two.py"):
        (tmp_path / name).write_text("class C:\n    def run(self): return 1\n")
    args = ["where", "--line", "2", "--file", "one.py", "--file", "two.py"]
    result = invoke(*args, "--json", cwd=tmp_path, source="")
    rows = json.loads(result.stdout)["results"]
    assert [(r["source"], r["symbol"]) for r in rows] == [
        ("one.py", "one"), ("one.py", "one.C"), ("one.py", "one.C.run"),
        ("two.py", "two"), ("two.py", "two.C"), ("two.py", "two.C.run"),
    ]
    text = invoke(*args, cwd=tmp_path, source="").stdout
    assert "one.py:2\nmodule one" in text and "two.py:2\nmodule two" in text


def test_where_reports_no_scope_outside_input():
    result = invoke("where", "--line", "999", "--json")
    assert result.returncode == 0 and json.loads(result.stdout)["results"] == []
    assert invoke("where", "--line", "999").stdout.strip() == "no matches"


@pytest.mark.parametrize("command", ["call", "calls", "enclosing"])
def test_obsolete_command_names_are_not_public_aliases(command):
    result = invoke(command, "--json", source="not Python")
    assert result.returncode == 2
    assert "invalid choice" in json.loads(result.stdout)["error"]["message"]
