"""Invocation-local code graph. Callable vertices retain binding contexts."""

from __future__ import annotations

import ast
import hashlib
from collections import defaultdict, deque
from dataclasses import dataclass, field

from .model import Value
from .queries import call_edges

SPAN = ("source", "line", "column", "end_line", "end_column")
LABELS = {"Symbol", "Event", "Binding"}
RELATIONS = {"CALLS", "CONTAINS", "DEFINES", "INHERITS", "MRO", "READS", "WRITES", "REFERS_TO"}


def identity(*parts):
    return hashlib.sha256(repr(parts).encode()).hexdigest()


@dataclass(eq=False)
class Node:
    id: str
    label: str
    props: dict
    binding_context: tuple | None = None
    symbol: object = None

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other):
        return isinstance(other, Node) and self.id == other.id


@dataclass(eq=False)
class Edge:
    id: str
    type: str
    start: Node
    end: Node
    props: dict

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other):
        return isinstance(other, Edge) and self.id == other.id


@dataclass
class Walk:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)


@dataclass
class SourceCode:
    """A source projection verified against an original input definition."""

    id: str
    props: dict


@dataclass
class SourceDiff:
    id: str
    props: dict
    before: SourceCode | None = None
    after: SourceCode | None = None


def serialize(value):
    if isinstance(value, (SourceCode, SourceDiff)):
        return {"id": value.id, **value.props}
    if isinstance(value, Node):
        return {"id": value.id, "labels": [value.label], **value.props}
    if isinstance(value, Edge):
        return {"id": value.id, "type": value.type, "from": value.start.id, "to": value.end.id, **value.props}
    if isinstance(value, Walk):
        return {"nodes": [serialize(n) for n in value.nodes], "relationships": [serialize(e) for e in value.edges]}
    if isinstance(value, list):
        return [serialize(v) for v in value]
    if isinstance(value, dict):
        return {k: serialize(v) for k, v in value.items()}
    return value


