"""Public actions tested against source evidence and a real pinned ty server."""

import ast
import io
import json
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

from lsp_cli import backend
from lsp_cli.commands import ACTIONS, main
from lsp_cli.session import Session
from lsp_cli.platform_support import executable

from .search_helpers import internal_search
from .test_git_sources import git, write
from .test_git_sources import repository as repository  # noqa: PLC0414

CODE = """def leaf(n):
    return n
class Base:
    def run(self, x):
        return leaf(x)
class Child(Base):
    def run(self, x):
        self.value = x
        return super().run(x)
def start(x):
    obj = Child()
    value = obj.run(x)
    return value
"""
SPAN = {"source", "line", "column", "end_line", "end_column"}


def invoke(*args, source=CODE):
    stdout, stderr = io.StringIO(), io.StringIO()
    with patch("sys.stdin", io.StringIO(source)), redirect_stdout(stdout), redirect_stderr(stderr):
        status = main(list(args))
    return status, stdout.getvalue(), stderr.getvalue()


def query(*args, source=CODE, status=0):
    code, out, err = invoke(*args, "--json", source=source)
    assert code == status, out + err
    result = json.loads(out)
    assert result["schema_version"] == 3
    return result


def test_public_command_and_actions():
    run = subprocess.run(
        [str(executable("lsp")), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "{" + ",".join(ACTIONS) + "}" in run.stdout
    assert "search" not in run.stdout
    assert "--match" not in run.stdout
    for old in (
        "defs",
        "search",
        "hierarchy",
        "blame",
        "outline",
        "trace",
        "callee",
        "callers",
        "inherits",
        "query",
        "resolve",
        "info",
    ):
        assert query(old, status=2)["error"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("show", "Worker", "-e"), "argument -e/--regexp: expected one argument"),
        (("show", "Worker", "--kinds", "method"), "unrecognized arguments: --kinds"),
    ],
)
def test_argument_error_json_cannot_look_like_an_empty_search(arguments, message):
    status, out, err = invoke(*arguments, "--json")
    result = json.loads(out)
    assert status == 2 and not err
    assert result["status"] == "error"
    assert result["error"]["message"] == message
    assert result["results"] == result["queries"] == result["heads"] == []
    # Trial 213 read five lines and mistook the previous empty arrays for a miss.
    preview = "\n".join(out.splitlines()[:5])
    assert '"status": "error"' in preview and message in preview


@pytest.mark.parametrize(
    "args",
    [
        ["list"],
        ["show", "Child"],
        ["show", "Child.run", "--roles", "call", "write", "return"],
        ["refs", "leaf"],
        ["where", "<stdin>:8"],
        ["inheritance", "Child"],
        ["path", "start", "leaf"],
        ["complete", "<stdin>:13:4"],
    ],
)
def test_actions_have_source_identity_and_exact_spans(args):
    result = query(*args)
    assert result["results"]
    assert all(row.keys() >= SPAN for row in result["results"])
    assert result["sources"][0]["source"] == "<stdin>"
    assert len(result["sources"][0]["sha256"]) == 64


def test_fast_reads_do_not_start_semantic_engines(monkeypatch):
    def fail(*_args, **_kwargs):
        pytest.fail("Direct source reads must not start a semantic engine")

    monkeypatch.setattr(Session, "native", fail)
    monkeypatch.setattr(backend, "PyanFacts", fail)
    for args in (["show", "Child"], ["list"], ["where", "<stdin>:8"]):
        assert query(*args)["results"]


def test_lexical_bindings_shadowing_and_reassignment():
    source = """value = 0
def first(value, *, extra=1):
    conn = value
    conn = extra
    def nested():
        nonlocal conn
        conn = 4
        return conn
    return conn
def second():
    conn = 8
    return conn
"""
    result = internal_search("conn", mode="exact", all_results=True, source=source)
    assert [r["symbol"] for r in result["results"]] == ["__stdin__.first.conn", "__stdin__.second.conn"]
    assert len(result["results"][0]["declarations"]) == 3
    uses = query("refs", "first.conn", source=source)["occurrences"]
    assert {r["line"] for r in uses} == {3, 4, 7, 8, 9}
    assert all(r["resolution"] == "resolved" for r in uses)
    params = internal_search("value", kinds=["parameter"], source=source)["results"]
    assert [r["symbol"] for r in params] == ["__stdin__.first.value"]


