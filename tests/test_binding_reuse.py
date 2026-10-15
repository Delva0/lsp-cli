"""Reuse immutable bindings while preserving query isolation and diagnostics."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from pyrel.analysis import Analysis
from pyrel.backend import PyanFacts, SiteVisitor
from pyrel.model import Source, State, Value
from pyrel.queries import query


def test_state_forks_share_values_but_not_mutable_query_state():
    value = Value("function", "task", environment=(("captured", Value("literal", "1")),))
    original = State(names={"task": value}, attrs={("object", "task"): value}, context=({"branch": "if"},))
    fork = deepcopy(original)
    assert fork.names["task"] is value and fork.attrs["object", "task"] is value
    fork.names.clear()
    fork.attrs.clear()
    fork.context[0]["branch"] = "else"
    assert original.names == {"task": value}
    assert original.attrs == {("object", "task"): value}
    assert original.context == ({"branch": "if"},)


def test_pyan_keeps_warnings_without_formatting_discarded_debug_logs(monkeypatch):
    class ExpensiveDebug:
        def __str__(self):
            raise AssertionError("Discarded debug logs must not be formatted")

    def visit(_sources, *, logger, **_options):
        logger.debug("%s", ExpensiveDebug())
        logger.warning("unresolved base")
        logger.warning("unresolved base")
        return SimpleNamespace(nodes={}, mro={})

    monkeypatch.setattr(SiteVisitor, "from_sources", visit)
    assert PyanFacts([]).warnings == ["unresolved base"]


@pytest.mark.parametrize("body", [
    "def start():\n alias = imported\n alias()\n",
    "def start(flag):\n alias = imported if flag else replacement\n alias()\n",
    "def start():\n imported()\n imported = replacement\n",
    "def start(value):\n imported()\n match value:\n  case {'x': imported}: imported()\n",
    "def start():\n try:\n  imported()\n except Exception as imported:\n  imported()\n",
    "def start():\n funcs = [lambda: callback() for callback in (imported,)]\n funcs[0]()\n",
    ("def outer(callback):\n def inner(): return callback()\n return inner\n"
    "def start():\n outer(imported)()\n outer(replacement)()\n"),
    ("class A(Parent):\n @classmethod\n def make(cls): return cls()\n"
    " @staticmethod\n def run(): return imported()\n"
    "class B(A):\n def run(self): return super().run()\n"
    "def start(): B.make().run()\n"),
    ("class A(Parent): pass\nclass B(Parent):\n def run(self): return replacement()\n"
    "class C(A, B):\n def run(self): return super().run()\n"
    "def start(): C().run()\n"),
    ("class A(Parent):\n def __getattribute__(self, key): return replacement\n"
    "def start(): A().run()\n"),
    "def start():\n global imported\n imported = replacement\n imported()\n",
    "def start[T](arg: T): return imported()\n",
])
def test_flow_evidence_matches_original_pyan_pipeline(body):
    sources = [
        Source("base.py", "pkg.base", "def target(): pass\nclass Base:\n def run(self): return target()\n"),
        Source("pkg/__init__.py", "pkg", "from .base import Base, target\n", True),
        Source("app.py", "app", "from pkg import Base as Parent, target as imported\n"
               "def replacement(): pass\n" + body),
    ]
    current = Analysis(sources)
    original = Analysis(sources, facts_factory=PyanFacts)

    def evidence(value):
        if isinstance(value, dict):
            return {key: evidence(item) for key, item in value.items() if key not in {"binding_engine", "mro_engine"}}
        if isinstance(value, (list, tuple)):
            return [evidence(item) for item in value]
        return value

    for action, target in [("trace", "app.start"), ("refs", "pkg.base.target")]:
        assert evidence(query(current, action, [target])["results"]) == evidence(
            query(original, action, [target])["results"],
        )
    for name, symbol in current.symbols.items():
        if symbol.kind == "class":
            assert evidence(query(current, "inherits", [name])["results"]) == evidence(
                query(original, "inherits", [name])["results"],
            )


def test_analysis_forks_isolate_nested_evidence_and_parameter_contexts():
    analysis = Analysis([Source("app.py", "app", "def target(): pass\ndef f(cb):\n if cb:\n  cb()\n")])
    baseline = analysis.function("app.f", arguments={"cb": Value("function", "app.target")})
    fork = analysis.fork()
    changed = fork.function("app.f", arguments={"cb": Value("function", "app.target")})
    changed.events[-1]["context"][0]["label"] = "changed"
    changed.calls.clear()
    changed.state.names.clear()
    assert baseline.events[-1]["context"][0]["label"] != "changed"
    assert baseline.calls and baseline.state.names
    assert fork.function("app.f").events[-1]["resolution"] == "unresolved"
