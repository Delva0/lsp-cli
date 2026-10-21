"""Independent ordered queries share input work without sharing semantic state."""

import io
import json
import subprocess

import pytest

from lsp_cli import query_cli as cli, session

from .test_cli import CLI, SPAN, invoke
from .test_git_queries import commit
from .test_git_sources import git, address_query, write
from .test_git_sources import repository as repository  # noqa: PLC0414 -- Re-export the shared pytest fixture.


@pytest.mark.parametrize(("command", "targets", "options"), [
    ("outline", ["v1:pkg.worker", "v2:pkg.base"], []),
    ("defs", ["v1:Worker", "v2:Worker.ping"], []),
    ("show", ["v1:Worker.ping", "v2:Worker.ping", "v1:Worker.ping"], []),
    ("callee", ["v1:Worker.run", "v2:Worker.run"], ["--edge", "resolved"]),
    ("callers", ["v1:Worker.run", "v1:Base.ping"], []),
    ("refs", ["v1:self.timeout", "v2:self.timeout"], ["--role", "write"]),
    ("trace", ["v1:Worker.run", "v1:Worker.ping"], ["--event", "CALL,ATTR_WRITE"]),
    ("inherits", ["v2:Worker", "v2:Base"], []),
    ("diff", ["v1..v2:Worker.ping", "v1..v2:Worker.run"], ["-U0"]),
    ("history", ["Worker.run", "v1:Worker.ping"], ["-n1"]),
    ("blame", ["v1:Worker.run", "v2:Worker.run"], []),
])
def test_every_single_target_command_preserves_each_independent_query(repository, command, targets, options):
    expected = []
    for target in targets:
        one = address_query(command, target, *options, "--json", cwd=repository / "src/pkg")
        assert one.returncode == 0, one.stdout + one.stderr
        expected.append({"input": target, **json.loads(one.stdout)})
    result = address_query(command, *targets, *options, "--json", cwd=repository / "src/pkg")
    assert result.returncode == 0, result.stdout + result.stderr
    body = json.loads(result.stdout)
    assert body == {"schema_version": 1, "command": command, "status": "ok", "queries": expected}
    assert all(row.keys() >= SPAN for group in body["queries"] for row in group["results"])


@pytest.mark.parametrize("extra", [[], ["--max-files", "1"]])
def test_cached_git_inputs_do_not_expand_another_query_scope_or_budget(repository, extra):
    write(repository, "src/pkg/base.py", "class Base:\n def ping(self): return 2\n")
    write(repository, "one.py", "def run(): return 1\n")
    write(repository, "two.py", "def run(): return 2\n")
    commit(repository, "separate scopes")
    targets = ["HEAD:one.run", "HEAD:two.run", "HEAD:Worker.run", "HEAD:one.run"]
    expected = [json.loads(address_query("trace", target, *extra, "--json", cwd=repository).stdout) for target in targets]
    result = address_query("trace", *targets, *extra, "--json", cwd=repository)
    assert result.returncode == 0
    assert json.loads(result.stdout)["queries"] == [
        {"input": target, **row} for target, row in zip(targets, expected, strict=True)
    ]


@pytest.mark.parametrize("bad", ["missing:Worker.run", "v1:Worker.missing", ":Worker.run", "v1:"])
def test_bad_target_preserves_successful_siblings_in_order(repository, bad):
    targets = [bad, "v1:Worker.run", bad, "v2:Worker.ping"]
    result = address_query("show", *targets, "--json", cwd=repository)
    body = json.loads(result.stdout)
    assert result.returncode == 2 and body["status"] == "partial"
    assert [row["input"] for row in body["queries"]] == targets
    assert ["error" in row for row in body["queries"]] == [True, False, True, False]
    text = address_query("show", *targets, cwd=repository)
    assert text.returncode == 2 and not text.stderr
    assert "[2/4] show v1:Worker.run" in text.stdout and "pkg.worker.Worker.run" in text.stdout


def test_unparseable_git_input_keeps_its_error_and_other_revision_readable(repository):
    write(repository, "bad.py", "def broken(:\n")
    commit(repository, "broken source")
    result = address_query("show", "HEAD:bad", "v1:Worker.run", "--json", cwd=repository)
    body = json.loads(result.stdout)
    assert result.returncode == 2 and body["status"] == "partial"
    single = address_query("show", "HEAD:bad", "--json", cwd=repository)
    assert body["queries"][0] == {"input": "HEAD:bad", **json.loads(single.stdout)}
    assert body["queries"][1]["results"][0]["symbol"] == "pkg.worker.Worker.run"


def test_stdin_syntax_errors_retain_the_original_spans_for_each_query():
    result = invoke("show", "one", "two", "--json", source="def broken(:\n")
    body = json.loads(result.stdout)
    assert result.returncode == 2 and body["status"] == "error"
    assert all(row["error"].keys() >= SPAN and row["error"]["line"] == 1 for row in body["queries"])