def test_comprehension_lambda_global_match_and_field_symbols():
    source = """count = 0
class Store:
    def run(self, items):
        global count
        self.value = items
        count += 1
        values = [(last := item) for item in items]
        fn = lambda argument: argument + count
        match items:
            case {"key": captured, **rest}: return captured
        return last
"""
    rows = query("list", "--recursive", source=source)["results"]
    names = [r["symbol"] for r in rows]
    assert names.count("__stdin__.count") == 1
    assert "__stdin__.Store.value" in names
    assert "__stdin__.Store.run.last" in names
    assert "__stdin__.Store.run.captured" in names and "__stdin__.Store.run.rest" in names
    assert any("<listcomp>" in n and n.endswith(".item") for n in names)
    assert any("<lambda>" in n and n.endswith(".argument") for n in names)


@pytest.mark.parametrize(
    ("target", "needle"),
    [
        ("start.x", "def start(x):"),
        ("start.value", "value = obj.run(x)"),
        ("Child.value", "self.value = x"),
    ],
)
def test_show_nonfunction_declarations(target, needle):
    result = query("show", target)
    assert any(needle in row["code"] for row in result["results"])


def test_filter_union_and_flat_original_control_context():
    source = """def leaf(): pass
def work(flag, obj, data):
    if flag:
        result = leaf()
        obj.timeout = 2
    else:
        data[0] = leaf()
    try:
        with open("x") as stream:
            for item in stream:
                result = item
    except OSError:
        raise
    finally:
        obj.closed = True
    return result
"""
    result = query("show", "work", "--roles", "call", "write", "return", "raise", source=source)
    events = result["occurrences"]
    assert {e["kind"] for e in events} >= {"CALL", "ASSIGN", "ATTR_WRITE", "SUBSCRIPT_WRITE", "RETURN", "RAISE"}
    assert [(e["line"], e["column"]) for e in events] == sorted((e["line"], e["column"]) for e in events)
    assert next(e for e in events if e["kind"] == "SUBSCRIPT_WRITE")["context"][0]["kind"] == "else"
    code, out, _err = invoke("show", "work", "--roles", "attr_write", source=source)
    assert code == 0 and sum(line.lstrip().startswith("5 |") for line in out.splitlines()) == 1
    assert "if flag:" in out and "finally:" in out
    assert "[write]" not in out and "ATTR_WRITE" not in out
    assert out.startswith("<stdin>\n") and "def work(flag, obj, data):" in out
    assert all(
        e["kind"] == "ATTR_WRITE" for e in query("show", "work", "--roles", "attr_write", source=source)["occurrences"]
    )


def test_doc_filter_preserves_nested_docs_and_signature_context_without_engines(monkeypatch):
    def fail(*_args, **_kwargs):
        pytest.fail("Docstrings must be read from source without a semantic engine")

    monkeypatch.setattr(Session, "native", fail)
    monkeypatch.setattr(backend, "PyanFacts", fail)
    source = '''"""Module docs."""
class Worker:
    """Class docs."""
    async def run(
        self, value,
    ):
        r"""Keep 原文.

        Preserve indentation and \\n escapes.
        """
        def nested():
            """Nested docs."""
            return value
        return nested()
'''
    module = query("show", "__stdin__", "--roles", "doc", source=source)
    assert [row["line"] for row in module["occurrences"]] == [1, 3, 7, 12]
    result = query("show", "Worker.run", "--roles", "doc", source=source)
    assert {row["kind"] for row in result["occurrences"]} == {"DOC"}
    assert [row["line"] for row in result["occurrences"]] == [7, 12]
    lines = source.splitlines()
    for row in result["results"]:
        assert row["code"] == "\n".join(lines[row["line"] - 1 : row["end_line"]])
    code = "\n".join(row["code"] for row in result["results"])
    assert "class Worker:" in code and "async def run(" in code and "def nested():" in code
    assert "Module docs" not in code and "Class docs" not in code and "return" not in code


