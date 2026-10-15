"""Python scripts remain readable when their paths are not importable names."""

import pytest

from pyrel import workspace

from .test_actions import query
from .test_git_sources import git, write

CODE = "def helper():\n    return 7\n\ndef run():\n    return helper()\n"
PATHS = [
    "tools-dev/install.py",
    "tools dev/install.py",
    "scripts/install-tool.py",
    "tools.v2/install.py",
    "__init__.py",
]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("mode", ["file", "stdin", "git"])
def test_nonpackage_paths_support_exact_file_and_symbol_reads(tmp_path, monkeypatch, path, mode):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, path, CODE)
    if mode == "git":
        git(tmp_path, "init", "-q")
        git(tmp_path, "add", ".")
        git(tmp_path, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "script")
        args, target, label = ["--rev", "HEAD"], "HEAD:" + path, "HEAD:" + path
        # Versioned reads must use the blob, even when the worktree cannot parse.
        write(tmp_path, path, "not the requested revision!\n")
    else:
        args = ["--stdin-filename", path] if mode == "stdin" else ["--files", path]
        target = label = path
    result = query("show", target, *args, source=CODE)
    assert result["results"][0]["code"] == CODE.rstrip()
    assert result["sources"][0]["source"] == label
    assert result["sources"][0]["module_origin"] == "file"
    symbol = query("show", "run", *args, source=CODE)["results"][0]
    assert {key: symbol[key] for key in ("source", "line", "column", "end_line", "end_column")} == {
        "source": label,
        "line": 4,
        "column": 0,
        "end_line": 5,
        "end_column": 19,
    }
    from .search_helpers import internal_search

    discovered = internal_search("helper", files=args, kinds=["function"], source=CODE)
    assert len(discovered["results"]) == 1
    assert discovered["results"][0]["source"] == label


def test_file_scopes_do_not_normalize_or_merge_distinct_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paths = ["a-b.py", "a_b.py", "a.b/install.py", "a/b/install.py", "a%2Eb/install.py"]
    args = []
    for path in paths:
        write(tmp_path, path, CODE)
        args.extend(["--files", path])
    result = query("list", *args, source="")
    assert len({source["module"] for source in result["sources"]}) == len(paths)
    for path in paths:
        selected = query("show", path, *args, source="")
        assert selected["results"][0]["source"] == path


@pytest.mark.parametrize("native", [True, False])
def test_file_scope_local_calls_and_refs_keep_binding_resolution(tmp_path, monkeypatch, native):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "tools-dev/install.py", CODE)
    args = ["--files", "tools-dev/install.py"]
    if not native:

        def unavailable(*_args):
            raise workspace.NeedsBindingsError("test fallback")

        monkeypatch.setattr(workspace.Workspace, "request", unavailable)
    call = query("show", "run", *args, "--roles", "call")["occurrences"][0]
    assert call["target"] == "tools-dev.install.helper" and call["resolution"] == "resolved"
    refs = query("refs", "helper", *args, "--roles", "call")
    assert {row["line"] for row in refs["occurrences"]} == {5}
    assert query("path", "run", "helper", *args)["status"] == "found"


def test_file_scope_does_not_invent_an_import_alias(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "a-b.py", CODE)
    write(tmp_path, "caller.py", "from a_b import helper\ndef start(): return helper()\n")
    args = ["--files", "a-b.py", "--files", "caller.py", "--roles", "call"]
    call = query("show", "start", *args)["occurrences"][0]
    assert call["resolution"] == "unresolved" and call["target"] is None
    write(tmp_path, "a_b.py", "def helper(): return 42\n")
    call = query("show", "start", *args, "--files", "a_b.py")["occurrences"][0]
    assert call["resolution"] == "resolved" and call["target"] == "a_b.helper"


def test_explicit_identity_stays_validated_and_duplicate_hint_uses_public_flag(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "tools-dev/install.py", CODE)
    invalid = query("show", "run", "--files", "not-valid=tools-dev/install.py", status=2)
    assert "invalid module identity" in invalid["error"]["message"]
    assigned = query("show", "pkg.install.run", "--files", "pkg.install=tools-dev/install.py")
    assert assigned["sources"][0]["module_origin"] == "explicit"
    write(tmp_path, "other.py", CODE)
    duplicate = query("list", "--files", "same=tools-dev/install.py", "same=other.py", status=2)
    assert "--files MODULE=PATH" in duplicate["error"]["message"]
    assert "--source" not in duplicate["error"]["message"]