def test_stdin_read_once_analysis_reused_and_query_state_isolated(monkeypatch, capsys):
    source = """def target(): pass
class Base:
 def ping(self): return target()
def factory():
 alias = target
 class Child(Base):
  def run(self): return super().ping()
 def local():
  alias()
  return Child().run()
 return local
def run():
 callback = factory()
 return callback()
"""
    targets = ["__stdin__.run", "factory.local", "factory.Child.run", "__stdin__.run"]
    expected = [json.loads(invoke("callee", target, "--json", source=source).stdout) for target in targets]
    builds, reads = [], []
    original = session.Analysis

    def analyze(sources):
        builds.append(sources)
        return original(sources)

    class Stdin(io.StringIO):
        def read(self, *args):
            reads.append(True)
            assert len(reads) == 1
            return super().read(*args)

    monkeypatch.setattr(session, "Analysis", analyze)
    monkeypatch.setattr(cli.sys, "stdin", Stdin(source))
    assert cli.relationship_main(["callee", *targets, "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["queries"] == [{"input": target, **row} for target, row in zip(targets, expected, strict=True)]
    assert len(builds) == 1
    assert body["queries"][2]["results"][-1]["target"] == "__stdin__.Base.ping"


def test_git_reuses_inventory_blobs_and_analysis_with_original_query_metadata(repository, monkeypatch, capsys):
    git_calls, blobs, builds = [], [], []
    original_git, original_analysis = session.GitSources.git, session.Analysis

    def read_git(self, *args, **kwargs):
        git_calls.append(args)
        if args[0] == "cat-file":
            blobs.extend(kwargs["input_data"].decode().splitlines() if "--batch" in args else [args[-1]])
        return original_git(self, *args, **kwargs)

    def analyze(sources):
        builds.append(sources)
        return original_analysis(sources)

    monkeypatch.setattr(session.GitSources, "git", read_git)
    monkeypatch.setattr(session, "Analysis", analyze)
    assert cli.relationship_main(["show", "v1:Worker.run", "v1:Worker.ping", "--repo", str(repository), "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert sum(args[0] == "ls-tree" for args in git_calls) == 1
    assert len(blobs) == len(set(blobs))
    assert len(builds) == 1
    direct, inherited = [row["git"] for row in body["queries"]]
    assert direct["commit"] == inherited["commit"]
    # A direct source read no longer loads imports for binding analysis.
    assert direct["analyzed_files"] < inherited["analyzed_files"]


def test_module_identity_hints_are_per_query_and_stdin_encoding_is_preserved(tmp_path):
    (tmp_path / "task.py").write_text("def run(): return 1\n")
    targets = ["one.task.run", "two.task.run"]
    result = invoke("show", *targets, "--file", "task.py", "--json", cwd=tmp_path)
    groups = json.loads(result.stdout)["queries"]
    assert result.returncode == 0
    assert [row["sources"][0]["module"] for row in groups] == ["one.task", "two.task"]
    source = "# coding: latin-1\ndef café(): pass\ndef run(): café()\n".encode("latin-1")
    result = subprocess.run([*CLI, "show", "café", "run", "--json"], input=source, capture_output=True, check=False)
    assert result.returncode == 0
    assert json.loads(result.stdout)["queries"][0]["results"][0]["code"] == "def café(): pass"


def test_revision_options_apply_independently_and_bare_symbols_do_not_inherit_sibling_revision(repository):
    result = address_query("show", "v1:Worker.ping", "v2:Worker.ping", "--rev", "v1", "--json", cwd=repository)
    groups = json.loads(result.stdout)["queries"]
    assert result.returncode == 2 and "error" not in groups[0]
    assert "different Git commits" in groups[1]["error"]["message"]
    result = address_query("show", "v1:Worker.run", "local", "--json", cwd=repository, source="def local(): pass\n")
    groups = json.loads(result.stdout)["queries"]
    assert result.returncode == 0 and "git" in groups[0] and "git" not in groups[1]
    assert groups[1]["results"][0]["source"] == "<stdin>"


@pytest.mark.parametrize("args", [["path", "v1:Worker.run", "v1:Base.ping", "v2:Worker.run", "v2:Base.ping"],
                                 ["where", "v1:pkg.worker", "v2:pkg.worker", "--line", "4"]])
def test_path_and_where_keep_their_original_arity(repository, args):
    result = address_query(*args, "--json", cwd=repository)
    assert result.returncode == 2 and "queries" not in json.loads(result.stdout)


def test_all_failed_status_stats_and_single_protocol(repository):
    result = address_query("show", "v1:Worker.missing", "v2:Worker.missing", "--json", "--stats", cwd=repository)
    assert result.returncode == 2 and json.loads(result.stdout)["status"] == "error"
    assert "total=" in result.stderr
    single = address_query("show", "v1:Worker.run", "--json", cwd=repository)
    assert single.returncode == 0 and "queries" not in json.loads(single.stdout)
    assert git(repository, "status", "--porcelain") == "M src/pkg/base.py"
