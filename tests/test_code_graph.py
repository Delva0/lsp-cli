"""Code graph facts keep Python binding contexts and original source evidence."""

import pytest

from pyrel.analysis import Analysis
from pyrel.code_graph import SPAN, CodeGraph, QueryLimit, Walk, serialize
from pyrel.model import Source


def graph(text):
    return CodeGraph(Analysis([Source("fixture.py", "fixture", text)]))


def reachable(g, name):
    pending, seen, names = [g.symbol(name)], set(), set()
    while pending:
        node = pending.pop()
        if node.id in seen:
            continue
        seen.add(node.id)
        names.add(node.props["name"])
        pending.extend(n for _, n in g.adjacent(node, "out", {"CALLS"}))
    return names


def test_contexts_do_not_create_a_path_between_unrelated_parameter_calls():
    g = graph("def sink(): pass\ndef other(): pass\ndef relay(fn): fn()\n"
              "def start(): relay(other)\ndef elsewhere(): relay(sink)\n")
    g.scan()
    assert reachable(g, "fixture.start") == {"fixture.start", "fixture.relay", "fixture.other"}
    assert reachable(g, "fixture.elsewhere") == {"fixture.elsewhere", "fixture.relay", "fixture.sink"}
    assert len({n.id for n in g.nodes.values() if n.props.get("name") == "fixture.relay"}) >= 3


def test_mro_dispatch_aliases_writes_and_source_spans():
    g = graph("def target(): pass\nclass Base:\n def ping(self): target()\n"
              "class Child(Base):\n def run(self):\n  alias = super().ping\n  alias()\n"
              "  self.cert_reqs = 0\n")
    assert "fixture.target" in reachable(g, "fixture.Child.run")
    node = g.symbol("fixture.Child")
    assert [n.props["name"] for _, n in g.adjacent(node, "out", {"INHERITS"})] == ["fixture.Base"]
    write = next(n for n in g.nodes.values() if n.props.get("kind") == "ATTR_WRITE")
    assert write.props["attribute"] == "cert_reqs"
    assert write.props["line"] == 8 and write.props["column"] == 2
    assert write.props["target_span"]["end_column"] == 16
    assert all(set(SPAN) <= n.props.keys() for n in g.nodes.values())
    assert all(set(SPAN) <= e.props.keys() for e in g.edges.values())


def test_path_evidence_preserves_occurrence_order_and_parallel_calls():
    g = graph("def target(): pass\ndef run():\n target()\n target()\n")
    start = g.symbol("fixture.run")
    edges = g.adjacent(start, "out", {"CALLS"})
    assert [e.props["line"] for e, _ in edges] == [3, 4]
    edge, end = edges[0]
    record = serialize(Walk([start, end], [edge]))
    assert [n["name"] for n in record["nodes"]] == ["fixture.run", "fixture.target"]
    assert record["relationships"][0]["line"] == 3


def test_unresolved_and_budgets_remain_explicit():
    g = graph("def start(fn): fn()\n")
    assert reachable(g, "fixture.start") == {"fixture.start"}
    assert next(iter(g.unresolved.values()))["resolution"] == "unresolved"
    g = graph("def run(): pass\n")
    g.max_work = 0
    with pytest.raises(QueryLimit, match="budget"):
        g.expand(g.symbol("fixture.run"))


def test_local_class_after_inference_budget_retains_unknown_bases():
    from pyrel.analysis import Summary

    g = graph("class Base: pass\ndef factory():\n class Local(Base): pass\n return Local\n")
    # Exhaust the inference budget before the local class declaration is visited.
    g.analysis.cache.update({(f"context-{i}",): Summary() for i in range(4096)})
    g.materialize([(g.analysis.symbols["fixture"], "")])
    local = g.symbol("fixture.factory.Local")
    assert local.props["mro"] is None
    assert not [e for e in g.outgoing.get(local.id, []) if e.type in {"MRO", "INHERITS"}]
    assert any(d["kind"] == "unresolved_mro" and d["symbol"] == "fixture.factory.Local" for d in g.diagnostics)
    bases = g.analysis.base_bindings(local.symbol)
    assert len(bases) == 1 and bases[0].kind == "unknown"
