"""Operation budgets must preserve evidence, input boundaries and native semantics."""

import ast
import subprocess
import sys
from types import SimpleNamespace

import pytest

from lsp_cli.analysis import Analysis
from lsp_cli.model import Source, Value
from lsp_cli.session import Session
from lsp_cli.symbols import Catalog
from lsp_cli.workspace import Workspace

from .test_actions import query
from .test_git_sources import git, write
from .test_git_sources import repository as repository  # noqa: PLC0414


def test_context_lookup_walks_each_scope_once_and_returns_isolated_records(monkeypatch):
    from lsp_cli.lsp_queries import contexts

    source = Source("sample.py", "sample", (
        "async def work(flag):\n"
        " try:\n"
        "  if flag:\n   async with resource():\n    await ready()\n"
        "  else:\n   for item in values():\n    consume(item)\n"
        " except ValueError:\n  recover()\n"
        " finally:\n  cleanup()\n"
    ))
    root = source.tree.body[0]
    assert source.syntax == tuple(ast.walk(source.tree))
    assert all(children == tuple(ast.iter_child_nodes(node)) for node, children in source.tree_index[1].items())
    calls = {ast.unparse(n.func): n for n in ast.walk(root) if isinstance(n, ast.Call)}
    expected = {"ready": ["try", "if", "async_with"], "consume": ["try", "else", "loop"],
                "recover": ["except"], "cleanup": ["finally"]}
    for name, kinds in expected.items():
        assert [row["kind"] for row in contexts(source, root, calls[name])] == kinds
    monkeypatch.setattr(ast, "iter_fields", lambda _node: pytest.fail("cached scope was scanned again"))
    rows = contexts(source, root, calls["ready"])
    rows[0]["kind"] = "changed"
    assert [row["kind"] for row in contexts(source, root, calls["ready"])] == expected["ready"]


def test_import_dependency_order_preserves_fixed_point_and_reduces_module_visits(monkeypatch):
    sources = [Source(f"mod{i}.py", f"mod{i}", f"from mod{i + 1} import target\n") for i in range(16)]
    sources.append(Source("mod16.py", "mod16", "def target(): pass\n"))
    count = []
    original = Analysis.walk

    def record(self, symbol, *args, **kwargs):
        if symbol.kind == "module":
            count.append(symbol.name)
        return original(self, symbol, *args, **kwargs)

    monkeypatch.setattr(Analysis, "walk", record)
    scheduled = Analysis(sources)
    assert len(count) <= len(sources) * 2
    monkeypatch.setattr(Analysis, "initialization_sources", lambda self: self.sources)
    old = Analysis(sources)
    assert {name: state.names for name, state in scheduled.globals.items()} == {
        name: state.names for name, state in old.globals.items()
    }


def test_interleaved_single_source_queries_do_not_rebuild_catalog_indexes(monkeypatch):
    sources = [Source(f"m{i}.py", f"m{i}", "class C:\n def f(self): return 1\n") for i in range(20)]
    indexed = []
    original = Catalog._index

    def record(self):
        indexed.extend(source.name for source in self.sources)
        original(self)

    monkeypatch.setattr(Catalog, "_index", record)
    with Session() as session:
        for _ in range(3):
            for source in sources:
                assert session.catalog([source]).select(source.module + ".C.f")
    assert sorted(indexed) == sorted(source.name for source in sources)


def test_refs_batch_caller_binding_analysis_without_losing_contexts(repository, monkeypatch):
    write(repository, "target.py", "class C:\n def run(self): return 1\n"
          "def factory() -> C: return C()\n")
    for index in range(4):
        write(repository, f"caller{index}.py", "from target import factory\n"
              "def start():\n obj = factory()\n missing()\n return obj.run()\n")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "multiple caller closures")
    builds = []
    original = Analysis.__init__

    def record(self, sources, **kwargs):
        builds.append({source.module for source in sources})
        original(self, sources, **kwargs)

    monkeypatch.setattr(Analysis, "__init__", record)
    result = query("refs", "HEAD:target.C.run", "--roles", "call", "--repo", str(repository))
    assert len(builds) == 1
    assert {f"caller{i}" for i in range(4)} <= builds[0]
    assert {row["source"] for row in result["occurrences"]} == {f"HEAD:caller{i}.py" for i in range(4)}
    assert all(row["resolution"] == "resolved" and row["target"] == "target.C.run"
               for row in result["occurrences"])


