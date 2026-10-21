"""Candidate planning preserves exhaustive results without parsing unrelated code."""

import pytest

from lsp_cli import batch

from .test_actions import query
from .test_git_sources import git, write
from .test_git_sources import repository as repository  # noqa: PLC0414


@pytest.mark.parametrize("action", ["show", "list", "diff"])
@pytest.mark.parametrize("pattern", ["test_*preempt*", "test_?reempt*", "test_*leak*", "{missing,test_*preempt*}"])
def test_name_glob_and_absent_batch_member_match_exhaustive_results(repository, monkeypatch, action, pattern):
    # This fixture's v2 intentionally contains a syntax error. The exhaustive
    # reference needs a valid unrelated base so additions/deletions are provable.
    write(repository, "src/pkg/base.py", "class Base:\n def ping(self): return 1\n")
    write(repository, "tests/cases.py", "def test_preempt_old(): return 1\ndef test_leak(): return 2\n")
    write(repository, "tests/unicode.py", "def ｔｅｓｔ_preempt_unicode(): return 3\n")
    for number in range(25):
        write(repository, f"noise/n{number}.py", "# 非 ASCII 注释\ndef unrelated(): return 0\n")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "glob baseline")
    git(repository, "tag", "glob-before")
    write(repository, "tests/cases.py", "def test_preempt_added(): return 4\ndef test_leak(): return 5\n")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "added deleted and modified tests")
    revision = "glob-before..HEAD" if action == "diff" else "HEAD"
    args = (action, revision + ":" + pattern, "--repo", str(repository))
    result = query(*args, "--max-files", "100" if "missing" in pattern else "8")
    assert all(c["complete"] and c["parsed_files"] < 8
               for q in result["queries"] for c in q["coverage"])
    monkeypatch.setattr(batch, "candidate_paths", lambda loader, *_a, **_kw: set(loader.paths))
    monkeypatch.setattr(batch, "lexical_candidate", lambda *_a, **_kw: True)
    exhaustive = query(*args)
    for key in ("results", "heads", "status"):
        assert result.get(key) == exhaustive.get(key)
    assert [q["matches"] for q in result["queries"]] == [q["matches"] for q in exhaustive["queries"]]


def test_absent_name_does_not_parse_unrelated_sources_but_matching_syntax_errors_remain(repository):
    for number in range(25):
        write(repository, f"noise/n{number}.py", "def unrelated(): return 0\n")
    write(repository, "src/pkg/base.py", "class Base:\n def ping(self): return 1\n")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "missing symbol and relevant invalid syntax")
    for name in ("absent", "absent_*"):
        result = query("show", "HEAD:" + name, "--repo", str(repository), status=1)
        assert result["coverage"]["complete"] and result["coverage"]["parsed_files"] == 0
    write(repository, "broken.py", "def broken_target(:\n")
    git(repository, "add", "broken.py")
    git(repository, "commit", "-qm", "invalid source must prevent reliable absence")
    result = query("show", "HEAD:broken_*", "--repo", str(repository), status=2)
    assert not result["coverage"]["complete"]
    assert result["coverage"]["failed_files"] == ["broken.py"]


def test_syntax_proofs_reuse_blobs_across_versions_without_reusing_error_locations(repository, monkeypatch):
    from collections import Counter

    from lsp_cli import git_sources
    from lsp_cli.commands import parser
    from lsp_cli.session import Session

    broken = "def broken_target(\n"
    fixed = "def fixed_target(): return 1\n"
    for name in ("a.py", "b.py"):
        write(repository, name, broken)
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "identical invalid blobs")
    git(repository, "tag", "syntax-before")
    write(repository, "b.py", fixed)
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "new content must be validated")
    counts = Counter()
    original = git_sources.ast.parse

    def parse(text, *args, **kwargs):
        counts[text] += 1
        return original(text, *args, **kwargs)

    monkeypatch.setattr(git_sources.ast, "parse", parse)
    args = parser().parse_args(["show", "HEAD:absent", "--repo", str(repository)])

    def inspect(session, revision):
        loader = session.git(args, revision)
        loader.validate_absence()
        return loader.failed, loader.diagnostics

    with Session() as session:
        before, after = session.parallel([
            (revision, lambda lane, rev=revision: inspect(lane, rev))
            for revision in ("syntax-before", "HEAD")
        ])
    assert counts[broken] == counts[fixed] == 1
    assert {"a.py", "b.py"} <= before[0]
    assert "a.py" in after[0] and "b.py" not in after[0]
    assert any("syntax-before:b.py" in row["message"] for row in before[1])
    assert any("HEAD:a.py" in row["message"] for row in after[1])
    assert all("syntax-before" not in row["message"] for row in after[1])