def test_doc_filter_union_preserves_other_filters_and_deduplicates():
    source = 'def leaf(): pass\ndef work():\n    """Work docs."""\n    leaf()\n'
    calls = query("show", "work", "--roles", "call", source=source)
    assert "Work docs" not in "\n".join(row["code"] for row in calls["results"])
    combined = query("show", "work", "--roles", "doc", "call", "doc", source=source)
    assert [(row["kind"], row["line"]) for row in combined["occurrences"]] == [("DOC", 3), ("CALL", 4)]
    assert "\n".join(row["code"] for row in combined["results"]) == "\n".join(source.splitlines()[1:])


@pytest.mark.parametrize("body", ["pass", "# Comment\n    pass", 'value = "string"\n    "Not a docstring."'])
def test_doc_filter_zero_matches_keeps_the_declaration(body):
    source = f"def work():\n    {body}\n"
    result = query("show", "work", "--roles", "doc", source=source)
    assert not result["occurrences"]
    assert result["results"][0]["code"] == "def work():"
    status, out, err = invoke("show", "work", "--roles", "doc", source=source)
    assert status == 0 and "def work():" in out and not err
    assert out == "<stdin>\n1 | def work():\n"


def test_filtered_empty_target_keeps_its_head_in_a_mixed_batch():
    source = 'def empty(): pass\ndef documented():\n    """Documented function."""\n'
    status, out, err = invoke("show", "empty", "documented", "absent", "--roles", "doc", source=source)
    assert status == 0 and not err
    assert "Documented function." in out
    assert "def empty():" in out
    assert out.count("<stdin>") == 1 and "__stdin__." not in out
    assert "absent" not in out and "pass" not in out


def test_empty_docstring_matches_and_doc_filter_rejects_refs():
    source = 'def work():\n    ""\n'
    assert query("show", "work", "--roles", "doc", source=source)["occurrences"][0]["text"] == '""'
    assert "invalid choice" in query("refs", "work", "--roles", "doc", source=source, status=2)["error"]["message"]


@pytest.mark.parametrize("native", [True, False])
def test_refs_filters_are_relative_to_selected_binding(monkeypatch, native):
    if not native:
        from lsp_cli.workspace import NeedsBindingsError, Workspace

        def unavailable(*_args):
            raise NeedsBindingsError("test fallback")

        monkeypatch.setattr(Workspace, "request", unavailable)
    source = """def charge(amount): return amount
def pay(amount):
    handler = charge
    receipt = charge(amount)
    receipt += 1
    return receipt
"""
    reads = query("refs", "charge", "--roles", "read", source=source)["occurrences"]
    assert {r["line"] for r in reads} == {3, 4}
    calls = query("refs", "charge", "--roles", "call", source=source)["occurrences"]
    assert {r["line"] for r in calls} == {4}
    args = query("refs", "pay.amount", "--roles", "call", source=source)["occurrences"]
    assert not args
    writes = query("refs", "pay.receipt", "--roles", "write", source=source)["occurrences"]
    assert {r["line"] for r in writes} == {4, 5}
    reads = query("refs", "pay.receipt", "--roles", "read", source=source)["occurrences"]
    assert {r["line"] for r in reads} == {5, 6}


def test_refs_skip_flow_for_unrelated_unknown_calls(monkeypatch):
    def unexpected_flow(*_args, **_kwargs):
        pytest.fail("proven references should not analyze unrelated dynamic calls")

    monkeypatch.setattr(Session, "analysis", unexpected_flow)
    result = query(
        "refs",
        "render",
        "--roles",
        "call",
        source="""def render(value): return value
def inspect(plugin):
    plugin.unknown()
    first = render('first')
    return render(first)
""",
    )
    rows = result["occurrences"]
    assert {r["line"] for r in rows} == {4, 5}
    assert all(r["resolution"] == "resolved" and r["engine"] == "ty-lsp" for r in rows)


def test_refs_still_validate_selected_callable_aliases(monkeypatch):
    original = Session.analysis
    analyses = []

    def observe(self, *args, **kwargs):
        analyses.append(True)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Session, "analysis", observe)
    result = query(
        "refs",
        "render",
        "--roles",
        "call",
        source="""def render(value): return value
def inspect(value):
    handler = render
    return handler(value)
""",
    )
    rows = result["occurrences"]
    assert analyses
    assert {r["line"] for r in rows} == {4}
    assert all(r["resolution"] == "resolved" and r["engine"] == "python-flow" for r in rows)