def test_growing_catalog_tokenizes_each_source_once(monkeypatch):
    sources = [Source(f"m{i}.py", f"m{i}", f"def f(value):\n result = value + {i}\n return result\n")
               for i in range(5)]
    seen = []
    original = Catalog.__init__

    def record(self, inputs):
        seen.extend(id(source) for source in inputs)
        original(self, inputs)

    with Session() as session, monkeypatch.context() as patch:
        patch.setattr(Catalog, "__init__", record)
        for end in range(1, 6):
            combined = session.catalog(sources[:end])
        assert seen == [id(source) for source in sources]
    exhaustive = Catalog(sources)
    assert [entry.record() for entry in combined.entries] == [entry.record() for entry in exhaustive.entries]
    for target in ("f", "m2.f", "result", "m3.py", "value", "absent"):
        assert [e.record() for e in combined.select(target)] == [e.record() for e in exhaustive.select(target)]
    assert [(r["source"].name, r["node"].lineno, r["entry"].name if r["entry"] else None)
            for r in combined.occurrences] == [
        (r["source"].name, r["node"].lineno, r["entry"].name if r["entry"] else None)
        for r in exhaustive.occurrences
    ]


def test_position_index_matches_full_ast_walk_including_unicode_decorators_and_boundaries():
    text = """@decorate("é")
class Café:
    @wrap(lambda x: x)
    def run(self, 名: str = "😀"):
        values = [fn(x) for x in 名 if test(x)]
        if values:
            return self.run(values[0])
        return None
""".replace("\n", "\r\n")
    source = Source("unicode.py", "unicode", text)
    for line, content in enumerate(source.lines, 1):
        for column in range(len(content) + 1):
            point = line, column
            expected = []
            for node in ast.walk(source.tree):
                if hasattr(node, "lineno"):
                    span = source.span(node)
                    if (span["line"], span["column"]) <= point < (span["end_line"], span["end_column"]):
                        expected.append(node)
            assert source.nodes_at(point) == expected, point


@pytest.mark.parametrize("selector", ["Absent.member", "Store.not_present"])
def test_impossible_binding_does_not_start_flow(selector, monkeypatch):
    def fail(*_args):
        pytest.fail("No declaration prefix or binding key can establish this member")
    monkeypatch.setattr(Analysis, "__init__", fail)
    assert not query("show", selector, source="class Store:\n def run(self): return 1\n", status=1)["results"]


@pytest.mark.parametrize("selector", [
    "Alias.run", "instance.run", "product.run", "Container.renamed", "container.callback", "Missing.member",
])
def test_flow_preflight_matches_exhaustive_binding(selector, monkeypatch):
    source = """def leaf(): return 1
class Store:
    def run(self): return leaf()
Alias = Store
instance = Store()
def factory(): return Store()
product = factory()
class Container:
    renamed = leaf
    def __init__(self):
        self.callback = leaf
container = Container()
"""
    status = 1 if selector == "Missing.member" else 0
    planned = query("show", selector, source=source, status=status)
    monkeypatch.setattr(Workspace, "flow_candidate", lambda *_args: True)
    exhaustive = query("show", selector, source=source, status=status)
    for key in ("results", "heads", "status"):
        assert planned.get(key) == exhaustive.get(key)


def test_missing_module_member_does_not_analyze_unrelated_same_named_bindings(repository, monkeypatch):
    write(repository, "src/pkg/empty.py", "from .other import Worker\n")
    write(repository, "src/pkg/other.py", "class Worker:\n def run(self): return 1\n"
          "worker_base = Worker\n")
    git(repository, "add", "src/pkg/empty.py", "src/pkg/other.py")
    git(repository, "commit", "-qm", "a missing module member with an unrelated namesake")
    args = ("show", "HEAD:pkg.empty.worker_base.Worker.run", "--repo", str(repository))
    with monkeypatch.context() as patch:
        patch.setattr(Workspace, "flow_candidate", lambda *_args: True)
        exhaustive = query(*args, status=1)

    def fail(*_args):
        pytest.fail("An absent module member cannot be supplied by unrelated declarations")

    monkeypatch.setattr(Analysis, "__init__", fail)
    planned = query(*args, status=1)
    for key in ("results", "heads", "status"):
        assert planned.get(key) == exhaustive.get(key)


