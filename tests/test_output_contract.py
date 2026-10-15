"""Located targets survive content selection; normal misses never become errors."""

import pytest

from pyrel.semantics import ROLES

from .test_action_roles import commit_source
from .test_actions import invoke, query
from .test_git_sources import git, write
from .test_git_sources import repository as repository  # noqa: PLC0414


@pytest.mark.parametrize("args", [
    ("list", "v1:Absent"), ("show", "v1:Absent"), ("refs", "v1:Absent"),
    ("inheritance", "v1:Absent"), ("path", "v1:Absent", "AlsoAbsent"),
    ("history", "v1:Absent"), ("diff", "v1..v2:Absent"),
    ("where", "v1:absent*.py:1"), ("complete", "v1:absent*.py:1:0"),
])
def test_all_actions_are_silent_for_unmatched_targets(repository, args):
    args = (*args, "--repo", str(repository))
    status, out, err = invoke(*args)
    assert (status, out, err) == (1, "", "")
    result = query(*args, status=1)
    assert result["queries"][0]["status"] == "no_match"
    assert not result["heads"] and not result["results"]
    assert "error" not in result and "errors" not in result["queries"][0]


@pytest.mark.parametrize("role", ROLES)
def test_every_show_role_preserves_headers_without_role_hits(role):
    source = "class Worker:\n    async def run(self): pass\n"
    args = ("show", "Worker.run", "--roles", role)
    result = query(*args, source=source)
    assert result["heads"][0]["symbol"] == "__stdin__.Worker.run"
    assert result["queries"][0]["status"] == "empty"
    assert result["occurrences"] == []
    assert "\n".join(r["code"] for r in result["results"]) == "class Worker:\n    async def run(self):"
    status, out, err = invoke(*args, source=source)
    assert status == 0 and not err and "pass" not in out


def test_role_union_does_not_expect_every_role_to_match():
    source = "def leaf(): pass\ndef run():\n    leaf()\n"
    alone = query("show", "run", "--roles", "call", source=source)
    union = query("show", "run", "--roles", "doc", "call", "doc", source=source)
    assert union["occurrences"] == alone["occurrences"]
    assert union["results"] == alone["results"]
    assert union["queries"][0]["status"] == "ok"
    status, out, err = invoke("show", "run", "--roles", "doc", "call", source=source)
    assert status == 0 and "leaf()" in out and not err


def test_header_spans_retain_unicode_multiline_decorators_and_control_context():
    source = (
        "if True:\r\n"
        "    @decorate\r\n"
        "    async def café(\r\n"
        "        value: dict[str, int] = {'x': 1},\r\n"
        "    ) -> int: return 42\r\n"
    )
    result = query("show", "café", "--roles", "doc", source=source)
    assert not result["occurrences"]
    code = "\n".join(r["code"] for r in result["results"])
    assert all(part in code for part in ("if True:", "@decorate", "async def café", "{'x': 1}", ") -> int:"))
    assert "return 42" not in code
    lines = source.splitlines(keepends=True)
    for row in result["results"]:
        original = "".join(lines[row["line"] - 1:row["end_line"] - 1])
        original += lines[row["end_line"] - 1][:row["end_column"]]
        assert row["code"] == original


def test_module_identity_survives_empty_roles():
    target = "__stdin__"
    result = query("show", target, "--roles", "doc", source="VALUE = 42\n")
    assert result["heads"] and not result["occurrences"] and not result["results"]
    status, out, err = invoke("show", target, "--roles", "doc", source="VALUE = 42\n")
    assert (status, out, err) == (0, "<stdin>\n", "")
    assert "VALUE = 42" not in out