def test_calls_use_ty_and_unresolved_remains_unresolved():
    source = 'def café(): return 1\ndef start(unknown):\n    "😀"; café(); unknown()\n'
    result = query("show", "start", "--roles", "call", source=source)
    call, unknown = result["occurrences"]
    assert call["target"] == "__stdin__.café" and call["engine"] == "ty-lsp"
    assert call["column"] == source.splitlines()[2].index("café")
    assert unknown["target"] is None and unknown["resolution"] == "unresolved"


def test_native_failure_has_flow_fallback(monkeypatch):
    monkeypatch.setattr(Session, "native", lambda *_a: {"error": {"message": "method unavailable"}})
    result = query("show", "Child.run", "--roles", "call")
    assert any(r["target"] == "__stdin__.Base.run" for r in result["occurrences"])
    assert any(d["kind"] == "lsp_fallback" for d in result["diagnostics"])
    result = query("refs", "leaf")
    assert result["occurrences"]


def test_where_is_lexical_chain_and_separate_binding():
    result = query("where", "<stdin>:5:16")
    assert [r["symbol"] for r in result["results"]] == ["__stdin__", "__stdin__.Base", "__stdin__.Base.run"]
    assert [r["symbol"] for r in result["targets"]] == ["__stdin__.leaf"]
    assert result["context"][0]["code"] == "        return leaf(x)"


def test_diamond_mro_inherited_show_and_overrides():
    source = """class A:
    def f(self): return 1
class B(A): pass
class C(A):
    def f(self): return 2
class D(B, C): pass
"""
    result = query("inheritance", "D", source=source)
    assert result["mro"] == ["__stdin__.D", "__stdin__.B", "__stdin__.C", "__stdin__.A", "builtins.object"]
    assert query("show", "D.f", source=source)["symbol"]["symbol"] == "__stdin__.C.f"
    overrides = query("inheritance", "A.f", source=source)["results"]
    assert any(r["symbol"] == "__stdin__.C.f" and r["relation"] == "overrides" for r in overrides)
    assert all(r["symbol"] != "__stdin__" for r in overrides)


@pytest.mark.parametrize(
    "source",
    [
        "class A(External): pass\n",
        "class A(metaclass=Unknown): pass\n",
        "class X: pass\nclass Y: pass\nclass B(X, Y): pass\nclass C(Y, X): pass\nclass A(B,C): pass\n",
    ],
)
def test_unresolved_mro_is_explicit(source):
    result = query("inheritance", "A", source=source)
    assert result["resolution"] == "unresolved" and not result["mro"] and result["reason"]


def test_multihop_path_and_source_evidence():
    result = query("path", "start", "leaf")
    assert result["status"] == "found"
    assert [r["target"] for r in result["results"]] == ["__stdin__.Child.run", "__stdin__.Base.run", "__stdin__.leaf"]
    assert result["evidence"]


@pytest.mark.parametrize("body", ["return\n    leaf()", "if False:\n        leaf()", "gen = (leaf() for x in [])"])
def test_path_excludes_provably_unexecuted_calls(body):
    result = query("path", "start", "leaf", source="def leaf(): pass\ndef start():\n    " + body + "\n")
    assert result["status"] != "found"


def test_path_argument_continuation_uses_flow():
    source = "def leaf(): pass\ndef invoke(fn): fn()\ndef start(): invoke(leaf)\n"
    result = query("path", "start", "leaf", source=source)
    assert result["status"] == "found" and len(result["results"]) == 2
    assert result["backend"]["fallback"] == "python-flow"


def test_completion_supports_incomplete_python():
    source = "class Worker:\n    def execute(self): pass\nw = Worker()\nw.\n"
    result = query("complete", "<stdin>:4:2", source=source)
    assert "execute" in {r["label"] for r in result["results"]}
    assert "textDocument/completion" in result["backend"]["methods"]


def test_search_scope_kinds_limit_and_grep_ready_stdout():
    source = "def first(conn):\n    value=conn\n    return value\ndef second(conn):\n    return conn\n"
    result = internal_search("conn", mode="exact", limit=1, source=source)
    assert result["matched"] == 2 and result["truncated"]
    result = internal_search("conn", mode="exact", all_results=True, source=source)
    assert len(result["results"]) == 2 and not result["truncated"]
    assert all(row["address"] and row["selection"] for row in result["results"])


