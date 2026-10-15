"""The public batch grammar, coverage, original evidence and traversal cost."""

from collections import Counter

from pyrel import relations
from pyrel.commands import parser
from pyrel.selectors import expand

from .test_actions import invoke, query
from .test_git_sources import git, write
from .test_git_sources import repository as repository  # noqa: PLC0414


def test_rg_matching_modes_and_union():
    source = "def foo_bar(): pass\ndef Foo_baz(): pass\ndef same(): pass\n"
    assert [r["symbol"] for r in query("show", "-e", "^foo.*bar$", source=source)["results"]] == ["__stdin__.foo_bar"]
    assert len(query("show", "-i", "foo_*", source=source)["queries"][0]["matches"]) == 2
    assert query("show", "-F", "foo.*bar", source=source, status=1)["queries"][0]["status"] == "no_match"
    assert query("show", "-F", "oo_ba", source=source)["results"][0]["symbol"] == "__stdin__.foo_bar"
    assert query("show", "-P", "-e", r"foo(?=_bar)", source=source)["results"]
    result = query("show", "-e", "^foo_bar$", "same", "-e", "^same$", source=source)
    assert len(result["results"]) == 2
    assert [q["matching"] for q in result["queries"]] == ["regex", "glob", "regex"]
    assert all("results" not in q for q in result["queries"])
    assert all(q["result_indices"] for q in result["queries"])
    assert invoke("show", "-P", "same", source=source)[0] == 2


def test_invalid_regex_does_not_discard_other_queries():
    result = query("show", "leaf", "-e", "[", status=2)
    assert result["results"] and result["queries"][0]["status"] == "ok"
    assert result["queries"][1]["errors"]


def test_invalid_symbol_braces_fail_before_loading_a_revision(monkeypatch):
    from pyrel import batch

    def unexpected(*args, **kwargs):
        raise AssertionError("An invalid symbol set must not load any repository sources")

    monkeypatch.setattr(batch, "revision_job", unexpected)
    for args in (("show", "HEAD:{ParallelConfig}"), ("list", "HEAD:Class.{method}"),
                 ("path", "HEAD:start", "{end}")):
        result = query(*args, status=2)
        assert "Invalid symbol set" in result["queries"][0]["errors"][0]["message"]


def test_invalid_symbol_braces_do_not_discard_valid_queries():
    result = query("show", "leaf", "{leaf}", status=2)
    assert result["queries"][0]["status"] == "ok"
    assert result["queries"][1]["errors"]
    for args in (["where", "HEAD:pkg/{literal}.py:2"], ["show", "HEAD:pkg/{literal}.py"],
                 ["show", "-e", "HEAD:fo{2,3}"]):
        assert expand(parser().parse_args(args))


def test_revision_set_is_internal_stable_and_deduplicated(repository):
    result = query("show", "{v2,v1,v2}:Base.ping", "--repo", str(repository))
    assert [q["revision"] for q in result["queries"]] == ["v2", "v1"]
    assert [r["source"] for r in result["results"]] == ["v2:src/pkg/base.py", "v1:pkg/base.py"]
    status, out, _ = invoke("show", "{v2,v1}:Base.ping", "--repo", str(repository))
    assert status == 0 and "\n\nv1:pkg/base.py\n" in out


def test_bad_revision_and_absent_symbol_are_independent(repository):
    result = query("show", "{v1,bogus}:Base", "v2:absent", "--repo", str(repository), status=2)
    assert result["results"]
    assert [q["status"] for q in result["queries"]] == ["ok", "error", "no_match"]


def test_positions_expand_files_versions_and_isolate_invalid_lines(repository):
    result = query("where", "{v1,v2}:**/base.py:2", "v1:**/base.py:999", "--repo", str(repository), status=2)
    assert len(result["queries"]) == 3
    assert result["queries"][0]["result_indices"] and result["queries"][1]["result_indices"]
    assert result["queries"][2]["errors"]
    assert query("where", "<stdin>:8")["results"][-1]["symbol"].endswith("Child.run")
    assert invoke("where", "<stdin>:8", "--line", "8")[0] == 2
    assert invoke("complete", "<stdin>:8")[0] == 2