@pytest.mark.parametrize("action", ["show", "refs"])
@pytest.mark.parametrize(("source", "target", "expected"), [
    ('PoolKey = collections.namedtuple("PoolKey", fields)\n', "PoolKey",
     'PoolKey = collections.namedtuple("PoolKey", fields)'),
    ('VALUES: tuple[str, ...] = (\n    "hidden",\n)\n', "VALUES", "VALUES: tuple[str, ...] = ("),
    ('class C:\n    café = make(\n        "hidden",\n    )\n', "C.café", "class C:\n    café = make("),
    ('def run():\n    if ready:\n        item = build(\n            "hidden",\n        )\n',
     "run.item", "def run():\n    if ready:\n        item = build("),
    ("from collections import namedtuple as make\n", "make", "from collections import namedtuple as make"),
])
def test_empty_roles_keep_binding_declarations_without_inventing_hits(action, source, target, expected):
    role = "doc" if action == "show" else "override"
    args = (action, target, "--roles", role)
    result = query(*args, source=source)
    assert result["queries"][0]["status"] == "empty"
    assert not result["occurrences"]
    assert "\n".join(row["code"] for row in result["results"]) == expected
    status, out, err = invoke(*args, source=source)
    assert status == 0 and "hidden" not in out
    if target == "make":
        assert "unresolved" in err and "showing its import" in err
    else:
        assert not err
    assert "\n".join(line.split(" | ", 1)[1] for line in out.splitlines()[1:]) == expected


def test_batched_heads_share_flat_source_without_repeating_or_losing_inline_suites():
    source = "class C:\n    def empty(self): pass\n    def work(self): return 1\n"
    result = query("show", "C", "C.work", "--roles", "return", source=source)
    assert len(result["heads"]) == 2
    assert "return 1" in "\n".join(r["code"] for r in result["results"])
    assert not any("pass" in r["code"] for r in result["results"])
    status, out, err = invoke("show", "C", "C.work", "--roles", "return", source=source)
    assert status == 0 and not err
    assert out.count("def work(self): return 1") == 1


@pytest.mark.parametrize("roles", [(), ("--roles", "doc")])
def test_many_symbols_share_one_file_label_and_original_source_order(roles):
    source = "".join(f"def task_{i}():\n    return {i}\n" for i in range(100))
    targets = [f"task_{i}" for i in reversed(range(100))]
    status, out, err = invoke("show", *targets, "task_0", *roles, source=source)
    assert status == 0 and not err
    assert [line for line in out.splitlines() if " | " not in line] == ["<stdin>"]
    assert "__stdin__." not in out
    assert [int(line.partition(" |")[0]) for line in out.splitlines()[1:]] == (
        list(range(1, 201, 2)) if roles else list(range(1, 201))
    )
    assert out.count("def task_0():") == 1


def test_refs_merge_overlapping_targets_with_one_file_label():
    source = "def first(): pass\ndef second(): pass\ndef caller():\n    first(); second()\n"
    status, out, err = invoke("refs", "first", "second", "--roles", "call", source=source)
    assert status == 0 and not err
    assert out.count("<stdin>") == 1 and "__stdin__." not in out
    assert out.count("first(); second()") == 1
    assert all(declaration in out for declaration in ("def first():", "def second():", "def caller():"))
    assert "pass" not in out


@pytest.mark.parametrize("targets", [("work.arg", "work"), ("work", "work.arg")])
def test_header_only_parameter_and_full_operation_share_one_original_line(targets):
    source = "def work(arg): return arg\r\n"
    result = query("show", *targets, "--roles", "return", source=source)
    assert len(result["results"]) == 1
    assert result["results"][0]["code"] == source.rstrip("\r\n")
    assert result["results"][0]["end_column"] == len(source.rstrip("\r\n"))
    assert len(result["occurrences"]) == 1
    states = {q["pattern"]: q["status"] for q in result["queries"]}
    assert states == {"work": "ok", "work.arg": "empty"}


def test_refs_zero_hits_keeps_declaration_without_returning_its_body():
    args = ("refs", "idle", "--roles", "call", "override")
    result = query(*args, source="def idle(): pass\n")
    assert result["heads"][0]["symbol"] == "__stdin__.idle"
    assert not result["occurrences"] and result["queries"][0]["status"] == "empty"
    assert result["results"][0]["code"] == "def idle():"
    status, out, err = invoke(*args, source="def idle(): pass\n")
    assert (status, out, err) == (0, "<stdin>\n1 | def idle():\n", "")