def test_raw_and_json_keep_source_separate_from_metadata():
    source = "class Outer:\n    def f(self):\n        return 'a\u2028b'\n\n\n"
    status, code, err = invoke("show", "Outer.f", "--raw", source=source)
    assert status == 0 and not err
    assert isinstance(ast.parse(code).body[0], ast.FunctionDef)
    result = query("show", "__stdin__", source=source)
    assert "documents" not in result
    assert result["results"][0]["code"] + "\n" == source
    _status, out, _err = invoke("show", "Outer.f", source=source)
    assert "3 |" in out and "4 |" not in out
    assert invoke("show", "start.x", "--raw")[0] == 2


def test_git_revision_aliases_cross_files_and_batches(repository):
    before = git(repository, "status", "--porcelain")
    result = query(
        "show",
        "v1:Worker.ping",
        "v2:Worker.ping",
        "v1:missing",
        "--repo",
        str(repository),
    )
    first, second, missing = result["queries"]
    assert first["matches"][0]["symbol"] == "pkg.base.Base.ping"
    assert "return 1" in result["results"][first["result_indices"][0]]["code"]
    assert "return 2" in result["results"][second["result_indices"][0]]["code"]
    assert missing["status"] == "no_match" and "errors" not in missing
    assert git(repository, "status", "--porcelain") == before
    rows = query("refs", "v1:Base.ping", "--repo", str(repository), "--roles", "call")["occurrences"]
    assert any(r["source"] == "v1:pkg/worker.py" for r in rows)


@pytest.mark.parametrize("action", ["diff", "history"])
def test_git_history_actions_preserved(repository, action):
    target = ("v1..v2" if action == "diff" else "v2") + ":Base.ping"
    assert query(action, target, "--repo", str(repository))["results"]


def test_git_actions_use_the_same_local_symbol_catalog(repository):
    write(repository, "local.py", "def work(value: int):\n result=value\n result+=1\n return result\n")
    git(repository, "add", "local.py")
    git(repository, "commit", "-qm", "locals")
    git(repository, "tag", "locals1")
    write(repository, "local.py", "def work(value: float):\n result=value\n result+=2\n return result\n")
    git(repository, "add", "local.py")
    git(repository, "commit", "-qm", "change locals")
    for symbol in ("work.value", "work.result"):
        diff = query("diff", "locals1..HEAD:local." + symbol, "--repo", str(repository))
        assert diff["results"][0]["status"] == "modified"
        assert query("show", "--blame", "HEAD:local." + symbol, "--repo", str(repository))["results"]
        assert query("history", "HEAD:local." + symbol, "--repo", str(repository))["results"]


def test_search_full_revision_and_directory_scope(repository):
    result = internal_search(
        "self",
        revision="v2",
        scope="src/pkg",
        kinds=["parameter"],
        all_results=True,
        repo=repository,
    )
    assert {r["symbol"] for r in result["results"]} == {"pkg.base.Base.ping.self", "pkg.worker.Worker.run.self"}
    result = internal_search("self", revision="v2", scope="pkg.worker.Worker.run", all_results=True, repo=repository)
    assert [r["symbol"] for r in result["results"]] == ["pkg.worker.Worker.run.self"]


def test_cross_revision_path_rejected(repository):
    result = query("path", "v1:Worker.run", "v2:Base.ping", "--repo", str(repository), status=2)
    assert "one commit" in result["error"]["message"]


def test_source_budget_cannot_report_false_absence(repository):
    result = query(
        "show",
        "v1:pkg.worker.Worker.run",
        "--roles",
        "call",
        "--max-files",
        "1",
        "--repo",
        str(repository),
    )
    assert not result["coverage"]["complete"]
    assert any(e["resolution"] == "unresolved" for e in result["occurrences"])


def test_stdin_cross_file_alias_resolves_to_source(tmp_path):
    dep = tmp_path / "dep.py"
    dep.write_text("def leaf(): return 1\n")
    result = query(
        "show",
        "pkg.entry.alias",
        "--files",
        "pkg.dep=" + str(dep),
        "--files",
        "-",
        "--module",
        "pkg.entry",
        source="from .dep import leaf as alias\n",
    )
    assert result["symbol"]["symbol"] == "pkg.dep.leaf"