def test_address_colons_are_parsed_from_opposite_ends():
    spec = expand(parser().parse_args(["where", "{v1,v2}:{pkg,src}/**/*.py:42:8"]))
    assert [(s.revision, s.pattern, s.line, s.column) for s in spec] == [
        ("v1", "pkg/**/*.py", 42, 8), ("v1", "src/**/*.py", 42, 8),
        ("v2", "pkg/**/*.py", 42, 8), ("v2", "src/**/*.py", 42, 8),
    ]
    args = parser().parse_args(["path", "-e", r"{v1,v2}:(?:start|begin)$", "-e", r"(?:Client|Worker)\.send$"])
    assert expand(args)[0].destination == r"(?:Client|Worker)\.send$"
    assert invoke("where", "-e", r"v1:.*\.py:42")[0] == 2


def test_diff_union_matches_both_versions_and_tracks_original_coordinates(repository):
    write(repository, "extra.py", "def removed():\n return 1\ndef common():\n return 2\n")
    git(repository, "add", "extra.py")
    git(repository, "commit", "-qm", "before union")
    git(repository, "tag", "before-union")
    write(repository, "extra.py", "def common():\n return 3\ndef added():\n return 4\n")
    git(repository, "add", "extra.py")
    git(repository, "commit", "-qm", "after union")
    result = query("diff", "before-union..HEAD:{removed,common,added}", "--repo", str(repository))
    assert {r["symbol"]: r["status"] for r in result["results"]} == {
        "extra.removed": "deleted",
        "extra.common": "modified",
        "extra.added": "added",
    }
    common = next(r for r in result["results"] if r["symbol"] == "extra.common")
    assert common["before"]["line"] == 3 and common["after"]["line"] == 1
    selected = query(
        "diff", "before-union..HEAD:{removed,common,added}", "--roles", "return", "--repo", str(repository),
    )
    assert len(selected["results"]) == 3
    assert invoke("diff", "{v1,v2}:Base", "--repo", str(repository))[0] == 2


def test_path_cartesian_and_one_native_expansion_per_start(monkeypatch):
    source = (
        "def end_a(): pass\ndef end_b(): pass\ndef middle(): end_a(); end_b()\n"
        "def start_a(): middle()\ndef start_b(): middle()\n"
    )
    counts = Counter()
    original = relations.calls

    def counted(ws, entry, **kwargs):
        counts[entry.name] += 1
        return original(ws, entry, **kwargs)

    monkeypatch.setattr(relations, "calls", counted)
    result = query("path", "start_*", "end_*", source=source)
    paths = result["queries"][0]["selections"]
    assert len(paths) == 4 and all(p["status"] == "found" for p in paths)
    assert all(len(p["result_indices"]) == 2 for p in paths)
    assert max(counts.values()) == 1
    assert len(result["results"]) == 4  # shared edges are stored only once


def test_path_multiple_destinations_preserve_argument_flow():
    source = "def a(): pass\ndef b(): pass\ndef invoke(f): f()\ndef start(): invoke(a); invoke(b)\n"
    result = query("path", "start", "{a,b}", source=source)
    assert [p["status"] for p in result["queries"][0]["selections"]] == ["found", "found"]


def test_overlapping_show_is_deduplicated_with_query_indices():
    result = query("show", "Child", "Child.run")
    assert len(result["results"]) == 1
    assert result["queries"][0]["result_indices"] == result["queries"][1]["result_indices"] == [0]
    assert result["results"][0]["code"].count("def run") == 1
    assert "documents" not in result


def test_no_role_matches_are_distinct_from_no_symbols():
    result = query("show", "start", "--roles", "doc")
    assert result["queries"][0]["status"] == "empty"
    assert result["queries"][0]["matches"] and result["heads"]
    assert result["results"][0]["code"] == "def start(x):"
    assert not result["occurrences"]


def test_names_and_globs_respect_symbol_components():
    source = (
        "class Worker:\n def run(self):\n  def nested(): pass\n  return nested()\n def runner(self): pass\n"
        "class OtherWorker:\n def run(self): pass\n"
    )
    assert query("show", "Worker", source=source)["symbol"]["symbol"] == "__stdin__.Worker"
    assert query("show", "Worker.run", source=source)["symbol"]["symbol"] == "__stdin__.Worker.run"
    single = query("show", "Worker.r?n", source=source)
    assert [r["symbol"] for r in single["results"]] == ["__stdin__.Worker.run"]
    matched = query("show", "Worker.*", source=source)["queries"][0]["matches"]
    assert {m["symbol"] for m in matched} == {"__stdin__.Worker.run", "__stdin__.Worker.runner"}
    deep = query("show", "Worker.**", source=source)["queries"][0]["matches"]
    assert any(m["symbol"] == "__stdin__.Worker.run.nested" for m in deep)
    assert query("show", "Worker.run[!x]er", source=source)["symbol"]["symbol"] == "__stdin__.Worker.runner"
    assert query("show", "^Worker$", source=source, status=1)["queries"][0]["status"] == "no_match"


