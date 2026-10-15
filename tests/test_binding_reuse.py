"""Reuse immutable bindings while preserving query isolation and diagnostics."""

from copy import deepcopy
from types import SimpleNamespace

from pyrel.backend import PyanFacts, SiteVisitor
from pyrel.model import State, Value


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