def test_list_retains_unselected_scope_metadata_only_in_json():
    source = "class C:\n    def f(self, value): return value\n"
    args = ("list", "C", "-d", "0", "--kinds", "parameter")
    result = query(*args, source=source)
    assert result["heads"][0]["symbol"] == "__stdin__.C" and not result["results"]
    assert result["matched"] == 2 and result["truncated"]
    status, out, err = invoke(*args, source=source)
    assert (status, out, err) == (0, "", "")


@pytest.mark.parametrize("args", [("path", "missing", "idle"), ("path", "idle", "missing")])
def test_path_keeps_the_located_endpoint(args):
    result = query(*args, source="def idle(): pass\n")
    assert result["heads"][0]["symbol"] == "__stdin__.idle"
    status, out, err = invoke(*args, source="def idle(): pass\n")
    assert status == 0 and "__stdin__.idle" in out and not err


def test_no_match_does_not_break_a_successful_raw_query():
    source = "def f(): pass\n"
    assert invoke("show", "missing", "--raw", source=source) == (1, "", "")
    assert invoke("show", "missing", "f", "--raw", source=source) == (0, "def f(): pass\n", "")


def test_errors_and_incomplete_source_discovery_are_not_silenced(repository):
    for args in [
        ("show", "bogus:Base"), ("show", "v1:Base", "-e", "["),
        ("where", "v1:pkg/base.py:999"), ("show", "v1:Missing", "--max-files", "1"),
    ]:
        status, _out, err = invoke(*args, "--repo", str(repository))
        assert status == 2 and err


def test_empty_diff_keeps_both_heads_without_fabricating_a_signature_change(repository):
    commit_source(repository, "def work():\n    pass\n", "headers-old")
    commit_source(repository, "def work(value):\n    return value\n", "headers-new")
    args = ("diff", "headers-old..headers-new:roles.work", "--roles", "doc", "call", "--repo", str(repository))
    result = query(*args)
    assert len(result["heads"]) == 2
    assert result["queries"][0]["status"] == "empty"
    row = result["results"][0]
    assert row["symbol_status"] == "modified" and row["status"] == "empty"
    assert not row["patch"] and not row["hunks"]
    assert not result["occurrences"]["before"] and not result["occurrences"]["after"]
    assert row["before"]["fragments"][0]["code"] == "def work():"
    assert row["after"]["fragments"][0]["code"] == "def work(value):"
    status, out, err = invoke(*args)
    assert status == 0 and not err and "\n\nheaders-new:roles.py\n" in out
    assert "def work():" in out and "def work(value):" in out
    assert "no matches" not in out and "@@" not in out


def test_diff_summarizes_unchanged_symbols_without_losing_changes_or_json(repository):
    kept = "def keep_a(): return 1\ndef keep_b(): return 2\n"
    write(repository, "other.py", "def keep_c(): return 3\ndef keep_d(): return 4\n")
    git(repository, "add", "other.py")
    commit_source(repository, kept + "def edited(): return 1\ndef removed(): return 0\n", "aggregate-old")
    commit_source(repository, kept + "def edited(): return 2\ndef added(): return 3\n", "aggregate-new")
    args = ("diff", "aggregate-old..aggregate-new:{keep_*,edited,removed,added}", "--repo", str(repository))
    result = query(*args)
    statuses = [row["status"] for row in result["results"]]
    assert sorted(statuses) == ["added", "deleted", "modified"] + ["unchanged"] * 4
    unchanged = [row for row in result["results"] if row["status"] == "unchanged"]
    assert all(row["before"]["line"] and row["after"]["line"] for row in unchanged)
    assert len({index for q in result["queries"] for index in q["result_indices"]}) == 7
    status, out, err = invoke(*args)
    assert status == 0 and not err
    assert out.count("DIFF unchanged (2 symbols)") == 2
    for row in result["results"]:
        if row["patch"]:
            assert row["patch"] in out
            assert out.index(row["patch"]) < out.index("DIFF unchanged")
    assert "aggregate-old:other.py\n\naggregate-new:other.py\nDIFF unchanged (2 symbols)" in out
    status, out, err = invoke("diff", "aggregate-old..aggregate-new:roles.keep_a", "--repo", str(repository))
    assert status == 0 and not err
    assert out == "aggregate-old:roles.py\n\naggregate-new:roles.py\nDIFF unchanged\n"