def test_sets_factor_versions_and_symbols_without_shell_expansion(repository):
    result = query("show", "{v2,v1}:Base.{ping,ping}", "--repo", str(repository))
    assert [(q["revision"], q["pattern"]) for q in result["queries"]] == [("v2", "Base.ping"), ("v1", "Base.ping")]
    partial = query("show", "{v1,v2}:{Base.ping,missing}", "--repo", str(repository))
    assert [q["status"] for q in partial["queries"]] == ["ok", "no_match", "ok", "no_match"]
    assert len(partial["results"]) == 2
    assert query("show", "HEAD^{commit}:Base.ping", "--repo", str(repository))["results"]
    args = parser().parse_args(["show", "{v1,v2}:Client.{send,{open,close}}"])
    assert [s.pattern for s in expand(args)] == ["Client.send", "Client.open", "Client.close"] * 2
    regex = expand(parser().parse_args(["show", "-e", r"{v1,v2}:foo[ab]{1,2}$"]))
    assert [s.pattern for s in regex] == [r"foo[ab]{1,2}$"] * 2


def test_compact_e_and_mixed_path_endpoints_keep_argument_order():
    for args in (["-e^start$", "leaf"], ["--regexp=^start$", "leaf"], ["-ie^START$", "leaf"]):
        result = query("path", *args)
        assert result["status"] == "found"
        assert result["queries"][0]["matching"] == "regex"
        assert result["queries"][0]["destination_matching"] == "glob"
    assert query("path", "start", "-e", "^leaf$")["status"] == "found"


def test_file_globs_are_identical_for_sources_and_positions(repository, tmp_path, monkeypatch):
    root = tmp_path / "local"
    root.mkdir()
    monkeypatch.chdir(root)
    for path in ("pkg/a.py", "pkg/b.py", "pkg/nested/a.py"):
        write(root, path, "def task():\n return 1\n")
    one = query("show", "task", "--files", "pkg/?.py", source="")
    assert {r["source"] for r in one["results"]} == {"pkg/a.py", "pkg/b.py"}
    all_files = query("show", "task", "--files", "pkg/**/*.py", "pkg/a.py", source="")
    assert len(all_files["results"]) == 3
    where = query("where", "pkg/**/*.py:2", "--files", "pkg/", source="")
    assert {r["source"] for r in all_files["results"]} == {r["source"] for r in where["results"]}
    explicit = query("show", "task", "--files", "pkg/{a,b}.py", source="")
    assert len(explicit["results"]) == 2
    assert query("show", "-e", "^task$", "--files", "pkg/?.py", source="")["results"] == one["results"]
    assert query("show", "task", "--files", "pkg/missing*.py", source="", status=2)["error"]
    assert query("show", "task", "--file", "pkg/a.py", source="", status=2)["error"]
    # One recursive glob finds paths in both historical layouts, despite cwd.
    reads = query("show", "{v1,v2}:Base.ping", "--files", "**/base.py", "--repo", str(repository))
    positions = query("where", "{v1,v2}:**/base.py:2", "--repo", str(repository))
    assert {r["source"] for r in reads["results"]} == {r["source"] for r in positions["results"]}


def test_file_scope_is_not_widened_by_known_symbol_hint(repository):
    result = query("show", "v1:Worker.run", "--files", "pkg/base.py", "--repo", str(repository), status=1)
    assert result["queries"][0]["status"] == "no_match"


def test_multi_file_completion_and_invalid_positions_are_independent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = "class Worker:\n def run(self): pass\nw=Worker()\nw.\n"
    write(tmp_path, "a.py", source)
    write(tmp_path, "b.py", source)
    write(tmp_path, "short.py", "pass\n")
    result = query("complete", "*.py:4:2", "--files", "*.py", source="", status=2)
    assert result["queries"][0]["status"] == "partial"
    assert {r["source"] for r in result["results"] if r["label"] == "run"} == {"a.py", "b.py"}
    assert result["queries"][0]["errors"]