@pytest.mark.parametrize("pattern", ["**.ping", "*.ping", "**.leaf"])
def test_glob_listing_with_literal_leaf_matches_full_inventory(repository, monkeypatch, pattern):
    write(repository, "extra.py", "class Child:\n def ping(self): pass\n")
    write(repository, "other/leaf/nested.py", "class Nested: pass\n")
    write(repository, "unicode.py", "def ｐｉｎｇ(): pass\n")
    for number in range(40):
        write(repository, f"noise/f{number}.py", "def unused(): pass\n")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "scoped leaf glob")
    args = ["list", "HEAD:" + pattern, "--repo", str(repository), "-r"]
    result = query(*args, "--max-files", "12")
    coverage = [c for q in result["queries"] for c in q["coverage"]]
    assert coverage and all(c["complete"] and c["analyzed_files"] < 12 for c in coverage)
    monkeypatch.setattr(batch, "candidate_paths", lambda loader, *_args, **_kwargs: set(loader.paths))
    exhaustive = query(*args)
    assert result["results"] == exhaustive["results"]


def test_filename_in_explicit_file_scope_does_not_expand_dependencies(repository, monkeypatch):
    from lsp_cli.workspace import Workspace

    write(repository, "nested/task.py", "from pkg.base import Base\nclass Child(Base): pass\n")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "scoped filename")
    monkeypatch.setattr(Workspace, "analysis_sources", lambda *_a, **_kw: pytest.fail("filename expanded dependencies"))
    result = query("show", "HEAD:task.py", "--files", "nested/task.py", "--repo", str(repository))
    explicit = query("show", "HEAD:nested/task.py", "--repo", str(repository))
    assert result["results"] == explicit["results"]
    assert result["coverage"]["analyzed_files"] == 1


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


@pytest.mark.parametrize("pattern", [
    r"^pkg\.base\.Base\.(ping|absent)$",
    r"^pkg\.base\.Base\.ping$|^pkg\.worker\.Worker\.run$",
    r"^pkg\.base\.Base\.?ping$",
    r"^pkg\.worker\.Worker\.(ping|run)$",
])
def test_qualified_regex_plan_matches_exhaustive_inputs(repository, monkeypatch, pattern):
    result = query("show", "-e", "v1:" + pattern, "--repo", str(repository))
    monkeypatch.setattr(batch, "candidate_paths", lambda loader, *_a, **_kw: set(loader.paths))
    monkeypatch.setattr(batch, "lexical_candidate", lambda *_a, **_kw: True)
    reference = query("show", "-e", "v1:" + pattern, "--repo", str(repository))
    assert result["results"] == reference["results"]
    assert result["heads"] == reference["heads"]


def test_regex_member_sets_do_not_scan_unrelated_modules(repository):
    write(repository, "api.py", "class API:\n def run(self): return 1\n def stop(self): return 2\n")
    for i in range(80):
        write(repository, f"noise/n{i}.py", "class Unrelated:\n def ignored(self): return 0\n")
    git(repository, "add", "api.py", "noise")
    git(repository, "commit", "-qm", "regex candidate scope")
    result = query("show", "-e", r"HEAD:^api\.API\.(run|stop)$", "--repo", str(repository), "--max-files", "2")
    assert result["queries"][0]["coverage"][0]["parsed_files"] == 1
    assert {head["symbol"] for head in result["heads"]} == {"api.API.run", "api.API.stop"}