def test_binding_selection_keeps_import_evidence_without_enumerating_unrelated_declarations():
    from lsp_cli.discovery import Index

    sources = [
        Source("entry.py", "entry", "from worker import Worker as Alias\n"),
        Source("worker.py", "worker", "class Worker:\n def run(self): return 1\n"),
        Source("noise.py", "noise", "from worker import Worker\nunused = Worker\n"),
    ]
    index = Index(Analysis(sources))
    assert "declarations" not in sources[2].__dict__
    matches = index.select("entry.Alias.run")
    assert [symbol.name for symbol, _ in matches] == ["worker.Worker.run"]
    assert {record["name"] for record in index.resolutions["entry.Alias.run"]["evidence"]} >= {
        "entry.Alias", "worker.Worker",
    }
    assert "declarations" not in sources[2].__dict__


@pytest.mark.parametrize("selector", [
    "pkg.start.Alias", "Alias", "outer.saved", "pkg.start.outer.saved", "saved", "run", "absent",
])
def test_deferred_declaration_matching_preserves_full_enumeration(selector):
    from lsp_cli.discovery import Index, matching

    sources = [
        Source("start.py", "pkg.start", "from pkg.worker import Worker as Alias\n"
               "def outer():\n saved = Alias\n def inner():\n  nonlocal saved\n  saved = Alias\n return saved\n"),
        Source("worker.py", "pkg.worker", "class Worker:\n def run(self): pass\nAlias = Worker\n"),
        Source("noise.py", "noise", "Alias = 3\n"),
    ]
    index = Index(Analysis(sources))
    selected = matching(index.definitions, selector)
    exhaustive = matching(list(index.definitions), selector)
    def identity(symbol):
        return symbol.name, symbol.kind, symbol.source.name, id(symbol.node)

    assert list(map(identity, selected)) == list(map(identity, exhaustive))


@pytest.mark.parametrize(("binding", "status"), [
    ("from .other import Worker as Alias\n", 0),
    ("from .other import *\n", 0),
    ("from .other import Worker\nAlias = Worker\n", 0),
    # The flow engine conservatively keeps a globally mutated binding unknown.
    ("from .other import Worker\ndef install():\n global Alias\n Alias = Worker\ninstall()\n", 1),
])
def test_module_binding_preflight_keeps_aliases_star_imports_and_global_writes(
    repository, monkeypatch, binding, status,
):
    write(repository, "src/pkg/entry.py", binding)
    write(repository, "src/pkg/other.py", "class Worker:\n def run(self): return 1\nAlias = Worker\n")
    git(repository, "add", "src/pkg/entry.py", "src/pkg/other.py")
    git(repository, "commit", "-qm", "valid dynamic module binding")
    args = ("show", "HEAD:pkg.entry.Alias.run", "--repo", str(repository))
    planned = query(*args, status=status)
    monkeypatch.setattr(Workspace, "flow_candidate", lambda *_args: True)
    exhaustive = query(*args, status=status)
    for key in ("results", "heads", "status"):
        assert planned.get(key) == exhaustive.get(key)


def test_missing_member_of_known_mro_does_not_run_flow_for_other_classes(monkeypatch):
    source = ("class WorkerBase: pass\nclass Other:\n def get_model(self): return 1\n"
              "def unrelated(obj): return getattr(obj, 'get_model', None)\n")
    with monkeypatch.context() as patch:
        patch.setattr(Workspace, "flow_candidate", lambda *_args: True)
        exhaustive = query("show", "WorkerBase.get_model", source=source, status=1)

    def fail(*_args):
        pytest.fail("Known MRO and absence of receiver writes already exclude this member")

    monkeypatch.setattr(Analysis, "__init__", fail)
    planned = query("show", "WorkerBase.get_model", source=source, status=1)
    for key in ("results", "heads", "status"):
        assert planned.get(key) == exhaustive.get(key)


