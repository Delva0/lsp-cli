"""Batch queries preserve concrete bindings when imports and modules share names."""

import pytest

from .test_actions import query
from .test_git_sources import git, write
from .test_git_sources import repository as repository  # noqa: PLC0414


@pytest.fixture
def collision_repo(repository):
    write(repository, "src/pkg/__init__.py", "from . import helpers\nfrom . import implementation as shadow\n")
    write(repository, "src/pkg/helpers.py", "def prepare(): return 1\nclass Worker:\n def run(self): return 2\n")
    write(repository, "src/pkg/shadow.py", "def original(): return 3\n")
    write(repository, "src/pkg/implementation.py", "def replacement(): return 4\n")
    git(repository, "add", "src/pkg/__init__.py", "src/pkg/helpers.py",
        "src/pkg/shadow.py", "src/pkg/implementation.py")
    git(repository, "commit", "-qm", "modules with same-named package imports")
    return repository


@pytest.mark.parametrize("selector", [("HEAD:*",), ("HEAD:pkg.helpers",), ("-e", r"HEAD:^pkg\.helpers$")])
def test_list_keeps_matching_subtrees_despite_same_named_import(collision_repo, selector):
    result = query("list", *selector, "-r", "--kinds", "function", "method", "--repo", str(collision_repo))
    rows = {row["symbol"]: row for row in result["results"]}
    assert {"pkg.helpers.prepare", "pkg.helpers.Worker.run"} <= rows.keys()
    if selector != ("HEAD:*",):
        assert set(rows) == {"pkg.helpers.prepare", "pkg.helpers.Worker.run"}
    assert rows["pkg.helpers.prepare"]["source"] == "HEAD:src/pkg/helpers.py"
    assert rows["pkg.helpers.prepare"]["line"] == 1
    assert rows["pkg.helpers.Worker.run"]["line"] == 3
    assert all(not item.get("errors") for item in result["queries"])


def test_list_preserves_both_declarations_but_file_address_selects_its_module(collision_repo):
    result = query("list", "HEAD:pkg.helpers", "-d", "0", "--repo", str(collision_repo))
    assert {(r["source"], r["symbol"], r["kind"]) for r in result["results"]} == {
        ("HEAD:src/pkg/__init__.py", "pkg.helpers", "import"),
        ("HEAD:src/pkg/helpers.py", "pkg.helpers", "module"),
    }
    module = query("list", "HEAD:src/pkg/helpers.py", "-d", "0", "--repo", str(collision_repo))
    assert [(r["source"], r["kind"]) for r in module["results"]] == [("HEAD:src/pkg/helpers.py", "module")]
    scoped = query("list", "HEAD:pkg.helpers", "-r", "--files", "src/pkg/__init__.py", "--repo", str(collision_repo))
    assert [(r["source"], r["kind"]) for r in scoped["results"]] == [("HEAD:src/pkg/__init__.py", "import")]


def test_show_follows_selected_import_and_deduplicates_proven_definition(collision_repo):
    result = query("show", "HEAD:pkg.helpers", "--repo", str(collision_repo))
    assert len(result["results"]) == 1
    assert result["results"][0]["source"] == "HEAD:src/pkg/helpers.py"
    assert result["results"][0]["code"].startswith("def prepare()")


def test_show_does_not_guess_same_named_module_is_import_target(collision_repo):
    result = query("show", "HEAD:pkg.shadow", "--repo", str(collision_repo))
    assert {r["source"] for r in result["results"]} == {
        "HEAD:src/pkg/shadow.py", "HEAD:src/pkg/implementation.py",
    }
    assert {r["symbol"] for r in result["results"]} == {"pkg.shadow", "pkg.implementation"}


def test_unresolved_import_retains_its_source_without_guessing_a_module(collision_repo):
    write(collision_repo, "src/pkg/__init__.py", "from .missing import something as shadow\n")
    git(collision_repo, "add", "src/pkg/__init__.py")
    git(collision_repo, "commit", "-qm", "unresolved same-named import")
    result = query("show", "HEAD:pkg.shadow", "--repo", str(collision_repo))
    assert {r["source"] for r in result["results"]} == {
        "HEAD:src/pkg/__init__.py", "HEAD:src/pkg/shadow.py",
    }
    assert any(d["kind"] == "unresolved" and "pkg.shadow" in d["message"] for d in result["diagnostics"])
