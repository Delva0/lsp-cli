"""Exercise symbol-scoped diffs, line history and blame against actual Git data."""

import json

import pytest

from .test_cli import SPAN, invoke
from .test_git_sources import git, ripgit, write
from .test_git_sources import repository as repository  # noqa: PLC0414 -- Re-export the shared pytest fixture.


def command(repo, *args):
    result = ripgit(*args, "--json", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    body = json.loads(result.stdout)
    assert all(row.keys() >= SPAN for row in body["results"])
    return body


def commit(repo, subject):
    git(repo, "add", ".")
    git(repo, "commit", "-qm", subject)
    return git(repo, "rev-parse", "HEAD")


def test_defs_lexical_scope_and_stdin():
    code = "class Worker:\n def run(self):\n  def inner(): pass\n  return inner()\nclass Sibling: pass\n"
    result = invoke("defs", "Worker", "--json", source=code)
    assert result.returncode == 0, result.stdout
    rows = json.loads(result.stdout)["results"]
    assert [r["symbol"] for r in rows] == ["__stdin__.Worker", "__stdin__.Worker.run", "__stdin__.Worker.run.inner"]
    assert [r["line"] for r in rows] == [1, 2, 3]


def test_defs_git_module_class_and_inherited_method(repository):
    rows = command(repository, "defs", "v1:pkg.worker")["results"]
    assert [r["symbol"] for r in rows] == ["pkg.worker", "pkg.worker.Worker", "pkg.worker.Worker.run"]
    rows = command(repository, "defs", "v1:Worker")["results"]
    assert [r["symbol"] for r in rows] == ["pkg.worker.Worker", "pkg.worker.Worker.run"]
    rows = command(repository, "defs", "v1:Worker.ping")["results"]
    assert [r["symbol"] for r in rows] == ["pkg.base.Base.ping"]


def test_diff_tracks_definition_across_layout_changes(repository):
    before = git(repository, "status", "--porcelain")
    body = command(repository, "diff", "v1..v2:Worker.ping")
    row = body["results"][0]
    assert row["status"] == "modified"
    assert row["before"]["source"] == "v1:pkg/base.py"
    assert row["after"]["source"] == "v2:src/pkg/base.py"
    assert row["patch"] == ("--- v1:pkg/base.py\n+++ v2:src/pkg/base.py\n@@ -2 +2 @@\n"
                            "- def ping(self): return 1\n+ def ping(self): return 2")
    for side, rev in (("before", "v1"), ("after", "v2")):
        assert body["versions"][side]["commit"] == git(repository, "rev-parse", rev)
        assert row[side]["definition"]["symbol"] == "pkg.base.Base.ping"
    assert git(repository, "status", "--porcelain") == before


def test_diff_unchanged_and_physical_context_lines(repository):
    assert command(repository, "diff", "v1..v2:Worker.run")["results"][0]["status"] == "unchanged"
    write(repository, "src/pkg/base.py",
          "# heading\n\nclass Base:\n def ping(self):\n  a = 1\n  b = 2\n  return a + b\n")
    commit(repository, "long body")
    write(repository, "src/pkg/base.py",
          "# heading\n\nclass Base:\n def ping(self):\n  a = 1\n  b = 3\n  return a + b\n")
    commit(repository, "one line")
    row = command(repository, "diff", "HEAD^..HEAD:Base.ping", "-U0")["results"][0]
    assert row["patch"].endswith("@@ -6 +6 @@\n-  b = 2\n+  b = 3")


@pytest.mark.parametrize(("symbol", "status"), [("old", "deleted"), ("new", "added"), ("extra", "added")])
def test_diff_added_deleted_definitions_and_modules(repository, symbol, status):
    write(repository, "src/pkg/base.py", "class Base: pass\n")
    write(repository, "change.py", "def old(): pass\n")
    commit(repository, "before definitions")
    write(repository, "change.py", "def new(): pass\n")
    write(repository, "extra.py", "# new module\n")
    commit(repository, "after definitions")
    row = command(repository, "diff", "HEAD^..HEAD:" + symbol)["results"][0]
    assert row["status"] == status
    assert row["before" if status == "added" else "after"] is None
    assert "/dev/null" in row["patch"]


@pytest.mark.parametrize("target", ["Base.ping", "missing"])
def test_diff_does_not_treat_incomplete_discovery_as_deletion(repository, target):
    write(repository, "src/pkg/base.py", "from external import Unknown\nclass Base(Unknown): pass\n")
    commit(repository, "external base")
    extra = ["--max-files", "1"] if target == "missing" else []
    if target == "missing":
        write(repository, "one.py", "def missing(): pass\n")
        write(repository, "two.py", "def missing(): pass\n")
        commit(repository, "ambiguous candidates beyond limit")
    result = ripgit("diff", "v1..HEAD:" + target, *extra, "--json", cwd=repository)
    assert result.returncode == 2
    assert any(word in json.loads(result.stdout)["error"]["message"] for word in ("unresolved", "incomplete"))


@pytest.mark.parametrize("ambiguity", ["function", "class"])
def test_diff_ambiguity_is_an_error(repository, ambiguity):
    write(repository, "src/pkg/base.py", "class Base:\n def ping(self): pass\n")
    write(repository, "other.py", "class Base:\n def unrelated(self): pass\n")
    if ambiguity == "function":
        write(repository, "one.py", "def task(): pass\n")
        write(repository, "two.py", "def task(): pass\n")
    commit(repository, "ambiguous definitions")
    symbol = "task" if ambiguity == "function" else "Base.inherited"
    result = ripgit("diff", "v1..HEAD:" + symbol, "--json", cwd=repository)
    assert result.returncode == 2
    assert "ambiguous" in json.loads(result.stdout)["error"]["message"]


@pytest.mark.parametrize("address", ["Worker.run", "v1:Worker.run", "v1...v2:Worker.run", "v1..:Worker.run",
                                     "..v2:Worker.run", "v1..v2:Missing", "v1..bad:Worker.run"])
def test_diff_invalid_range_or_missing_symbol(repository, address):
    result = ripgit("diff", address, "--json", cwd=repository)
    assert result.returncode == 2 and json.loads(result.stdout)["error"]["message"]


def test_history_follows_lines_across_rename_and_ignores_other_edits(repository):
    write(repository, "src/pkg/base.py", git(repository, "show", "HEAD:src/pkg/base.py") + "\n")
    worker = git(repository, "show", "HEAD:src/pkg/worker.py").replace("timeout = 10", "timeout = 20")
    write(repository, "src/pkg/worker.py", worker + "\n")
    commit(repository, "worker change")
    write(repository, "src/pkg/worker.py", worker + "\n\ndef unrelated(): pass\n")
    commit(repository, "unrelated function")
    rows = command(repository, "history", "Worker.run")["results"]
    row = rows[0]
    assert row["source"] == "HEAD:src/pkg/worker.py" and row["line"] == 3 and row["end_line"] == 5
    assert row["scope"] == "git_line_history" and row["location_role"] == "starting_definition"
    assert [c["subject"] for c in row["commits"]] == ["worker change", "flat layout"]
    assert not row["truncated"]
    row = command(repository, "history", "v1:Worker.ping")["results"][0]
    assert [c["subject"] for c in row["commits"]] == ["flat layout"]


def test_history_limit_is_visible(repository):
    write(repository, "src/pkg/base.py", "class Base:\n def ping(self): return 3\n")
    commit(repository, "third value")
    body = command(repository, "history", "Worker.ping", "-n", "1")
    assert len(body["results"][0]["commits"]) == 1
    assert body["results"][0]["truncated"] and body["diagnostics"][-1]["kind"] == "history_limit"


def test_blame_retains_origin_current_lines_and_original_text(repository):
    write(repository, "src/pkg/base.py", git(repository, "show", "HEAD:src/pkg/base.py") + "\n")
    worker = git(repository, "show", "HEAD:src/pkg/worker.py").replace("timeout = 10", "timeout = 20")
    write(repository, "src/pkg/worker.py", "# moved down\n" + worker + "\n")
    commit(repository, "offset definition")
    body = command(repository, "blame", "HEAD:Worker")
    rows = body["results"]
    assert [r["line"] for r in rows] == [3, 4, 5, 6]
    assert [r["origin"]["line"] for r in rows] == [2, 3, 5, 5]
    assert rows[0]["origin"]["source"] == git(repository, "rev-parse", "v1") + ":pkg/worker.py"
    assert rows[2]["commit"] == git(repository, "rev-parse", "HEAD")
    assert rows[2]["text"] == "  self.timeout = 20"
    assert rows[1]["end_column"] == len(rows[1]["text"])
    assert rows[0]["author"] == "pyrel test"
    assert rows[0]["author_time"] > 0 and rows[0]["author_timezone"]


@pytest.mark.parametrize("encoding", ["utf-8", "latin-1"])
def test_blame_decorators_and_non_ascii_source(repository, encoding):
    write(repository, "src/pkg/base.py", "class Base: pass\n")
    source = f"# coding: {encoding}\ndef wrap(f): return f\n@wrap\ndef café():\n return 'é'  # café\n"
    (repository / "encoded.py").write_bytes(source.encode(encoding))
    commit(repository, "encoded definition")
    rows = command(repository, "blame", "HEAD:encoded.café")["results"]
    assert [r["line"] for r in rows] == [3, 4, 5]
    assert rows[-1]["text"] == " return 'é'  # café" and rows[-1]["end_column"] == len(rows[-1]["text"])


@pytest.mark.parametrize("cmd", ["history", "blame"])
def test_empty_module_has_no_invented_history_or_lines(repository, cmd):
    write(repository, "src/pkg/base.py", "class Base: pass\n")
    write(repository, "empty.py", "")
    commit(repository, "empty module")
    rows = command(repository, cmd, "HEAD:empty")["results"]
    assert (rows[0]["commits"] if cmd == "history" else rows) == []


@pytest.mark.parametrize("args", [
    ["history", "Base", "-n0"], ["diff", "v1..v2:Base", "-U-1"], ["blame", "Base", "-n1"],
    ["callee", "v1:Base.ping", "-U0"], ["history", "Base", "--receiver", "Worker"],
    ["blame", "Base", "--stdin-filename", "file.py"], ["diff", "v1..v2:Base", "--rev", "v1"],
])
def test_git_query_option_errors_are_actionable(repository, args):
    result = ripgit(*args, "--json", cwd=repository)
    assert result.returncode == 2 and json.loads(result.stdout)["error"]["message"]


@pytest.mark.parametrize("args", [["diff", "v1..v2:Worker.ping"], ["history", "Worker.ping"],
                                 ["blame", "v1:Worker.ping"]])
def test_git_queries_text_json_and_timing(repository, args):
    first = ripgit(*args, "--json", cwd=repository)
    second = ripgit(*args, "--json", "--stats", cwd=repository)
    assert first.returncode == second.returncode == 0 and first.stdout == second.stdout
    assert "total=" in second.stderr
    text = ripgit(*args, cwd=repository)
    assert text.returncode == 0 and "pkg/base.py:2:0-2:" in text.stdout
    assert "pkg.base.Base.ping" in text.stdout or "return 1" in text.stdout