@pytest.mark.parametrize("assignment", [
    "WorkerBase.callback = target\n",
    "key = 'callback'\nsetattr(WorkerBase, key, target)\n",
    "def install(cls):\n setattr(cls, 'callback', target)\ninstall(WorkerBase)\n",
    "getattr = setattr\ngetattr(WorkerBase, 'callback', target)\n",
    "def key(): return 'callback'\nsetattr(WorkerBase, key(), target)\n",
])
def test_known_mro_missing_declaration_keeps_receiver_writes(monkeypatch, assignment):
    source = "def target(): return 1\nclass WorkerBase: pass\n" + assignment
    planned = query("show", "WorkerBase.callback", source=source)
    monkeypatch.setattr(Workspace, "flow_candidate", lambda *_args: True)
    exhaustive = query("show", "WorkerBase.callback", source=source)
    for key in ("results", "heads", "status"):
        assert planned.get(key) == exhaustive.get(key)


def test_ambiguous_member_receiver_does_not_start_flow(repository, monkeypatch):
    write(repository, "left.py", "class Worker: pass\n")
    write(repository, "right.py", "class Worker: pass\ndef callback(): pass\n")
    git(repository, "add", "left.py", "right.py")
    git(repository, "commit", "-qm", "two possible receivers")

    def fail(*_args):
        pytest.fail("Receiver ambiguity does not require evaluating module bodies")

    monkeypatch.setattr(Analysis, "__init__", fail)
    result = query("show", "HEAD:Worker.callback", "--repo", str(repository), status=2)
    assert result["error"]["status"] == "ambiguous"
    assert {item["symbol"] for item in result["error"]["candidates"]} >= {"left.Worker", "right.Worker"}


def test_missing_member_does_not_expand_the_next_batch_catalog(repository, monkeypatch):
    write(repository, "src/pkg/worker.py", "from . import extra\nclass Worker:\n def run(self): return 1\n")
    write(repository, "src/pkg/extra.py", "import pkg\nfrom . import idle\ndef missing(): pass\n")
    write(repository, "src/pkg/idle.py", "import pkg\ndef unrelated(): return 2\n")
    git(repository, "add", "src/pkg/worker.py", "src/pkg/extra.py", "src/pkg/idle.py")
    git(repository, "commit", "-qm", "a flow dependency unrelated to the requested declarations")
    cataloged = set()
    original = Catalog.__init__

    def record(self, sources):
        sources = list(sources)
        cataloged.update(source.module for source in sources)
        original(self, sources)

    monkeypatch.setattr(Catalog, "__init__", record)
    result = query("show", "HEAD:pkg.worker.Worker.missing", "HEAD:pkg.worker.Worker.run",
                   "--repo", str(repository))
    assert result["queries"][0]["status"] == "no_match"
    assert result["queries"][1]["status"] == "ok"
    # extra supplies a legitimate alternative for the missing leaf. idle has
    # no matching declarations, even though it imports the same package root.
    assert "pkg.idle" not in cataloged


def test_readonly_class_and_safe_constructor_prove_absence_without_dependency_closure(repository, monkeypatch):
    write(repository, "src/pkg/unique.py", "from . import worker\nclass Unique:\n"
          " def __init__(self, config): self.config = config\n")
    write(repository, "annotations.py", "from pkg.unique import Unique\n"
          "def annotate(item: Unique) -> Unique: pass\nclass Derived(Unique): pass\n")
    git(repository, "add", "src/pkg/unique.py", "annotations.py")
    git(repository, "commit", "-qm", "only type and base references to a static class")

    def fail(*_args, **_kwargs):
        pytest.fail("The complete reference proof already excludes a concrete member binding")

    monkeypatch.setattr(Workspace, "analysis_sources", fail)
    result = query("show", "HEAD:Unique.absent", "--repo", str(repository), status=1)
    proof = result["coverage"]["negative_member_proofs"][0]
    assert proof["member"] == "absent"
    assert {source["module"] for source in proof["sources"]} == {"pkg.unique", "annotations"}


