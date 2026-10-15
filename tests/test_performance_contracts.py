"""Operation budgets must preserve evidence, input boundaries and native semantics."""

import ast
import subprocess
import sys
from types import SimpleNamespace

import pytest

from pyrel.analysis import Analysis
from pyrel.model import Source, Value
from pyrel.session import Session
from pyrel.symbols import Catalog
from pyrel.workspace import Workspace

from .test_actions import query
from .test_git_sources import git, write
from .test_git_sources import repository as repository  # noqa: PLC0414


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
    from pyrel.relations import type_order

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
    from pyrel.git_sources import GitSources
    from graphub_lsp import lsp_worker

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
        [sys.executable, "-c", ("import sys; from pyrel.commands import main; "
                               "assert 'pyan' not in sys.modules; assert 'asyncio' not in sys.modules")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_call_contexts_reuse_lexical_scopes_without_reusing_callback_bindings(monkeypatch):
    from pyrel.flow import Walker

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
