"""Exercise real ty, immutable revisions, fast reads, and semantic fallbacks."""

import json
from pathlib import Path

import pytest

from lsp_cli import backend
from lsp_cli.query_cli import subcommand_main
from lsp_cli.git_sources import GitSources
from lsp_cli.session import Session

from .test_git_sources import git, run, write
from .test_git_sources import repository as repository  # noqa: PLC0414


@pytest.mark.parametrize(("action", "target"), [
    ("show", "pkg.worker.Worker"), ("defs", "pkg.worker.Worker"),
    ("outline", "pkg.worker"), ("where", "pkg.worker"),
    ("diff", "pkg.base.Base.ping"), ("history", "pkg.base.Base.ping"), ("blame", "pkg.base.Base.ping"),
])
def test_source_actions_skip_binding_analysis(repository, monkeypatch, capsys, action, target):
    def fail(*_args, **_kwargs):
        pytest.fail("Reading a direct definition must not start pyan or build semantic summaries")
    monkeypatch.setattr(backend, "PyanFacts", fail)
    revision = "v1..v2" if action == "diff" else "v1"
    args = [action, revision + ":" + target, "--repo", str(repository), "--json"]
    if action == "where":
        args += ["--line", "4"]
    assert subcommand_main(args) == 0
    assert json.loads(capsys.readouterr().out)["results"]


@pytest.mark.parametrize("action", ["callee", "callers", "refs"])
def test_real_native_relationships_are_used(repository, action):
    target = "Worker.run" if action == "callee" else "Base.ping"
    body = run(repository, action, target)
    assert body["backend"]["name"] == "ty-lsp"
    assert body["backend"]["version"] == "0.0.84"
    assert body["results"]
    assert all(row["source"].startswith("v1:") for row in body["results"])
    assert all(row["engine"] == "ty-lsp" for row in body["results"])


def test_native_error_falls_back_with_feedback(repository, monkeypatch, capsys):
    monkeypatch.setattr(Session, "native", lambda *_args: {"error": {"message": "unsupported method", "code": -32601}})
    assert subcommand_main(["callee", "v1:Worker.run", "--repo", str(repository), "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert any(row.get("target") == "pkg.base.Base.ping" for row in body["results"])
    assert any(row["kind"] == "lsp_fallback" for row in body["diagnostics"])


def test_native_session_reuses_commit_and_closes_process_and_snapshot(repository):
    loader = GitSources(repository, "v1")
    with Session() as invocation:
        first = invocation.native(loader, "initialize", {})
        second = invocation.native(loader, "initialize", {})
        assert first["timing"]["cold"] and not second["timing"]["cold"]
        assert first["commit"] == second["commit"]
        native, = invocation.lsp_engine.sessions.values()
        root, process = native.root, native.client.process
        assert root.exists()
    assert process.returncode == 0
    assert not root.exists()


def test_unicode_columns_and_no_invented_dynamic_targets(repository):
    code = ('def café(): return 1\n'
            'def start(unknown):\n'
            ' if True:\n'
            '  value = "😀"; café(); unknown()\n'
            ' return value\n')
    write(repository, "unicode.py", code)
    git(repository, "add", "unicode.py")
    git(repository, "commit", "-qm", "unicode calls")
    body = run(repository, "callee", "unicode.start", rev="HEAD")
    resolved, = [r for r in body["results"] if r["callee"] == "café"]
    assert resolved["target"] == "unicode.café"
    assert resolved["column"] == code.splitlines()[3].index("café")
    assert resolved["context"][0]["kind"] == "if"
    dynamic, = [r for r in body["results"] if r["callee"] == "unknown"]
    assert dynamic["target"] is None and dynamic["resolution"] == "unresolved"
    references = run(repository, "refs", "unicode.café", rev="HEAD")
    assert any(r["column"] == resolved["column"] and r["line"] == 4 for r in references["results"])
    assert Path(repository, "src/pkg/base.py").read_text() == "invalid dirty worktree!\n"


def test_builtin_resolution_comes_from_lsp_not_a_same_named_guess(repository):
    write(repository, "shadow.py", "def len(value): return 1\ndef caller(): return len([]) + abs(-1)\n")
    git(repository, "add", "shadow.py")
    git(repository, "commit", "-qm", "shadow builtin")
    rows = run(repository, "callee", "shadow.caller", rev="HEAD")["results"]
    assert {row["callee"]: row["target"] for row in rows} == {"len": "shadow.len", "abs": "builtins.abs"}
