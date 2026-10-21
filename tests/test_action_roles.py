"""Public action contracts: scopes, relationship roles, attribution and sparse diffs."""

import pytest

from .test_actions import invoke, query
from .test_git_sources import git, write
from .test_git_sources import repository as repository  # noqa: PLC0414


def test_list_recursive_kinds_traverse_excluded_containers():
    source = """class Outer:
    class Inner:
        def work(self, item):
            value = item
            def nested():
                return value
            return nested()
class Other:
    def work(self): pass
"""
    assert not query("list", "Outer", "--kinds", "method", source=source)["results"]
    result = query("list", "Outer", "-r", "--kinds", "method", "parameter", "variable", source=source)
    names = {r["symbol"] for r in result["results"]}
    assert names == {
        "__stdin__.Outer.Inner.work",
        "__stdin__.Outer.Inner.work.self",
        "__stdin__.Outer.Inner.work.item",
        "__stdin__.Outer.Inner.work.value",
    }
    assert not result["truncated"]
    limited = query("list", "Outer", "-r", "--max-depth", "2", "--kinds", "parameter", source=source)
    assert not limited["results"] and limited["truncated"]


def test_list_directory_module_and_revision_scopes(repository):
    for scope in ("v2:src/pkg", "v2:pkg"):
        result = query("list", scope, "--repo", str(repository), "-r", "--kinds", "parameter")
        assert {r["symbol"] for r in result["results"]} == {"pkg.base.Base.ping.self", "pkg.worker.Worker.run.self"}
    module = query("list", "v2:pkg.worker", "--repo", str(repository), "-r", "--kinds", "method")
    assert [r["symbol"] for r in module["results"]] == ["pkg.worker.Worker.run"]
    assert query("list", "v2:", "--repo", str(repository), "-r")["results"]
    batch = query("list", "v1:Base", "v2:Worker", "--repo", str(repository), "-r", "--kinds", "method")
    assert len(batch["queries"]) == 2


def test_inheritance_text_keeps_mro_when_there_are_no_relationship_edges():
    status, out, err = invoke("inheritance", "A", source="class A: pass\n")
    assert status == 0 and "MRO: __stdin__.A -> builtins.object" in out
    assert "no matches" not in err
    status, out, err = invoke("inheritance", "A", source="class A(Unknown): pass\n")
    assert status == 0 and "MRO: unresolved:" in out
    assert "no matches" not in err


def test_path_text_keeps_status_for_zero_edges():
    status, out, err = invoke("path", "f", "f", source="def f(): pass\n")
    assert status == 0 and out.strip().endswith("\nfound")
    assert "no matches" not in err


@pytest.mark.parametrize("native", [True, False])
def test_inheritance_and_refs_include_real_overrides(native, monkeypatch):
    if not native:
        from lsp_cli.workspace import NeedsBindingsError, Workspace

        def unavailable(*args):
            raise NeedsBindingsError("disabled for fallback check")

        monkeypatch.setattr(Workspace, "request", unavailable)
    source = """class Base:
    def work(self): return 1
class Middle(Base): pass
class Child(Middle):
    def work(self): return 2
class Unrelated:
    def work(self): return 3
"""
    result = query("inheritance", "Base", source=source)
    assert result["mro"] == ["__stdin__.Base", "builtins.object"]
    assert set(result["descendants"]) == {"__stdin__.Middle", "__stdin__.Child"}
    refs = query("refs", "Base.work", "--roles", "override", source=source)
    assert {r["symbol"] for r in refs["occurrences"]} == {"__stdin__.Child.work"}
    assert all(r["line"] == 5 and r["target"] == "__stdin__.Base.work" for r in refs["occurrences"])
    default = query("refs", "Base.work", source=source)
    assert any("override" in r.get("roles", []) for r in default["occurrences"])
    inherit = query("refs", "Base", "--roles", "inherit", source=source)
    assert len(inherit["occurrences"]) == 1 and inherit["occurrences"][0]["line"] == 3
    assert "class Middle(Base)" in "\n".join(r["code"] for r in inherit["results"])
    assert "return 2" not in "\n".join(r["code"] for r in inherit["results"])


def test_abstract_implementation_and_override_union():
    source = """from abc import abstractmethod as abstract
class Contract:
    @abstract
    def work(self): pass
class Concrete(Contract):
    def work(self): return 1
"""
    result = query("refs", "Contract.work", "--roles", "implement", "override", "implement", source=source)
    assert len(result["occurrences"]) == 1
    assert set(result["occurrences"][0]["roles"]) == {"implement", "override"}
    status, out, _err = invoke("refs", "Contract.work", "--roles", "implement", "override", source=source)
    assert status == 0 and out.count("def work(self): return 1") == 1
    tree = query("inheritance", "Concrete.work", source=source)
    assert any(r["target"] == "__stdin__.Contract.work" for r in tree["implementations"])


