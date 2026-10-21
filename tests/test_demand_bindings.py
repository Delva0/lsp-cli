"""Demand-loaded call bindings must match the exhaustive import closure."""

import pytest

from lsp_cli.workspace import Workspace

from .test_actions import query
from .test_git_sources import git, write
from .test_git_sources import repository as repository  # noqa: PLC0414

CASES = [
    {
        "entry.py": "def run():\n from dep import factory\n callback = factory()\n return callback()\n",
        "dep.py": "def target(): return 1\ndef factory(): return target\n",
    },
    {
        "entry.py": "from dep import factory\ndef run():\n callback = factory()\n return callback()\n",
        "dep.py": "def factory():\n from second import factory as next_factory\n return next_factory()\n",
        "second.py": "def factory():\n from third import target\n return target\n",
        "third.py": "def target(): return 1\n",
    },
    {
        "entry.py": "from dep import factory\ndef run():\n obj = factory()\n return obj.target()\n",
        "dep.py": "def factory():\n import pkg.worker\n return pkg.worker.Worker()\n",
        "pkg/__init__.py": "",
        "pkg/worker.py": "class Worker:\n def target(self): return 1\n",
    },
    {
        "entry.py": "from dep import factory\ndef run():\n obj = factory()\n return obj.callback()\n",
        "dep.py": "def factory():\n from pkg.worker import Worker as Alias\n return Alias()\n",
        "pkg/__init__.py": "",
        "pkg/worker.py": ("class Worker:\n def __init__(self):\n"
                          "  from .handlers import target\n  self.callback = target\n"),
        "pkg/handlers.py": "def target(): return 1\n",
    },
    {
        "entry.py": "from dep import factory\ndef run():\n callback = factory()\n return callback()\n",
        "dep.py": "def factory():\n from second import target\n return target\n",
        "second.py": "from third import *\n",
        "third.py": "from second import target\ndef target(): return 1\n__all__ = ['target']\n",
    },
    {
        "entry.py": "from dep import factory\ndef run():\n callback = factory(unknown())\n return callback()\n",
        "dep.py": ("def factory(flag):\n if flag:\n  from left import target\n"
                   " else:\n  from right import target\n return target\n"),
        "left.py": "def target(): return 1\n",
        "right.py": "def target(): return 2\n",
    },
    {
        "entry.py": "from dep import factory\ndef run():\n callback = factory()\n return callback()\n",
        "dep.py": "def factory():\n from unavailable import target\n return target\n",
    },
    {
        "entry.py": "from dep import factory\ncallback = factory()\ndef run(): return callback()\n",
        "dep.py": "def factory():\n from second import target\n return target\n",
        "second.py": "def target(): return 1\n",
    },
    {
        "entry.py": ("from dep import factory\nclass Store:\n callback = factory()\n"
                     "def run():\n obj = Store()\n return obj.callback()\n"),
        "dep.py": "def factory():\n from second import target\n return target\n",
        "second.py": "def target(): return 1\n",
    },
    {
        "entry.py": "from dep import factory\ndef run():\n callback = factory()\n return callback()\n",
        "dep.py": "def factory():\n from second import target\n return target\n",
        "second.py": "def target(): return 1\nfrom third import *\n",
        "third.py": "def target(): return 2\n",
    },
]


@pytest.mark.parametrize("files", CASES)
def test_demand_call_bindings_match_exhaustive_import_closure(repository, monkeypatch, files):
    for path, text in files.items():
        write(repository, path, text)
    # Uncalled imports must not force their dependency trees into this query.
    with (repository / "entry.py").open("a") as out:
        out.write("\ndef unrelated():\n import noise\n return noise.unused()\n")
    write(repository, "noise.py", "from noise_dep import unused\n")
    write(repository, "noise_dep.py", "def unused(): return None\n")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "deferred imports and a separate uncalled dependency")
    args = ("show", "HEAD:entry.run", "--roles", "call", "--repo", str(repository))
    planned = query(*args)
    monkeypatch.setattr(Workspace, "call_analysis", lambda self, selectors, scopes: self.analysis(selectors))
    exhaustive = query(*args)
    assert planned["occurrences"] == exhaustive["occurrences"]
    assert planned["results"] == exhaustive["results"]
    assert planned["heads"] == exhaustive["heads"]
    assert not any("noise" in row["source"] for row in planned["sources"])
    assert any("noise" in row["source"] for row in exhaustive["sources"])


def test_eager_imports_keep_class_bodies_and_defer_nested_functions():
    from lsp_cli.model import Source

    source = Source("input.py", "input", """import first
if flag:
    from conditional import value
class Example:
    import class_body
    def run(self):
        import deferred
def factory():
    class Inner:
        import also_deferred
""")
    assert {node.names[0].name for node in source.eager_imports} == {"first", "value", "class_body"}


