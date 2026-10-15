"""Candidate planning preserves exhaustive results without parsing unrelated code."""

import pytest

from pyrel import batch

from .test_actions import query
from .test_git_sources import git, write
from .test_git_sources import repository as repository  # noqa: PLC0414


def test_direct_reads_do_not_spend_the_file_budget_on_unrelated_code(repository):
    write(repository, "api.py", "class API:\n def run(self): return 1\n")
    for i in range(150):
        write(repository, f"noise/n{i}.py", "def irrelevant(): return 0\n")
    git(repository, "add", "api.py", "noise")
    git(repository, "commit", "-qm", "many unrelated modules")
    for target in ("API.run", "api.API.run", "api.py"):
        result = query("show", "HEAD:" + target, "--repo", str(repository), "--max-files", "2")
        assert result["coverage"]["complete"]
        assert result["coverage"]["analyzed_files"] == 1
        assert [s["source"] for s in result["sources"]] == ["HEAD:api.py"]


@pytest.mark.parametrize(("action", "pattern"), [
    ("show", "Base.ping"), ("show", "Worker.ping"), ("show", "Worker.p*"),
    ("show", "ping"), ("show", "pkg.base.Base.ping"), ("show", "pkg.**"),
    ("show", "Task.run"), ("show", "base"), ("show", "missing"),
    ("list", "pkg"), ("list", "pkg/*"), ("list", "Worker"),
])
def test_candidate_plan_matches_exhaustive_selection(repository, monkeypatch, action, pattern):
    arguments = [action, "v1:" + pattern, "--repo", str(repository)]
    result = query(*arguments, status=1 if pattern == "missing" else 0)
    monkeypatch.setattr(batch, "candidate_paths", lambda loader, *_args, **_kwargs: set(loader.paths))
    monkeypatch.setattr(batch, "lexical_candidate", lambda *_args: True)
    exhaustive = query(*arguments, status=1 if pattern == "missing" else 0)
    for key in ("results", "heads", "occurrences", "status"):
        assert result.get(key) == exhaustive.get(key), key
    assert [q["matches"] for q in result["queries"]] == [q["matches"] for q in exhaustive["queries"]]


def test_normalized_identifiers_and_same_named_symbols_are_not_dropped(repository):
    write(repository, "one.py", "def K(): return 1\n")
    write(repository, "two.py", "def K(): return 2\n")
    write(repository, "three.py", "def K(): return 3\n")
    git(repository, "add", "one.py", "two.py", "three.py")
    git(repository, "commit", "-qm", "three equivalent identifier spellings")
    result = query("show", "HEAD:K", "--repo", str(repository))
    assert {row["symbol"] for row in result["results"]} == {"one.K", "two.K", "three.K"}


def test_scoped_name_colliding_with_a_module_keeps_all_matches(repository):
    write(repository, "collision.py", "class pkg:\n class base:\n  class Base:\n   def ping(self): return 3\n")
    git(repository, "add", "collision.py")
    git(repository, "commit", "-qm", "qualified name suffix collision")
    result = query("show", "HEAD:pkg.base.Base.ping", "--repo", str(repository))
    assert {row["symbol"] for row in result["results"]} == {"pkg.base.Base.ping", "collision.pkg.base.Base.ping"}