@pytest.mark.parametrize("source", [
    "def target(): return 1\ndef install(c): c.callback = target\nclass Unique: pass\ninstall(Unique)\n",
    ("def target(): return 1\ndef install(c): c.callback = target\n"
     "class Unique:\n def __init__(self): install(self)\n"),
])
def test_absence_proof_preserves_class_and_constructor_escape(source, monkeypatch):
    from lsp_cli import member_lookup

    planned = query("show", "Unique.callback", source=source)
    monkeypatch.setattr(member_lookup, "absent_member", lambda *_args: False)
    exhaustive = query("show", "Unique.callback", source=source)
    for key in ("results", "heads", "status"):
        assert planned.get(key) == exhaustive.get(key)


def test_absence_proof_does_not_ignore_star_rebinding_of_a_class(repository, monkeypatch):
    from lsp_cli import member_lookup

    write(repository, "src/pkg/start.py", "class Unique: pass\nfrom .overwrite import *\n")
    write(repository, "src/pkg/overwrite.py", "class Unique:\n def callback(self): return 'rebound'\n")
    git(repository, "add", "src/pkg/start.py", "src/pkg/overwrite.py")
    git(repository, "commit", "-qm", "a star import replaces the earlier class")
    args = ("show", "HEAD:pkg.start.Unique.callback", "--repo", str(repository))
    planned = query(*args)
    monkeypatch.setattr(member_lookup, "absent_member", lambda *_args: False)
    exhaustive = query(*args)
    for key in ("results", "heads", "status"):
        assert planned.get(key) == exhaustive.get(key)


def test_external_base_without_own_member_stays_unresolved_without_flow(monkeypatch):
    source = ("from unavailable import External\nclass Worker(External):\n"
              " def __init__(self, config):\n  super().__init__(config)\n  self.config = config\n")

    def fail(*_args):
        pytest.fail("The full input cannot establish a definition in an unavailable external base")

    monkeypatch.setattr(Analysis, "__init__", fail)
    result = query("show", "Worker.callback", source=source, status=1)
    assert result["status"] == "no_match"
    assert any(row["kind"] == "unresolved" and "External" in row["message"] for row in result["diagnostics"])
    assert result["coverage"]["unresolved_member_proofs"][0]["external_module"] == "unavailable"


@pytest.mark.parametrize("definition", [
    "class Worker(External):\n def __init__(self): self.callback = target\n",
    ("def install(obj): obj.callback = target\n"
     "class Worker(External):\n def __init__(self): install(self)\n"),
    "class Worker(External): pass\nWorker.callback = target\n",
])
def test_external_base_preflight_preserves_resolvable_own_bindings(definition, monkeypatch):
    from lsp_cli import member_lookup

    source = "from unavailable import External\ndef target(): return 1\n" + definition
    planned = query("show", "Worker.callback", source=source)
    monkeypatch.setattr(member_lookup, "unresolved_member", lambda *_args: None)
    exhaustive = query("show", "Worker.callback", source=source)
    for key in ("results", "heads", "status"):
        assert planned.get(key) == exhaustive.get(key)


def test_external_base_preflight_does_not_hide_a_rebound_base(monkeypatch):
    from lsp_cli import member_lookup

    source = ("from unavailable import External\nclass Known:\n def callback(self): return 1\n"
              "External = Known\nclass Worker(External): pass\n")
    planned = query("show", "Worker.callback", source=source)
    monkeypatch.setattr(member_lookup, "unresolved_member", lambda *_args: None)
    exhaustive = query("show", "Worker.callback", source=source)
    for key in ("results", "heads", "status"):
        assert planned.get(key) == exhaustive.get(key)