def test_compiler_scopes_are_reused_only_for_identical_source_objects(monkeypatch):
    import symtable

    from lsp_cli.lexical import LexicalFacts
    from lsp_cli.model import Source

    compiled = []
    original = symtable.symtable

    def record(*args):
        compiled.append(args[0])
        return original(*args)

    monkeypatch.setattr(symtable, "symtable", record)
    old = Source("input.py", "input", "def f():\n old_name = 1\n")
    new = Source("input.py", "input", "def f():\n new_name = 2\n")
    first = LexicalFacts([old])
    assert LexicalFacts([old]).scopes == first.scopes
    first.scopes.clear()
    assert LexicalFacts([old]).local_names("input.f") == {"old_name"}
    assert LexicalFacts([new]).local_names("input.f") == {"new_name"}
    assert compiled == [old.text, new.text]


@pytest.mark.parametrize(("selector", "entry", "worker", "status"), [
    ("entry.Alias.run", "from factory import create\nAlias = create()\n",
     "class Worker:\n def run(self): return 1\n", 0),
    ("entry.Wrapper.child.run", ("from factory import create\nclass Wrapper:\n"
     " def __init__(self): self.child = create()()\n"),
     "class Worker:\n def run(self): return 1\n", 0),
    ("entry.Alias.run", "from factory import create\nAlias = create()\n",
     "from unavailable import Base\nclass Worker(Base):\n def run(self): return 1\n", 0),
    ("entry.Alias.absent", "from factory import create\nAlias = create()\n",
     "from unavailable import Base\nclass Worker(Base): pass\n", 1),
    ("entry.Alias.run", "from factory import create\nAlias = create()\n",
     "from base import Parent\nclass Worker(Parent): pass\n", 0),
])
def test_member_selection_closes_deferred_imports_before_returning(
    repository, monkeypatch, selector, entry, worker, status,
):
    write(repository, "entry.py", entry)
    write(repository, "factory.py", "def create():\n from worker import Worker\n return Worker\n"
          "def unused():\n import noise\n return noise\n")
    write(repository, "worker.py", worker)
    write(repository, "base.py", "class Parent:\n def run(self): return 1\n")
    write(repository, "noise.py", "def never_called(): return 2\n")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "class aliases from deferred factory imports")
    args = ("show", "HEAD:" + selector, "--repo", str(repository))
    planned = query(*args, status=status)

    def exhaustive_analysis(self, selectors, evaluate, scopes=()):
        analysis = self.analysis(selectors)
        return analysis, evaluate(analysis)

    monkeypatch.setattr(Workspace, "demanded_analysis", exhaustive_analysis)
    exhaustive = query(*args, status=status)
    for key in ("results", "heads", "status"):
        assert planned.get(key) == exhaustive.get(key)


def test_rebuilt_analysis_reuses_lexical_effects_without_sharing_mutable_state():
    from lsp_cli.analysis import Analysis
    from lsp_cli.model import Source

    source = Source("input.py", "input", """def target(): pass
callback = target
def replace():
    global callback
    callback = None
def outer():
    def inner():
        yield target
    return inner
class Worker:
    def __init__(self): self.callback = target
    def replace(self): del self.callback
""")
    first = Analysis([source])
    assert first.generators == {"input.outer.inner"}
    assert first.global_writes["input"] == {"callback"}
    assert first.mutable_fields == {("input.Worker", "callback")}
    first.global_writes["input"].clear()
    first.mutable_fields.clear()
    second = Analysis([source, Source("new.py", "new", "def extra(): pass\n")])
    assert second.generators == {"input.outer.inner"}
    assert second.global_writes["input"] == {"callback"}
    assert second.mutable_fields == {("input.Worker", "callback")}
    changed = Analysis([Source("input.py", "input", "def outer(): return 1\n")])
    assert not changed.generators and not changed.mutable_fields


def test_unresolved_inherited_binding_keeps_its_reason_instead_of_silent_absence():
    from .test_actions import invoke

    source = "from unavailable import External\nclass Base(External):\n def run(self): pass\nclass Child(Base): pass\n"
    result = query("show", "Child.run", source=source, status=1)
    assert not result["results"]
    assert any(d["kind"] == "unresolved" and "MRO" in d["message"] for d in result["diagnostics"])
    status, output, error = invoke("show", "Child.run", source=source)
    assert status == 1 and not output and "unresolved" in error and "MRO" in error


def test_cached_scope_layout_respects_factories_and_global_nonlocal_declarations():
    from lsp_cli.analysis import Analysis
    from lsp_cli.lexical import LexicalFacts
    from lsp_cli.model import Source

    source = Source("input.py", "input", """global_value = 1
def outer():
    captured = 2
    def inner():
        global global_value
        nonlocal captured
        local = 3
        return local + captured + global_value
    return inner
""")

    class ExtraLocal(LexicalFacts):
        def local_names(self, scope):
            return super().local_names(scope) | {"injected"}

    for factory, injected in ((LexicalFacts, False), (ExtraLocal, True), (LexicalFacts, False)):
        analysis = Analysis([source], facts_factory=factory)
        analysis.function("input.outer.inner")
        local, global_, nonlocal_ = analysis.scopes["input.outer.inner"]
        assert ("injected" in local) is injected
        assert "local" in local and "captured" not in local and "global_value" not in local
        assert global_ == {"global_value"} and nonlocal_ == {"captured"}