def test_unchanged_diff_keeps_both_paths_across_a_directory_move(repository):
    status, out, err = invoke("diff", "v1..v2:Worker.run", "--repo", str(repository))
    assert status == 0 and not err
    assert "v1:pkg/worker.py\n\nv2:src/pkg/worker.py\nDIFF unchanged" in out


def test_binding_declaration_changes_are_not_empty_role_diff_hits(repository):
    commit_source(repository, "VALUE = 1\n", "binding-old")
    commit_source(repository, "VALUE = 2\n", "binding-new")
    result = query("diff", "binding-old..binding-new:VALUE", "--roles", "doc", "--repo", str(repository))
    row = result["results"][0]
    assert row["symbol_status"] == "modified" and row["status"] == "empty"
    assert not row["patch"] and not row["hunks"]
    assert not result["occurrences"]["before"] and not result["occurrences"]["after"]
    assert row["before"]["fragments"][0]["code"] == "VALUE = 1"
    assert row["after"]["fragments"][0]["code"] == "VALUE = 2"


def test_show_blame_keeps_attribution_when_only_the_signature_is_selected(repository):
    commit = commit_source(repository, "def work(): pass\n", "headers-blame")
    result = query("show", "headers-blame:work", "--roles", "doc", "--blame", "--repo", str(repository))
    assert not result["occurrences"]
    assert result["results"][0]["code"] == "def work():"
    assert result["results"][0]["blame"][0]["commit"] == commit


def test_empty_completion_keeps_its_input_position(monkeypatch):
    from pyrel.workspace import Workspace

    monkeypatch.setattr(Workspace, "request", lambda *_args: {"items": [], "isIncomplete": False})
    args = ("complete", "<stdin>:1:3")
    status, out, err = invoke(*args, source='"text"\n')
    assert status == 0 and out.startswith("<stdin>:1:3-1:3") and not err
    result = query(*args, source='"text"\n')
    assert result["heads"][0]["line"] == 1 and result["heads"][0]["column"] == 3


def test_bad_python_source_is_not_reported_as_an_absent_symbol(repository):
    write(repository, "broken.py", "def broken(\n")
    git(repository, "add", "broken.py")
    git(repository, "commit", "-qm", "broken source")
    status, _out, err = invoke("show", "HEAD:Absent", "--repo", str(repository))
    assert status == 2 and "incomplete" in err


def test_shared_file_input_error_is_reported_once(tmp_path):
    missing = tmp_path / "missing.py"
    args = ("show", "first", "second", "third", "fourth", "--files", str(missing))
    status, out, err = invoke(*args)
    assert status == 2 and not out
    assert err.splitlines() == [f"lsp: No files match '{missing}' in the working tree"]
    result = query(*args, status=2)
    assert len(result["queries"]) == 4
    assert all(q["errors"][0]["scope"] == "input" for q in result["queries"])


def test_distinct_input_errors_remain_visible_alongside_successes(repository):
    args = ("show", "bad-one:Base", "bad-one:Worker", "bad-two:Base", "v1:Base", "--repo", str(repository))
    status, out, err = invoke(*args)
    assert status == 2 and "class Base" in out
    assert len(err.splitlines()) == 2
    assert "bad-one" in err and "bad-two" in err