def test_zero_role_matches_keep_heads_without_fabricating_occurrences():
    for action, target, role in [("show", "empty", "doc"), ("refs", "empty", "override")]:
        result = query(action, target, "--roles", role, source="def empty(): pass\n")
        assert not result["occurrences"] and not result.get("documents")
        assert result["heads"][0]["symbol"] == "__stdin__.empty"
        assert result["results"][0]["code"] == "def empty():"
        status, out, err = invoke(action, target, "--roles", role, source="def empty(): pass\n")
        assert (status, out, err) == (0, "<stdin>\n1 | def empty():\n", "")


def commit_source(repository, text, tag, path="roles.py"):
    write(repository, path, text)
    git(repository, "add", path)
    git(repository, "commit", "-qm", tag)
    git(repository, "tag", tag)
    return git(repository, "rev-parse", "HEAD").strip()


def test_show_blame_attributes_only_displayed_lines(repository):
    before = 'def work(flag):\n    """documented"""\n    if flag:\n        print(\'old\')\n    return 1\n'
    old = commit_source(repository, before, "roles-old")
    new = commit_source(repository, before.replace("'old'", "'新'"), "roles-new")
    result = query("show", "roles-new:roles.work", "--repo", str(repository), "--blame", "--roles", "call")
    rows = [line for fragment in result["results"] for line in fragment["blame"]]
    assert [r["line"] for r in rows] == [1, 3, 4]
    assert [r["commit"] for r in rows] == [old, old, new]
    assert rows[-1]["origin"]["line"] == 4 and rows[-1]["end_column"] == len("        print('新')")
    status, out, _ = invoke("show", "roles-new:roles.work", "--repo", str(repository), "--roles", "call", "--blame")
    assert status == 0 and new[:12] in out and "documented" not in out and "return 1" not in out
    assert query("show", "work", "--blame", status=2)["error"]


def test_role_diff_both_sides_add_modify_delete_and_original_spans(repository):
    before = "\n" * 4 + (
        "def work(flag):\n    if flag:\n        print('removed')\n    value = 1\n    print('old')\n    return value\n"
    )
    after = "\n" * 2 + (
        "def work(flag):\n    value = 2\n    print('new')\n    if flag:\n        print('added')\n    return value\n"
    )
    commit_source(repository, before, "diff-before")
    commit_source(repository, after, "diff-after")
    diff = query("diff", "diff-before..diff-after:roles.work", "--repo", str(repository), "--roles", "call")
    row = diff["results"][0]
    assert row["status"] == "modified"
    assert "-        print('removed')" in row["patch"] and "+        print('added')" in row["patch"]
    assert "-    print('old')" in row["patch"] and "+    print('new')" in row["patch"]
    assert "value =" not in row["patch"] and "return value" not in row["patch"]
    for side, revision, original in (("before", "diff-before", before), ("after", "diff-after", after)):
        show = query("show", f"{revision}:roles.work", "--repo", str(repository), "--roles", "call")
        assert row[side]["fragments"] == show["results"]
        for hunk in row["hunks"]:
            positions = [r["old_line" if side == "before" else "new_line"] for r in hunk["lines"]]
            positions = [n for n in positions if n is not None]
            assert positions == list(range(hunk[side]["line"], hunk[side]["line"] + hunk[side]["count"]))
            for line in hunk["lines"]:
                number = line["old_line" if side == "before" else "new_line"]
                if number is not None:
                    assert original.splitlines()[number - 1] == line["text"]


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        ("def work():\n    print('gone')\n", "def work():\n    pass\n", "-    print('gone')"),
        ("def work():\n    pass\n", "def work():\n    print('new')\n", "+    print('new')"),
        ("def work():\n    pass\n", "def work():\n    return 2\n", None),
        ("def other(): pass\n", "def work():\n    print('new')\n", "+    print('new')"),
        ("def work():\n    print('gone')\n", "def other(): pass\n", "-    print('gone')"),
    ],
)
def test_role_diff_empty_and_missing_sides(repository, before, after, expected):
    commit_source(repository, before, "from")
    commit_source(repository, after, "to")
    args = ("diff", "from..to:roles.work", "--repo", str(repository), "--roles", "call")
    result = query(*args)
    if expected:
        assert expected in result["results"][0]["patch"]
    else:
        assert result["results"][0]["status"] == "empty"
        assert result["results"][0]["patch"] == ""
        status, out, err = invoke(*args)
        assert status == 0 and "from:roles.py\n" in out and "to:roles.py\n" in out and not err