@pytest.mark.parametrize("definition", [
    "from .base import Root\nclass Child(Root): pass\n",
    "from .base import Root as Alias\nclass Child(Alias): pass\n",
    "from .base import Root\nAlias = Root\nclass Child(Alias): pass\n",
    "from .base import Root\nclass Left(Root): pass\nclass Right(Root): pass\nclass Child(Left, Right): pass\n",
    "from .base import Root\nclass Child(Root):\n def needle(self): return 2\nclass Grandchild(Child): pass\n",
])
@pytest.mark.parametrize("pattern", ["needle", "nee*", "n?ed*"])
def test_bare_member_uses_ty_descendants_without_cataloging_unrelated_files(repository, monkeypatch, definition, pattern):
    from lsp_cli import planning

    write(repository, "src/pkg/base.py", "class Root:\n def needle(self): return 1\n")
    write(repository, "src/pkg/child.py", definition)
    for number in range(12):
        write(repository, f"noise/other{number}.py", f"class Other{number}: pass\n")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "inherited member outside its declaration file")
    args = ("show", "HEAD:" + pattern, "--repo", str(repository))
    with monkeypatch.context() as patch:
        patch.setattr(Workspace, "all", lambda *_args: pytest.fail("unrelated declarations are not descendants"))
        planned = query(*args)
    monkeypatch.setattr(planning, "descendant_candidates", lambda *_args: None)
    exhaustive = query(*args)
    for key in ("results", "heads", "status"):
        assert planned.get(key) == exhaustive.get(key)
    selected = {record.get("selected_as") for group in planned["queries"] for record in group["selections"]}
    # Assignment aliases require the existing flow fallback for a qualified
    # address; ty's declaration-only MRO does not project them into bare names.
    if "Alias = Root" in definition:
        direct = query("show", "HEAD:pkg.child.Child.needle", "--repo", str(repository))
        assert any("def needle" in row.get("code", "") for row in direct["results"])
    else:
        assert "pkg.child.Child.needle" in selected
    assert (planned["queries"][0]["coverage"][0]["analyzed_files"]
            < exhaustive["queries"][0]["coverage"][0]["analyzed_files"])


def test_path_sets_plan_the_union_without_analyzing_unrelated_modules(repository, monkeypatch):
    for i in range(60):
        write(repository, f"noise/n{i}.py", f"class Noise{i}:\n def other(self): return 1\n")
    git(repository, "add", "noise")
    git(repository, "commit", "-qm", "unrelated modules")
    analyzed = []
    original = Analysis.__init__

    def record(self, sources):
        analyzed.extend(source.module for source in sources)
        original(self, sources)

    monkeypatch.setattr(Analysis, "__init__", record)
    result = query("path", "HEAD:{start,Worker.run}", "{Base.ping,Worker.run}", "--repo", str(repository))
    assert result["results"]
    assert not any(name.startswith("noise.") for name in analyzed)
    assert not any("noise/" in source["source"] for source in result["sources"])


def test_flow_uses_definition_dependencies_not_name_search_candidates(repository, monkeypatch):
    write(repository, "consumer.py", "from pkg.worker import Worker\ndef unused(): return Worker()\n")
    git(repository, "add", "consumer.py")
    git(repository, "commit", "-qm", "an unrelated lexical hit")
    original = Analysis.__init__
    analyzed = []

    def record(self, sources):
        analyzed.extend(source.module for source in sources)
        original(self, sources)

    monkeypatch.setattr(Analysis, "__init__", record)
    result = query("path", "HEAD:Worker.run", "Base.ping", "--repo", str(repository))
    assert result["results"]
    assert "consumer" not in analyzed