def test_public_short_options_and_focused_help(capsys):
    import pytest

    from pyrel.commands import ACTIONS, validate

    args = parser().parse_args(["list", "Worker", "-r", "-k", "method", "-d", "2", "-j", "-f", "a.py", "b.py"])
    validate(args)
    assert args.recursive and args.kinds == ["method"] and args.max_depth == 2 and args.json
    assert args.source == ["a.py", "b.py"]
    args = parser().parse_args(["show", "v1:Worker", "-b", "-R", "call", "return"])
    assert args.blame and args.roles == ["call", "return"]
    for action in ACTIONS:
        with pytest.raises(SystemExit) as caught:
            parser().parse_args([action, "--help"])
        help_text = capsys.readouterr().out
        assert caught.value.code == 0 and f"usage: lsp {action} " in help_text
        assert "{" + ",".join(ACTIONS) + "}" not in help_text
        assert "--files" in help_text and "--file " not in help_text
        if action in {"where", "complete"}:
            assert "--regexp" not in help_text


def test_malformed_position_preserves_other_results():
    result = query("where", "<stdin>:8", "<stdin>:bad", status=2)
    assert result["queries"][0]["result_indices"]
    assert result["queries"][1]["errors"]


def test_inherited_destinations_keep_receiver_identity():
    source = (
        "class A:\n def work(self): pass\nclass B(A): pass\nclass C(A): pass\n"
        "def start(): B().work()\n"
    )
    result = query("path", "start", "{B,C}.work", source=source)
    selections = result["queries"][0]["selections"]
    assert len(selections) == 2
    statuses = {p["destination_receiver"]: p["status"] for p in selections}
    assert statuses["__stdin__.B"] == "found"
    assert statuses["__stdin__.C"] != "found"


def test_inherited_globs_preserve_zero_depth_and_component_spanning_matches():
    source = "class Base:\n def work(self): pass\nclass Child(Base): pass\n"
    for pattern in ("Child.work", "Child.**.work", "Child**", "Ch[.]ild.work", "{Base,Child}.work"):
        result = query("show", pattern, source=source, status=1 if pattern == "Ch[.]ild.work" else 0)
        selected = {m.get("selected_as") for q in result["queries"] for m in q.get("selections", [])}
        if pattern == "Ch[.]ild.work":
            assert not result["queries"][0]["matches"]
        else:
            assert "__stdin__.Child.work" in selected


def test_directory_dot_case_insensitive_files_and_duplicate_globs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "Pkg/task.py", "def work(): pass\n")
    write(tmp_path, "Pkg/sub/task.py", "def work(): pass\n")
    all_files = query("show", "work", "--files", ".", "./Pkg/**/*.py", source="")
    assert len(all_files["results"]) == 2
    insensitive = query("show", "WORK", "-i", "--files", "pkg/TASK.py", source="")
    assert [r["source"] for r in insensitive["results"]] == ["Pkg/task.py"]
    positions = query("where", "pkg/TASK.py:1", "-i", "--files", ".", source="")
    assert {r["source"] for r in positions["results"]} == {"Pkg/task.py"}


def test_overlapping_fragments_preserve_crlf_and_unicode_line_separators():
    from pyrel.batch import merge_source
    from pyrel.evidence import code_record
    from pyrel.model import Source

    source = Source("sample.py", "sample", "x='a\u2028b'\r\ny=2\r\nz=3\r\n")
    rows = merge_source([code_record(source, 1, 2), code_record(source, 2, 3)])
    assert len(rows) == 1
    assert rows[0]["code"] == source.text.removesuffix("\r\n")
    assert rows[0]["end_line"] == 3


def test_revision_jobs_overlap_without_reordering_results():
    from threading import Barrier

    from pyrel.session import Session

    barrier = Barrier(2)
    def job(value):
        def run(_session):
            barrier.wait(timeout=5)
            return value
        return run

    with Session() as session:
        assert session.parallel([("v2", job("v2")), ("v1", job("v1"))]) == ["v2", "v1"]


def test_regex_quantifiers_are_not_interpreted_as_brace_sets():
    source = "def foo(): pass\ndef foooo(): pass\n"
    result = query("show", "foo", "-e", r"^fo{2,}$", source=source)
    assert [q["status"] for q in result["queries"]] == ["ok", "ok"]
    assert len(result["queries"][1]["matches"]) == 2


def test_local_completion_keeps_same_basenames_in_separate_packages(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for package, method in (("a", "first"), ("b", "second")):
        write(tmp_path, f"{package}/task.py", f"class Worker:\n def {method}(self): pass\nw=Worker()\nw.\n")
    result = query("complete", "*/task.py:4:2", "--files", "**/task.py", source="")
    assert {(r["source"], r["label"]) for r in result["results"] if r["label"] in {"first", "second"}} == {
        ("a/task.py", "first"), ("b/task.py", "second"),
    }
    assert {s["module"] for s in result["sources"]} == {"a.task", "b.task"}