def test_missing_root_with_text_mentions_does_not_load_other_query_dependencies(repository, monkeypatch):
    from lsp_cli.workspace import Workspace

    write(repository, "api.py", "class Actual:\n def run(self): return 1\n")
    write(repository, "mentions.py", "# Missing is a name in a comment, not a declaration.\nimport pkg.worker\n")
    git(repository, "add", "api.py", "mentions.py")
    git(repository, "commit", "-qm", "mentioned absent name")

    def fail(*_args, **_kwargs):
        pytest.fail("The complete binding search already disproved this declaration prefix")

    monkeypatch.setattr(Workspace, "analysis_sources", fail)
    result = query("show", "HEAD:Actual.run", "HEAD:Missing.run", "--repo", str(repository))
    assert [head["symbol"] for head in result["heads"]] == ["api.Actual.run"]
    assert result["queries"][1]["status"] == "no_match"


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


@pytest.mark.parametrize("action", ["show", "list", "history"])
def test_package_prefix_and_unicode_comments_do_not_parse_unrelated_modules(repository, monkeypatch, action):
    from lsp_cli.model import Source

    for i in range(80):
        write(repository, f"src/pkg/noise/n{i}.py", f"# 中文说明 K\nimport pkg\nclass Noise{i}: pass\n")
    git(repository, "add", "src/pkg/noise")
    git(repository, "commit", "-qm", "shared package prefix and non-ASCII comments")
    parsed = []
    original = Source.__post_init__

    def record(source):
        parsed.append(source.name)
        original(source)

    monkeypatch.setattr(Source, "__post_init__", record)
    result = query(action, "HEAD:pkg.worker.Worker.run", "--repo", str(repository))
    assert result["results"]
    assert not any("noise/" in name for name in parsed)
    assert result["coverage"]["complete"]


@pytest.mark.parametrize("spelling", ["K", "K", "\\\n K"])
def test_scope_prefilter_preserves_unicode_and_continued_headers(repository, spelling):
    write(repository, "continued.py", f"class {spelling}:\n def run(self): return 1\n")
    git(repository, "add", "continued.py")
    git(repository, "commit", "-qm", "valid alternative class spelling")
    result = query("show", "HEAD:K.run", "--repo", str(repository))
    assert {row["symbol"] for row in result["results"]} == {"continued.K.run"}


def test_scope_prefilter_keeps_import_and_assignment_alias_fallback(repository):
    write(repository, "aliases.py", "from pkg.worker import Worker as Imported\nAssigned = Imported\n")
    git(repository, "add", "aliases.py")
    git(repository, "commit", "-qm", "aliases are not class declarations")
    for target in ("Imported.run", "Assigned.run", "aliases.Assigned.run"):
        result = query("show", "HEAD:" + target, "--repo", str(repository))
        assert {row["symbol"] for row in result["results"]} == {"pkg.worker.Worker.run"}


def test_batch_blobs_preserve_duplicate_content_and_non_utf8_sources(repository, monkeypatch):
    from lsp_cli.git_sources import GitSources

    content = b"# coding: latin-1\nvalue = 'caf\xe9'\n"
    for name in ("one.py", "two.py"):
        (repository / name).write_bytes(content)
    git(repository, "add", "one.py", "two.py")
    git(repository, "commit", "-qm", "duplicate non-UTF8 blobs")
    loader = GitSources(repository, "HEAD")
    original, commands = loader.git, []

    def record(*args, **kwargs):
        commands.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(loader, "git", record)
    loader.read_many(iter(("one.py", "two.py")))
    assert loader.read("one.py") == loader.read("two.py") == content
    loader.read_many(("two.py", "one.py"))
    assert commands == [("cat-file", "--batch")]
    assert "café" in loader.source("one.py").text