def test_protocol_member_implementation_keeps_partial_ancestry_explicit():
    source = """from typing import Protocol as Interface
class P(Interface):
    def go(self) -> int: ...
class C(P):
    def go(self) -> int: return 1
"""
    result = query("inheritance", "C.go", source=source)
    assert "__stdin__.P" in result["ancestors"]
    assert any(r["target"] == "__stdin__.P.go" for r in result["implementations"])
    refs = query("refs", "P.go", "--roles", "implement", source=source)
    assert [r["symbol"] for r in refs["occurrences"]] == ["__stdin__.C.go"]
    # Missing external Protocol source cannot be advertised as a complete MRO.
    assert result["resolution"] == "unresolved" and result["reason"]


def test_cross_file_alias_inheritance_refs(repository):
    write(repository, "contract.py", "class Contract:\n def run(self): return 1\n")
    write(
        repository,
        "consumer.py",
        "from contract import Contract as Parent\nclass Child(Parent):\n def run(self): return 2\n",
    )
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "cross file inheritance")
    result = query("refs", "HEAD:Contract", "--repo", str(repository), "--roles", "inherit", "import")
    assert any("inherit" in r["roles"] and r["source"] == "HEAD:consumer.py" for r in result["occurrences"])
    method = query("refs", "HEAD:Contract.run", "--repo", str(repository), "--roles", "override")
    assert [r["symbol"] for r in method["occurrences"]] == ["consumer.Child.run"]


def test_role_context_includes_decorators_match_and_branches():
    source = """def decorate(fn): return fn
@decorate
def work(value):
    match value:
        case 1:
            print('one')
        case _:
            try:
                print('try')
            except ValueError:
                print('except')
            else:
                print('else')
            finally:
                print('finally')
"""
    status, out, _ = invoke("show", "work", "--roles", "call", source=source)
    assert status == 0
    for part in [
        "@decorate",
        "def work",
        "match value",
        "case 1:",
        "case _:",
        "try:",
        "except ValueError:",
        "else:",
        "finally:",
    ]:
        assert part in out


def test_role_diff_retains_context_across_gaps_at_zero_context(repository):
    before = "def work(x):\n    note = 1\n    if x:\n        ignored = 1\n        print('old')\n"
    after = before.replace("'old'", "'new'")
    commit_source(repository, before, "context-old")
    commit_source(repository, after, "context-new")
    row = query(
        "diff",
        "context-old..context-new:roles.work",
        "--repo",
        str(repository),
        "--roles",
        "call",
        "-U",
        "0",
    )["results"][0]
    assert "def work(x):" in row["patch"] and "if x:" in row["patch"]
    assert "note =" not in row["patch"] and "ignored =" not in row["patch"]
    assert row["hunks"][0]["before"] == {"line": 1, "count": 1}


@pytest.mark.parametrize("native", [True, False])
def test_inheritance_depth_reports_omitted_descendants(native, monkeypatch):
    if not native:
        from lsp_cli.workspace import NeedsBindingsError, Workspace

        def unavailable(*args):
            raise NeedsBindingsError("disabled for fallback check")

        monkeypatch.setattr(Workspace, "request", unavailable)
    source = "class A: pass\nclass B(A): pass\nclass C(B): pass\n"
    result = query("inheritance", "A", "--max-depth", "1", source=source)
    assert result["descendants"] == ["__stdin__.B"]
    assert result["truncated"]
    assert all(r["symbol"] != "__stdin__.C" for r in result["results"])


def test_each_action_has_its_own_help_and_rejects_retired_options():
    import io
    from contextlib import redirect_stdout

    from lsp_cli.commands import ACTIONS, parser

    for action in ACTIONS:
        output = io.StringIO()
        with redirect_stdout(output), pytest.raises(SystemExit) as exc:
            parser().parse_args([action, "--help"])
        assert exc.value.code == 0
        help_text = output.getvalue()
        assert f"usage: lsp {action}" in help_text
        assert help_text.count("usage:") == 1
        assert "{" + ",".join(ACTIONS) + "}" not in help_text
        assert all(flag not in help_text for flag in ("--filters", "--match", "--all"))
        assert ("--roles" in help_text) == (action in {"show", "refs", "diff"})
        assert ("--kinds" in help_text) == (action == "list")
    assert query("show", "empty", "--filters", "call", status=2)["error"]


def test_module_entrypoint_exposes_the_same_public_actions():
    import io
    import runpy
    from contextlib import redirect_stdout
    from unittest.mock import patch

    output = io.StringIO()
    with patch("sys.argv", ["lsp", "--help"]), redirect_stdout(output), pytest.raises(SystemExit) as exc:
        runpy.run_module("lsp_cli", run_name="__main__")
    assert exc.value.code == 0
    assert "{list,show,refs,inheritance,path,history,diff,where,complete}" in output.getvalue()