@pytest.mark.parametrize(("selector", "expected"), [
    ("Worker.callback", {"pkg.worker", "pkg.base"}),
    ("consumer.Worker.callback", {"consumer", "pkg.worker", "pkg.base"}),
])
def test_binding_prefix_plans_the_same_receiver_as_flow(repository, selector, expected, monkeypatch):
    write(repository, "src/pkg/worker.py", "from .base import Base\nclass Worker(Base):\n"
          "    def run(self): return 1\nclass Other:\n    def callback(self): return 2\n")
    write(repository, "consumer.py", "from pkg.worker import Worker\n")
    git(repository, "add", "src/pkg/worker.py", "consumer.py")
    git(repository, "commit", "-qm", "imported receiver and an unrelated same-name member")
    analyzed = set()
    original = Analysis.__init__

    def record(self, sources):
        analyzed.update(s.module for s in sources)
        original(self, sources)

    monkeypatch.setattr(Analysis, "__init__", record)
    monkeypatch.setattr(Workspace, "flow_candidate", lambda *_args: True)
    # A class query must not select another class's same-named member.
    # Keep the flow result while excluding suffix-matched import consumers.
    planned = query("show", "HEAD:" + selector, "--repo", str(repository), status=1)
    assert expected <= analyzed
    if selector == "Worker.callback":
        assert "consumer" not in analyzed
    monkeypatch.setattr(Workspace, "binding_prefix", lambda self, name: self.catalog.select(name))
    exhaustive = query("show", "HEAD:" + selector, "--repo", str(repository), status=1)
    for key in ("results", "heads", "status"):
        assert planned.get(key) == exhaustive.get(key)


def test_unresolved_mro_reuses_proof_until_analysis_inputs_change():
    from lsp_cli.relations import type_order

    source = Source("m.py", "m", "class Child(Base): pass\n")
    ws = SimpleNamespace(sources=[source], catalog=Catalog([source]))
    probes = []

    def definition(_source, _span):
        probes.append(True)
        return ws.catalog.select("Base")

    ws.definition = definition
    child = ws.catalog.select("Child")[0]
    for _ in range(3):
        with pytest.raises(ValueError, match="Base Base is unresolved"):
            type_order(ws, child)
    assert len(probes) == 1
    ws.sources.append(Source("base.py", "base", "class Base: pass\n"))
    ws.catalog = Catalog(ws.sources)
    order, _ = type_order(ws, child)
    assert [entry.name for entry in order] == ["m.Child", "base.Base", "builtins.object"]
    assert len(probes) == 2


@pytest.mark.parametrize("target", ["Child.run", "Child.explicit", "start"])
def test_unrelated_magic_methods_do_not_force_native_calls_into_flow(target, monkeypatch):
    source = """class Unrelated:
    def __getattr__(self, name): return None
class Base:
    def run(self): return 1
class Child(Base):
    def run(self): return super().run()
    def explicit(self): return super(Child, self).run()
def start():
    item = Child()
    return item.run()
"""
    def fail(*_args):
        pytest.fail("A proven lexical receiver has no dynamic attributes in its MRO")
    monkeypatch.setattr(Analysis, "__init__", fail)
    result = query("show", target, "--roles", "call", source=source)
    assert result["occurrences"]
    assert all(row["engine"] == "ty-lsp" for row in result["occurrences"])


@pytest.mark.parametrize("body", [
    "@staticmethod\n    def run(self): return self.target()",
    "def run(self):\n        self = unknown()\n        return self.target()",
    "def run(self):\n        setattr(self, 'target', unknown())\n        return self.target()",
])
def test_dynamic_or_unbound_receivers_keep_flow_validation(body, monkeypatch):
    source = ("class Unrelated:\n    def __getattr__(self, name): return None\n"
              "class Example:\n    def target(self): return 1\n    " + body + "\n")
    called = []
    original = Analysis.__init__
    def record(self, sources):
        called.append(True)
        original(self, sources)
    monkeypatch.setattr(Analysis, "__init__", record)
    result = query("show", "Example.run", "--roles", "call", source=source)
    assert called
    row, = [r for r in result["occurrences"] if r.get("callee") == "self.target"]
    assert row["resolution"] == "unresolved"