def evidence(values):
    found = {}

    def add(value):
        if isinstance(value, SourceDiff):
            add(value.before)
            add(value.after)
            return
        if isinstance(value, (Node, Edge, SourceCode)):
            record = {k: value.props[k] for k in SPAN}
            record.update(id=value.id)
            if isinstance(value, Node):
                record.update(kind=value.props.get("kind"), name=value.props.get("name"))
            elif isinstance(value, Edge):
                record.update(kind=value.type)
            else:
                record.update(kind="SOURCE", name=value.props["symbol"])
            found.setdefault(value.id, record)
        elif isinstance(value, Walk):
            for item in [*value.nodes, *value.edges]:
                add(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                add(item)
        elif isinstance(value, dict):
            for item in value.values():
                add(item)
    for value in values:
        add(value)
    return list(found.values())


class CodeGraph:
    """Expand the selected context only; reverse/global queries explicitly scan."""

    def __init__(self, analysis, *, max_work=100000, declarations=None):
        self.analysis = analysis
        self.nodes = {}
        self.edges = {}
        self.outgoing = {}
        self.incoming = {}
        self.expanded = set()
        self.scanned = False
        self.work = 0
        self.max_work = max_work
        self.diagnostics = []
        self.unresolved = {}
        self.children = defaultdict(list)
        self.declarations = declarations or {}
        for symbol in analysis.symbols.values():
            self.children[symbol.parent].append(symbol)

    def tick(self):
        self.work += 1
        if self.work > self.max_work:
            raise QueryLimit(f"query work budget {self.max_work} reached; narrow the pattern or raise --max-work")

    def limited(self, message):
        row = {"kind": "query_limit", "message": message}
        if row not in self.diagnostics:
            self.diagnostics.append(row)

    def symbol(self, name, receiver="", arguments=None, closure=None, attributes=None):
        symbol = self.analysis.symbols[name]
        receiver = receiver or (symbol.owner if symbol.kind != "staticmethod" else "") or ""
        arguments = dict(arguments or {})
        if symbol.owner and symbol.kind != "staticmethod":
            params = [*symbol.node.args.posonlyargs, *symbol.node.args.args]
            if params:
                arguments.setdefault(params[0].arg, Value(
                    "class" if symbol.kind == "classmethod" else "instance", receiver, identity=f"self:{receiver}",
                ))
        context = (name, receiver, tuple(sorted(arguments.items())), tuple(sorted((closure or {}).items())),
                   tuple(sorted((attributes or {}).items())))
        key = identity(symbol.source.identity(), context)
        if key not in self.nodes:
            props = {**symbol.record(), "name": name, "receiver": receiver, **symbol.source.identity(),
                     "root": False, "expanded": False}
            props.pop("symbol", None)
            self.nodes[key] = Node(key, "Symbol", props, context, symbol)
        return self.nodes[key]

    def declaration(self, symbol):
        key = identity(symbol.source.identity(), symbol.name, symbol.record(), "declaration")
        if key not in self.nodes:
            props = {**symbol.record(), **symbol.source.identity(), "name": symbol.name,
                     "receiver": "", "root": False, "expanded": False}
            props.pop("symbol", None)
            self.nodes[key] = Node(key, "Symbol", props, symbol=symbol)
        return self.nodes[key]

    def node(self, label, key, props):
        if "order" in props:
            props = dict(props)
            props["seq"] = props.pop("order")
        key = identity(label, key)
        if key not in self.nodes:
            self.nodes[key] = Node(key, label, props)
        return self.nodes[key]

    def edge(self, kind, start, end, props):
        props = dict(props)
        if "order" in props:
            props["seq"] = props.pop("order")
        if kind == "MRO":
            props["position"] = props.pop("index")
        key = identity(kind, start.id, end.id, props)
        if key not in self.edges:
            result = self.edges[key] = Edge(key, kind, start, end, props)
            self.outgoing.setdefault(start.id, []).append(result)
            self.incoming.setdefault(end.id, []).append(result)

    def expand(self, node):
        if node.id in self.expanded or node.label != "Symbol":
            return
        if node.binding_context is None:
            return  # Source declarations are not invented callable contexts.
        self.tick()
        self.expanded.add(node.id)
        a, symbol = self.analysis, node.symbol
        node.props["expanded"] = True
        for child in self.children[symbol.name]:
            self.edge("DEFINES", node, self.symbol(child.name), child.record())
        for child in self.declarations.get(symbol.name, ()):
            self.edge("DEFINES", node, self.declaration(child), child.record())
        if symbol.kind == "class":
            order = a.mro(symbol.name)
            node.props["mro"] = order
            if order is None:
                self.diagnostics.append({**symbol.record(), "kind": "unresolved_mro",
                                         "message": a.mro_errors[symbol.name]})
            for base, syntax in zip(a.base_bindings(symbol), symbol.node.bases, strict=True):
                if base.target in a.symbols and base.kind == "class":
                    self.edge("INHERITS", node, self.symbol(base.target), symbol.source.span(syntax))
            for index, base in enumerate((order or [])[1:], 1):
                if base in a.symbols:
                    self.edge("MRO", node, self.symbol(base), dict(symbol.source.span(symbol.node), index=index))
        name, receiver, arguments, closure, attributes = node.binding_context
        summary = (a.summary(symbol) if symbol.kind in {"class", "module"} else
                   a.function(name, receiver, dict(arguments), dict(closure), dict(attributes)))
        for record in summary.events:
            props = dict(record)
            if record["kind"] == "ATTR_WRITE":
                target = ast.parse(record["target"], mode="eval").body
                props["attribute"] = target.attr if isinstance(target, ast.Attribute) else None
            event = self.node("Event", (node.id, "event", record["order"]), props)
            self.edge("CONTAINS", node, event, {k: record[k] for k in SPAN})
        for index, ref in enumerate(summary.refs):
            event = self.node("Event", (node.id, "ref", index), dict(ref))
            self.edge("CONTAINS", node, event, {k: ref[k] for k in SPAN})
            # Binding names are scoped identifiers, not evidence that separate
            # runtime objects/allocations are identical across call contexts.
            binding = self.node("Binding", (node.id, ref["binding"]),
                                dict(name=ref["binding"], scope=name, **{k: ref[k] for k in SPAN}))
            if ref["role"] in {"read", "write"}:
                self.edge("READS" if ref["role"] == "read" else "WRITES", node, binding, dict(ref))
            target = (self.symbol(ref["target"]) if ref.get("resolution") == "resolved"
                      and ref.get("target") in a.symbols else binding)
            self.edge("REFERS_TO", event, target, dict(ref))
        # A constructor call does not re-execute the class declaration body.
        # Its bound __init__ continuation is attached at the caller below.
        for record in (() if symbol.kind == "class" else call_edges(summary)):
            if record["resolution"] != "resolved" or record.get("target") not in a.symbols:
                if record["resolution"] == "unresolved":
                    frontier = dict(record, caller=name)
                    frontier["seq"] = frontier.pop("order")
                    self.unresolved.setdefault((node.id, record["order"]), frontier)
                continue
            target = record["target"]
            context = summary.calls.get(record["order"], (target, record.get("receiver", ""), {}, {}, {}))
            # Keep the constructed class visible and retain the constructor's
            # bound context as the continuation of the same source call site.
            if a.symbols[target].kind == "class":
                self.edge("CALLS", node, self.symbol(target), dict(record, caller=name))
                if context[0] == target:
                    continue
            self.edge("CALLS", node, self.symbol(*context), dict(record, caller=name))

    def materialize(self, selections=()):
        """Materialize a declared scope, independently of the Cypher text.

        A selected module/class includes its lexical descendants. Follow calls
        with their bindings; references and definitions remain visible as stubs
        unless selected or called. A stub's expanded=false is queryable.
        """
        from .discovery import DECLARATIONS

        roots = [self.declaration(symbol) if symbol.kind in DECLARATIONS else self.symbol(symbol.name, receiver)
                 for symbol, receiver in selections]
        seeds = list(roots)
        if not selections:
            seeds = [self.symbol(name) for name in self.analysis.symbols]
        for root in roots:
            root.props["root"] = True
            if root.symbol.kind in {"class", "module"}:
                seeds.extend(self.symbol(name) for name in self.analysis.symbols
                             if name.startswith(root.symbol.name + "."))
        pending = deque(seeds)
        try:
            while pending:
                node = pending.popleft()
                if node.id in self.expanded:
                    continue
                self.expand(node)
                pending.extend(edge.end for edge in self.outgoing.get(node.id, ())
                               if edge.type in {"CALLS", "INHERITS", "MRO"}
                               and edge.end.id not in self.expanded)
        except QueryLimit as error:
            self.limited(str(error))
        return roots

    def scan(self):
        if self.scanned:
            return
        self.scanned = True
        for symbol in self.analysis.symbols.values():
            self.expand(self.symbol(symbol.name))
        # Each outgoing occurrence carries its own context. Keep those contexts
        # separate when building a reverse adjacency view.
        pending = list(self.nodes.values())
        index = 0
        while index < len(pending):
            self.expand(pending[index])
            index += 1
            if index == len(pending):
                pending.extend(n for n in self.nodes.values() if n.label == "Symbol" and n.id not in self.expanded)

    def adjacent(self, node, direction, kinds):
        self.expand(node)
        if direction in {"in", "both"}:
            self.scan()
        rows = []
        if direction in {"out", "both"}:
            rows.extend((e, e.end) for e in self.outgoing.get(node.id, ()))
        if direction in {"in", "both"}:
            rows.extend((e, e.start) for e in self.incoming.get(node.id, ()))
        return [(e, n) for e, n in rows if not kinds or e.type in kinds]


class QueryLimit(Exception):
    """An explicit partial result, never an empty proof of absence."""
