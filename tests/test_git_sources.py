"""Verify Git revision discovery against real repositories and semantic queries."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from .test_cli import SPAN, invoke


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def write(repo, path, content):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "lsp_cli@example.invalid")
    git(tmp_path, "config", "user.name", "lsp_cli test")
    write(tmp_path, "pkg/__init__.py", "from .base import Base\n")
    write(tmp_path, "pkg/base.py", "class Base:\n def ping(self): return 1\n")
    write(tmp_path, "pkg/worker.py", "from . import Base as Parent\n"
          "class Worker(Parent):\n def run(self):\n  self.timeout = 10\n  return super().ping()\n")
    write(tmp_path, "pkg/client.py", "from .worker import Worker as Task\ndef start(): return Task().run()\n")
    write(tmp_path, "unrelated.py", "raise RuntimeError('source must never execute')\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "flat layout")
    git(tmp_path, "tag", "v1")
    (tmp_path / "src").mkdir()
    git(tmp_path, "mv", "pkg", "src/pkg")
    write(tmp_path, "src/pkg/base.py", "class Base:\n def ping(self): return 2\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "src layout")
    git(tmp_path, "tag", "v2")
    write(tmp_path, "src/pkg/base.py", "invalid dirty worktree!\n")
    return tmp_path


def run(repo, *args, rev="v1"):
    result = invoke(*args, "--repo", str(repo), "--rev", rev, "--json", source="")
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("rev", ["v1", "v2"])
def test_revision_cross_file_mro_and_dirty_worktree(repository, rev):
    before = git(repository, "status", "--porcelain")
    body = run(repository, "trace", "pkg.worker.Worker.run", rev=rev)
    assert body["git"]["complete"]
    assert body["git"]["analyzed_files"] == 3
    assert next(r for r in body["results"] if r.get("callee") == "super().ping")["target"] == "pkg.base.Base.ping"
    assert next(r for r in body["results"] if r["kind"] == "ATTR_WRITE")["line"] == 4
    assert all(r.keys() >= SPAN for r in body["results"])
    for source in body["sources"]:
        assert source["git_commit"] == git(repository, "rev-parse", rev)
        assert source["git_blob"] == git(repository, "rev-parse", rev + ":" + source["git_path"])
        assert source["source"] == rev + ":" + source["git_path"]
    assert git(repository, "status", "--porcelain") == before


def test_short_symbol_and_static_cross_file_path(repository):
    assert run(repository, "callee", "Worker.run")["results"][-1]["target"] == "pkg.base.Base.ping"
    body = run(repository, "path", "pkg.client.start", "pkg.base.Base.ping")
    assert body["status"] == "found"


@pytest.mark.parametrize("directory", [".", "src/pkg"])
@pytest.mark.parametrize("rev", ["v1", "v2"])
def test_revision_defaults_to_current_repository(repository, directory, rev):
    result = invoke("show", "Worker.ping", "--rev", rev, "--json", source="", cwd=repository / directory)
    assert result.returncode == 0, result.stdout + result.stderr
    body = json.loads(result.stdout)
    assert body == run(repository, "show", "Worker.ping", rev=rev)
    assert body["results"][0]["symbol"] == "pkg.base.Base.ping"
    assert body["git"]["repository"] == str(repository)


def test_explicit_repository_overrides_cwd(repository, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = invoke("show", "Worker.ping", "--repo", str(repository / "src/pkg"),
                    "--json", source="", cwd=elsewhere)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == run(repository, "show", "Worker.ping", rev="HEAD")


@pytest.mark.parametrize("kind", ["bare", "worktree"])
def test_repository_discovery_supports_bare_and_linked_worktree(repository, tmp_path, kind):
    target = tmp_path / kind
    if kind == "bare":
        git(repository, "clone", "--bare", "-q", str(repository), str(target))
    else:
        git(repository, "worktree", "add", "--detach", "-q", str(target), "v1")
    result = invoke("show", "Worker.ping", "--rev", "v1", "--json", source="",
                    cwd=target if kind == "bare" else target / "pkg")
    assert result.returncode == 0, result.stdout + result.stderr
    body = json.loads(result.stdout)
    expected = run(repository, "show", "Worker.ping")
    assert body["results"] == expected["results"]
    assert body["git"]["commit"] == expected["git"]["commit"]
    assert body["git"]["repository"] == str(target)


def test_revision_outside_repository_reports_git_error(tmp_path):
    result = invoke("show", "Worker.run", "--rev", "v1", "--json", source="", cwd=tmp_path)
    assert result.returncode == 2
    assert "not a git repository" in json.loads(result.stdout)["error"]["message"]


def test_stdin_in_repository_remains_the_default_input(repository):
    code = "def local_only(): return 42\n"
    result = invoke("show", "local_only", "--json", source=code, cwd=repository / "src/pkg")
    assert result.returncode == 0, result.stdout + result.stderr
    body = json.loads(result.stdout)
    assert "git" not in body
    assert body["results"][0]["code"] == code.rstrip()
    assert body["results"][0]["source"] == "<stdin>"


def test_reverse_importers_are_discovered_without_explicit_files(repository):
    body = run(repository, "callers", "pkg.worker.Worker.run")
    assert any(r["caller"] == "pkg.client.start" for r in body["results"])
    inherited = run(repository, "inherits", "pkg.base.Base")["results"][0]
    assert "pkg.worker.Worker" in str(inherited["subclasses"])
    refs = run(repository, "refs", "self.timeout", "--role", "write")["results"]
    assert len(refs) == 1 and refs[0]["line"] == 4


def test_file_selection_outline_where_and_encoding(repository):
    body = run(repository, "outline", "--file", "pkg/worker.py")
    assert {s["module"] for s in body["sources"]} == {"pkg.worker"}
    scopes = run(repository, "where", "--file", "pkg/worker.py", "--line", "4")["results"]
    assert [scope["symbol"] for scope in scopes] == ["pkg.worker", "pkg.worker.Worker", "pkg.worker.Worker.run"]
    (repository / "encoded.py").write_bytes(b"# coding: latin-1\ndef caf\xe9(): pass\n")
    git(repository, "add", "encoded.py")
    git(repository, "commit", "-qm", "encoded source")
    assert run(repository, "outline", "--file", "encoded.py", rev="HEAD")["results"][-1]["symbol"] == "encoded.café"


def test_source_limit_is_reported_as_incomplete(repository):
    body = run(repository, "trace", "pkg.worker.Worker.run", "--max-files", "1")
    assert not body["git"]["complete"] and body["git"]["omitted_files"]
    assert next(r for r in body["results"] if r.get("callee") == "super().ping")["resolution"] == "unresolved"
    assert any(d["kind"] == "source_limit" for d in body["diagnostics"])


def test_stable_json_with_optional_stderr_timings(repository):
    args = ["trace", "pkg.worker.Worker.run", "--repo", str(repository), "--rev", "v1", "--json"]
    first, second = invoke(*args, source=""), invoke(*args, "--stats", source="")
    assert first.returncode == second.returncode == 0 and first.stdout == second.stdout
    assert "analysis=" in second.stderr


@pytest.mark.parametrize("extra", [["--rev", "missing"], ["--file", "missing.py"], ["--file", "../base.py"]])
def test_git_errors_are_actionable_json(repository, extra):
    result = invoke("trace", "Worker.run", "--repo", str(repository), *extra, "--json", source="")
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["message"]


def test_invalid_revision_suggests_existing_tag_without_switching(repository):
    git(repository, "tag", "2.2.1", "v1")
    result = invoke("show", "Worker.run", "--repo", str(repository),
                    "--rev", "v2.2.1", "--json", source="")
    assert result.returncode == 2
    message = json.loads(result.stdout)["error"]["message"]
    assert "Cannot resolve Git revision 'v2.2.1'" in message
    assert "Similar existing refs: '2.2.1'" in message
    assert repr(str(repository)) in message
    assert "Retry the same symbol query with an exact revision" in message
    fixed = run(repository, "show", "Worker.run", rev="2.2.1")
    assert fixed["results"][0]["source"] == "2.2.1:pkg/worker.py"


def test_invalid_revision_without_close_ref_keeps_git_error(repository):
    result = invoke("show", "Worker.run", "--repo", str(repository),
                    "--rev", "no-such-release-98765", "--json", source="")
    assert result.returncode == 2
    message = json.loads(result.stdout)["error"]["message"]
    assert "Cannot resolve Git revision 'no-such-release-98765'" in message
    assert "git tag --list" in message
    assert "git:" in message


def test_custom_import_root_from_configuration(repository):
    write(repository, "lib/custom.py", "def start(): return 42\n")
    write(repository, "pyproject.toml", '[project]\nname = "example"\n')
    write(repository, "setup.cfg", "[options]\npackage_dir =\n = lib\n")
    git(repository, "add", "lib", "pyproject.toml", "setup.cfg")
    git(repository, "commit", "-qm", "custom layout")
    assert run(repository, "callee", "custom.start", rev="HEAD")["symbol"]["symbol"] == "custom.start"


@pytest.mark.parametrize(("rev", "value", "path"), [("v1", "1", "pkg/base.py"), ("v2", "2", "src/pkg/base.py")])
def test_show_git_inherited_method_uses_revision_not_worktree(repository, rev, value, path):
    body = run(repository, "show", "pkg.worker.Worker.ping", rev=rev)
    row = body["results"][0]
    assert row["symbol"] == "pkg.base.Base.ping" and row["source"] == rev + ":" + path
    assert row["code"] == " def ping(self): return " + value


def address_query(*args, cwd, source=""):
    return subprocess.run([sys.executable, "-c",
                           "from lsp_cli.query_cli import relationship_main; "
                           "raise SystemExit(relationship_main(prog='lsp'))", *args], cwd=cwd,
                          input=source, text=True, capture_output=True, check=False)


@pytest.mark.parametrize(("addressed", "unversioned"), [
    (["show", "v1:Worker.ping"], ["show", "Worker.ping"]),
    (["callee", "v1:Worker.run"], ["callee", "Worker.run"]),
    (["trace", "v1:Worker.run"], ["trace", "Worker.run"]),
    (["callers", "v1:pkg.worker.Worker.run"], ["callers", "pkg.worker.Worker.run"]),
    (["refs", "v1:self.timeout", "--role", "write"], ["refs", "self.timeout", "--role", "write"]),
    (["inherits", "v1:pkg.base.Base"], ["inherits", "pkg.base.Base"]),
    (["path", "v1:pkg.client.start", "v1:pkg.base.Base.ping"], ["path", "pkg.client.start", "pkg.base.Base.ping"]),
    (["outline", "v1:pkg.worker"], ["outline", "--file", "pkg/worker.py"]),
    (["where", "v1:pkg.worker", "--line", "4"], ["where", "--file", "pkg/worker.py", "--line", "4"]),
])
def test_lsp_addresses_preserve_semantics_and_spans(repository, addressed, unversioned):
    result = address_query(*addressed, "--json", cwd=repository / "src/pkg")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == run(repository, *unversioned)


def test_lsp_accepts_ref_expressions_aliases_and_one_explicit_revision(repository):
    result = address_query("show", "HEAD^:Worker.ping", "--json", cwd=repository)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["results"][0]["source"] == "HEAD^:pkg/base.py"
    git(repository, "tag", "release/old", "v1")
    result = address_query("path", "v1:pkg.client.start", "release/old:pkg.base.Base.ping", "--json", cwd=repository)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "found"
    result = address_query("path", "v1:pkg.client.start", "pkg.base.Base.ping", "--json", cwd=repository)
    assert result.returncode == 0 and json.loads(result.stdout)["status"] == "found"


@pytest.mark.parametrize("args", [
    ["path", "v1:pkg.client.start", "v2:pkg.base.Base.ping"],
    ["show", "v1:Worker.ping", "--rev", "v2"],
    ["callee", "v1:Base.ping", "--receiver", "v2:Worker"],
])
def test_lsp_rejects_mixed_commits_before_analysis(repository, args):
    result = address_query(*args, "--json", cwd=repository)
    assert result.returncode == 2
    body = json.loads(result.stdout)
    assert "different Git commits" in body["error"]["message"]
    assert not body["results"] and not body["sources"]


@pytest.mark.parametrize("args", [
    ["show", "v1:"], ["show", ":Worker.run"], ["show", "missing:Worker.run"],
    ["outline", "v1:Worker"], ["where", "v1:Worker", "--line", "3"],
])
def test_lsp_invalid_addresses_report_errors(repository, args):
    result = address_query(*args, "--json", cwd=repository)
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["message"]


def test_lsp_module_source_and_stdin(repository):
    result = address_query("show", "v1:pkg.worker", "--json", cwd=repository)
    assert result.returncode == 0, result.stdout + result.stderr
    row = json.loads(result.stdout)["results"][0]
    assert row["symbol_kind"] == "module" and row["line"] == 1
    assert row["code"] == git(repository, "show", "v1:pkg/worker.py")
    code = "def local_only(): return 42\n"
    result = address_query("show", "local_only", "--json", cwd=repository, source=code)
    assert result.returncode == 0, result.stdout + result.stderr
    body = json.loads(result.stdout)
    assert "git" not in body and body["results"][0]["code"] == code.rstrip()


def test_lsp_entrypoint_reports_its_name(repository):
    result = address_query("--version", cwd=repository)
    assert result.returncode == 0 and result.stdout.startswith("lsp ")
    result = address_query("--help", cwd=repository)
    assert result.returncode == 0 and "usage: lsp" in result.stdout and "REV:" in result.stdout
    result = address_query("show", "v1:", cwd=repository)
    assert result.returncode == 2 and result.stderr.startswith("lsp:")