def test_validated_native_requests_skip_git_but_public_refs_still_move(repository, monkeypatch):
    from lsp_cli.git_sources import GitSources
    from lsp_cli.lsp import worker as lsp_worker

    loader = GitSources(repository, "v1")
    invoked = []
    original = lsp_worker.command

    async def record(*argv):
        invoked.append(argv)
        return await original(*argv)

    monkeypatch.setattr(lsp_worker, "command", record)
    git(repository, "update-ref", "refs/tags/moving", git(repository, "rev-parse", "v1"))
    with Session() as session:
        first = session.native(loader, "initialize", {})
        invoked.clear()
        assert session.native(loader, "initialize", {})["commit"] == first["commit"]
        assert not invoked
        address = {"repo": str(repository), "ref": "moving"}
        api = {"method": "initialize", "params": {}}
        old = session.runner.run(session.lsp_engine.query(address, api))
        git(repository, "update-ref", "refs/tags/moving", git(repository, "rev-parse", "v2"))
        new = session.runner.run(session.lsp_engine.query(address, api))
        assert old["commit"]["oid"] == loader.commit
        assert new["commit"]["oid"] == git(repository, "rev-parse", "v2")
        assert any("rev-parse" in argv for argv in invoked)


@pytest.mark.parametrize("effect", [
    "def __init__(self): mutate(self)",
    "def __init__(self): self.setup()\n    def setup(self): mutate(self)",
])
def test_constructor_escape_keeps_the_flow_fallback(effect):
    source = ("class Noise:\n    def __getattr__(self, name): return None\n"
              "class Example:\n    " + effect + "\n    def target(self): return 1\n"
              "def start():\n    obj = Example()\n    return obj.target()\n")
    result = query("show", "start", "--roles", "call", source=source)
    row, = [r for r in result["occurrences"] if r.get("callee") == "obj.target"]
    assert row["resolution"] == "unresolved"
    assert row["reason"] == "receiver attributes may have changed in a call"


def test_resolved_helper_can_still_mutate_the_method_receiver():
    source = """class Noise:
    def __getattr__(self, name): return None
def helper(obj): mutate(obj)
class Example:
    def target(self): return 1
    def start(self):
        helper(self)
        return self.target()
"""
    result = query("show", "Example.start", "--roles", "call", source=source)
    row, = [r for r in result["occurrences"] if r.get("callee") == "self.target"]
    assert row["resolution"] == "unresolved"


@pytest.mark.parametrize("expression", ["super()", "super(Child, self)"])
def test_super_constructor_effects_cannot_be_skipped(expression):
    source = """class Noise:
    def __getattr__(self, name): return None
class Parent:
    def __init__(self): mutate(self)
class Child(Parent):
    def __init__(self): super().__init__()
    def target(self): return 1
def start():
    obj = Child()
    return obj.target()
"""
    result = query("show", "start", "--roles", "call", source=source.replace("super()", expression))
    row, = [r for r in result["occurrences"] if r.get("callee") == "obj.target"]
    assert row["resolution"] == "unresolved"


def test_loop_effects_after_a_call_keep_flow_validation(monkeypatch):
    source = """class Noise:
    def __getattr__(self, name): return None
class Example:
    def target(self): return 1
def start():
    obj = Example()
    for _ in range(2):
        obj.target()
        mutate(obj)
"""
    built = []
    original = Analysis.__init__

    def record(self, sources):
        built.append(True)
        original(self, sources)

    monkeypatch.setattr(Analysis, "__init__", record)
    query("show", "start", "--roles", "call", source=source)
    assert built


def test_cli_frontend_does_not_import_unused_semantic_engines():
    result = subprocess.run(
        [sys.executable, "-c", ("import sys; from lsp_cli.commands import main; "
                               "assert 'pyan' not in sys.modules; assert 'asyncio' not in sys.modules")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_call_contexts_reuse_lexical_scopes_without_reusing_callback_bindings(monkeypatch):
    from lsp_cli.flow import Walker

    visited = []
    original = Walker._scopes

    def scopes(self, node, root=False):
        if root:
            visited.append(id(node))
        return original(self, node, root)

    monkeypatch.setattr(Walker, "_scopes", scopes)
    analysis = Analysis([Source("m.py", "m", "def first(): pass\ndef second(): pass\n"
                               "def execute(task):\n    alias = task\n    return alias()\n")])
    for target in ("m.first", "m.second", "m.first"):
        fork = analysis.fork()
        result = fork.function("m.execute", arguments={"task": Value("function", target)})
        assert next(e for e in result.events if e["kind"] == "CALL")["target"] == target
    assert len(visited) == len(set(visited))