def test_native_input_resources_are_closed(monkeypatch):
    resources = []
    original = Session.close

    def close(session):
        resources.extend(Path(resource.name) for resource in session.resources)
        original(session)

    monkeypatch.setattr(Session, "close", close)
    assert query("refs", "leaf")["results"]
    assert resources and all(not path.exists() for path in resources)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("def a(): pass\ndef b(): pass\ndef start(flag):\n if flag: fn=a\n else: fn=b\n fn()\n", [None]),
        ("def a(): pass\ndef start(flag):\n if flag: fn=a\n else: fn=a\n fn()\n", ["__stdin__.a"]),
        ("def a(): pass\ndef change(other):\n global a\n a=other\ndef start(): a()\n", [None]),
        (
            "class C:\n def __getattribute__(self,key): return unknown\n def run(self): pass\ndef start(): C().run()\n",
            [None, "__stdin__.C"],
        ),
        (
            (
                "class C:\n def run(self): pass\ndef start(mutator):\n item=C()\n item.run()\n"
                " mutator(item)\n item.run()\n"
            ),
            ["__stdin__.C", "__stdin__.C.run", None, None],
        ),
        ("def a(): pass\nfrom missing import *\ndef start(): a()\n", [None]),
        (
            "class A:\n def f(self): pass\nclass C(A):\n def start(self, other):\n  self=other\n  super().f()\n",
            ["builtins.super", None],
        ),
    ],
)
def test_lsp_locations_are_not_mistaken_for_unique_call_bindings(source, expected):
    result = query("show", "start", "--roles", "call", source=source)
    # Source order places an inner constructor before/after its enclosing call
    # according to end position; compare sites separately when columns coincide.
    actual = [r["target"] for r in result["occurrences"]]
    assert sorted(actual, key=str) == sorted(expected, key=str)


def test_inherited_receiver_changes_super_mro():
    source = """class A:
    def ping(self): pass
class B(A):
    def work(self): self.ping(); super().ping()
class C(A):
    def ping(self): pass
class D(B,C): pass
"""
    result = query("show", "D.work", "--roles", "call", source=source)
    assert [r["target"] for r in result["occurrences"] if r["callee"] != "super"] == ["__stdin__.C.ping"] * 2
    result = query("path", "D.work", "C.ping", source=source)
    assert result["status"] == "found"


def test_reference_call_filter_keeps_ambiguous_alias_unresolved():
    source = "def a(): pass\ndef b(): pass\ndef start(flag):\n fn = a if flag else b\n fn()\n"
    result = query("refs", "a", "--roles", "call", source=source)
    assert result["occurrences"]
    assert all(r["resolution"] == "unresolved" for r in result["occurrences"])


@pytest.mark.parametrize("target", ["Base", "Child"])
def test_custom_metaclass_does_not_become_a_proven_call_or_mro(target):
    source = (
        "class Base(metaclass=unknown):\n def run(self): pass\nclass Child(Base): pass\n"
        f"def start(): {target}().run()\n"
    )
    calls = query("show", "start", "--roles", "call", source=source)["occurrences"]
    assert next(r for r in calls if r["callee"].endswith(".run"))["resolution"] == "unresolved"
    assert query("inheritance", target, source=source)["resolution"] == "unresolved"
    assert query("path", "start", "Base.run", source=source)["status"] != "found"


def test_expression_refs_do_not_fabricate_receiver_definitions():
    result = query(
        "refs",
        "conn.cert_reqs",
        source="def configure(conn):\n conn.cert_reqs=0\n return conn.cert_reqs\n",
    )
    assert {r["line"] for r in result["occurrences"]} == {2, 3}
    assert all(r["resolution"] == "unresolved" and r["target"] is None for r in result["occurrences"])


def test_semantic_scope_is_filtered_before_embedding(monkeypatch):
    from lsp_cli import search

    received = []

    def embed(texts, _query):
        received.extend(texts)
        return [0.5] * len(texts)

    monkeypatch.setattr(search, "semantic", embed)
    result = internal_search("连接", source=CODE, mode="semantic", kinds=["parameter"])
    assert result["results"] and received
    assert all(text.startswith("parameter ") for text in received)
    assert result["backend"]["model"] == search.MODEL


def test_missing_semantic_weights_are_an_explicit_error(monkeypatch, tmp_path):
    monkeypatch.setenv("LSP_MODEL_CACHE", str(tmp_path))
    with pytest.raises(ValueError, match="not prepared"):
        internal_search("网络", source=CODE, mode="semantic")
