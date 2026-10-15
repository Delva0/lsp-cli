"""Lexical symbols, cross-input bindings, strict MRO and call summaries."""

from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import dataclass, field

from .model import UNKNOWN, Source, State, Symbol, Value, merge, unknown

FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
DEFINITIONS = (*FUNCTIONS, ast.ClassDef)


@dataclass
class Summary:
    events: list[dict] = field(default_factory=list)
    refs: list[dict] = field(default_factory=list)
    returns: list[Value] = field(default_factory=list)
    state: State = field(default_factory=State)
    calls: dict[int, tuple] = field(default_factory=dict)

    def __deepcopy__(self, memo):
        # Evidence contains only JSON-like containers and immutable Values.
        # Generic deepcopy repeats its type/protocol dispatch for every scalar
        # in thousands of source spans. Clone containers while retaining the
        # same aliasing and isolation guarantees for every mutable field.
        result = Summary()
        memo[id(self)] = result
        result.events = copy_evidence(self.events, memo)
        result.refs = copy_evidence(self.refs, memo)
        result.returns = list(self.returns)
        result.state = deepcopy(self.state, memo)
        result.calls = copy_evidence(self.calls, memo)
        return result

    def result(self):
        values = list(self.returns)
        if not self.state.control:
            values.append(Value("literal", "None"))
        return merge(values, "return paths do not establish one binding/type")


def copy_evidence(value, memo):
    if type(value) not in (dict, list, tuple):
        return value
    if id(value) in memo:
        return memo[id(value)]
    if isinstance(value, dict):
        result = {}
        memo[id(value)] = result
        result.update((key, copy_evidence(item, memo)) for key, item in value.items())
    elif isinstance(value, list):
        result = []
        memo[id(value)] = result
        result.extend(copy_evidence(item, memo) for item in value)
    else:
        result = tuple(copy_evidence(item, memo) for item in value)
        memo[id(value)] = result
    return result


class Analysis:
    """Analyze only explicitly supplied source units; never import their code."""

    def __init__(self, sources: list[Source], *, facts_factory=None):
        from .lexical import LexicalFacts

        self.sources = sources
        self.facts = (facts_factory or LexicalFacts)(sources)
        self.symbols: dict[str, Symbol] = {}
        self.children: dict[str, list[Symbol]] = {}
        self.by_node: dict[int, Symbol] = {}
        self.duplicates = set()
        self.scopes = {}
        self.modules = {s.module: s for s in sources}
        self.module_prefixes = frozenset(".".join(module.split(".")[:size])
                                         for module in self.modules for size in range(1, len(module.split(".")) + 1))
        self.globals = {s.module: State() for s in sources}
        self.class_states: dict[str, State] = {}
        self.class_summaries: dict[str, Summary] = {}
        self.module_summaries: dict[str, Summary] = {}
        self.bases: dict[str, list[Value]] = {}
        self.orders: dict[str, list[str] | None] = {}
        self.mro_errors: dict[str, str] = {}
        self.mro_engines = {}
        self.cache: dict[tuple, Summary] = {}
        self.active = set()
        self.closures: dict[str, dict[str, Value]] = {}
        self.defaults: dict[str, dict[str, Value]] = {}
        self.diagnostics = [{"kind": "backend", "message": w} for w in self.facts.warnings]
        self.ready = False
        self.requested_modules = set()
        self.global_writes = {s.module: set() for s in sources}
        self.mutable_fields = set()
        generators = set()
        for source in sources:
            symbol = Symbol(source.module, "module", source.tree, source, None)
            self.symbols[symbol.name] = symbol
            self.by_node[id(source.tree)] = symbol
            self._collect(source.tree, symbol)
        for symbol in self.symbols.values():
            if isinstance(symbol.node, FUNCTIONS):
                nodes = symbol.source.lexical_nodes[1].get(symbol.node, ())
                if any(isinstance(node, (ast.Yield, ast.YieldFrom)) for node in nodes):
                    generators.add(symbol.name)
                declared = {name for n in nodes if isinstance(n, ast.Global) for name in n.names}
                written = {n.id for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))}
                self.global_writes[symbol.source.module].update(declared & written)
                params = [*symbol.node.args.posonlyargs, *symbol.node.args.args]
                if symbol.owner and params and getattr(symbol.node, "name", "") != "__init__":
                    self.mutable_fields.update(
                        (symbol.owner, n.attr)
                        for n in nodes
                        if (
                            isinstance(n, ast.Attribute)
                            and isinstance(n.ctx, (ast.Store, ast.Del))
                            and isinstance(n.value, ast.Name)
                            and n.value.id == params[0].arg
                        )
                    )
        self.generators = frozenset(generators)
        # Each pass can propagate an import/re-export through another input.
        # A finite budget is visible, and unresolved facts never become edges.
        initialization_sources = self.initialization_sources()
        for _ in range(len(self.symbols) + 2):
            before = {name: dict(state.names) for name, state in self.globals.items()}
            self.cache.clear()
            for source in initialization_sources:
                summary = self.walk(self.symbols[source.module], State())
                self.globals[source.module] = summary.state
                self.module_summaries[source.module] = summary
            self.orders.clear()
            self.mro_errors.clear()
            for name, symbol in self.symbols.items():
                if symbol.kind == "class":
                    self.mro(name)
            if before == {name: state.names for name, state in self.globals.items()}:
                break
        else:
            self.diagnostics.append({"kind": "limit", "message": "module bindings did not converge"})
        self.cache.clear()
        self.ready = True

    def initialization_sources(self):
        """Evaluate available import dependencies first; retain order inside cycles.

        This schedules the existing fixed-point solver, without replacing it or
        truncating iterations. Unresolved and cyclic imports still converge under
        the original input order.
        """
        from contextlib import suppress
        from graphlib import CycleError, TopologicalSorter

        graph = {}
        for source in self.sources:
            dependencies = set()
            for node in source.imports:
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                else:
                    module = self.import_module(source, node)
                    names = [module, *(module + "." + alias.name for alias in node.names)]
                for name in names:
                    parts = name.split(".")
                    dependencies.update(".".join(parts[:size]) for size in range(1, len(parts) + 1)
                                        if ".".join(parts[:size]) in self.modules)
            graph[source.module] = dependencies - {source.module}
        sorter = TopologicalSorter(graph)
        with suppress(CycleError):
            sorter.prepare()
        ordered = []
        while ready := sorter.get_ready():
            ordered.extend(self.modules[name] for name in ready)
            sorter.done(*ready)
        completed = {source.module for source in ordered}
        ordered.extend(source for source in self.sources if source.module not in completed)
        return ordered

    def fork(self):
        """Reuse immutable binding facts and AST identities with fresh query state."""
        shared = (self.sources, self.facts, self.symbols, self.children, self.by_node,
                  self.modules, self.scopes, self.generators, self.module_prefixes)
        return deepcopy(self, {id(value): value for value in shared})

    def _collect(self, node, parent):
        for child in parent.source.lexical_nodes[0].get(node, ()):
            short = child.name if hasattr(child, "name") else f"<lambda>@{child.lineno}:{child.col_offset}"
            name = f"{parent.name}.{short}"
            if name in self.symbols:
                self.duplicates.add(name)
            kind = "class" if isinstance(child, ast.ClassDef) else "function"
            decorators = tuple(ast.unparse(d) for d in getattr(child, "decorator_list", []))
            owner = parent.name if parent.kind == "class" and kind == "function" else None
            if owner:
                kind = (
                    "classmethod"
                    if "classmethod" in decorators
                    else ("staticmethod" if "staticmethod" in decorators else "method")
                )
            kind = self.facts.kind(name, kind) if self.facts else kind
            symbol = Symbol(name, kind, child, parent.source, parent.name, owner, decorators)
            self.symbols[name] = symbol
            self.children.setdefault(parent.name, []).append(symbol)
            self.by_node[id(child)] = symbol
            self._collect(child, symbol)

    @staticmethod
    def own_nodes(node):
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, DEFINITIONS):
                yield child
                yield from Analysis.own_nodes(child)

    def value(self, name):
        if name in self.duplicates:
            return unknown("multiple definitions of this qualified name", Value("symbol", name))
        symbol = self.symbols.get(name)
        if symbol is not None:
            unsupported = set(symbol.decorators) - {"staticmethod", "classmethod"}
            if unsupported:
                return unknown("decorator may replace the binding", Value("symbol", name))
            return Value(
                "class" if symbol.kind == "class" else ("module" if symbol.kind == "module" else "function"),
                name,
            )
        if name in self.module_prefixes:
            return Value("module", name)
        return Value("external", name, reason="definition is outside the analysis input")

    def imported(self, module, name="", seen=frozenset()):
        qualified = f"{module}.{name}" if name else module
        self.requested_modules.update((module, qualified))
        if qualified in seen:
            return unknown("cyclic import/re-export")
        if name and module in self.globals and name in self.globals[module].names:
            value = self.globals[module].names[name]
            if value.kind == "external" and value.target != qualified:
                parent, _, short = value.target.rpartition(".")
                return self.imported(parent, short, seen | {qualified})
            return value
        if qualified in self.modules:
            return Value("module", qualified)
        if name and module in self.module_summaries:
            return unknown("name is not bound by the supplied module")
        return self.value(qualified)

    def import_module(self, source, node):
        if not node.level:
            return node.module or ""
        package = source.module.split(".") if source.is_package else source.module.split(".")[:-1]
        if node.level > len(package):
            return ""
        return ".".join([*package[: len(package) - node.level + 1], *([node.module] if node.module else [])])

    def base_bindings(self, symbol):
        """Preserve unknown bases when a local declaration was not analyzed."""
        return self.bases.get(symbol.name, [unknown("class declaration has not established its bases")
                                            for _ in symbol.node.bases])

    def dynamic_type(self, name, active=frozenset()):
        if name in active:
            return False
        symbol = self.symbols.get(name)
        if symbol and isinstance(symbol.node, ast.ClassDef) and any(k.arg == "metaclass" for k in symbol.node.keywords):
            return True
        return any(self.dynamic_type(base.target, active | {name}) for base in self.bases.get(name, [])
                   if base.kind == "class")

    def mro(self, name, active=frozenset()):
        if name == "builtins.object":
            return [name]
        if self.dynamic_type(name):
            self.mro_errors[name] = "custom metaclass may change MRO and member bindings"
            return None
        if self.ready and name not in self.bases and name in self.symbols:
            parent = self.symbols.get(self.symbols[name].parent)
            while parent is not None and parent.kind == "class":
                parent = self.symbols.get(parent.parent)
            if (
                parent is not None
                and isinstance(parent.node, FUNCTIONS)
                and not any(key[0] == parent.name for key in self.active)
            ):
                self.function(parent.name)
        if name in self.orders:
            return self.orders[name]
        if name in active:
            self.mro_errors[name] = "cyclic inheritance"
            return None
        if name not in self.bases:
            self.mro_errors[name] = "class declaration has not established its bases"
            return None
        values = self.bases[name]
        if any(v.kind != "class" or (v.target not in self.symbols and v.target != "builtins.object") for v in values):
            self.orders[name] = None
            self.mro_errors[name] = "base class is unresolved or outside the analysis input"
            return None
        bases = [v.target for v in values] or ["builtins.object"]
        if len(set(bases)) != len(bases):
            self.orders[name] = None
            self.mro_errors[name] = "duplicate base class"
            return None
        parents = [self.mro(base, active | {name}) for base in bases]
        if any(p is None for p in parents):
            self.orders[name] = None
            self.mro_errors[name] = "a base class has no reliable MRO"
            return None
        sequences = [list(p) for p in parents] + [list(bases)]
        result = [name]
        while any(sequences):
            sequences = [s for s in sequences if s]
            head = next((s[0] for s in sequences if all(s[0] not in other[1:] for other in sequences)), None)
            if head is None:
                self.orders[name] = None
                self.mro_errors[name] = "inconsistent C3 method resolution order"
                return None
            result.append(head)
            sequences = [[x for x in seq if x != head] for seq in sequences]
        # Pyan omits implicit object. Reuse its C3 result only after validating
        # missing bases, cycles and its documented DFS fallback on invalid C3.
        upstream = self.facts.mro.get(name)
        if upstream is not None and [n for n in result if n != "builtins.object"] == [
            n for n in upstream if n != "builtins.object"
        ]:
            result = [n for n in upstream if n != "builtins.object"] + ["builtins.object"]
            self.mro_engines[name] = "pyan3"
        else:
            self.mro_engines[name] = "pyrel-c3"
        self.orders[name] = result
        return result

    def member(self, value, attr, state=None):
        identity = value.identity or value.target
        if identity and state is not None and (identity, attr) in state.attrs:
            return state.attrs[(identity, attr)]
        if identity and state is not None and (identity, "*") in state.attrs:
            return unknown("receiver attributes may have changed in a call")
        if value.kind in {"module", "external"}:
            return self.imported(value.target, attr)
        if value.kind not in {"class", "instance", "super"}:
            return unknown("receiver type is unresolved")
        receiver = value.receiver if value.kind == "super" else value.target
        if self.dynamic_type(receiver):
            return unknown("custom metaclass may change MRO and member bindings")
        order = self.mro(receiver)
        if value.kind == "super":
            if order is None or value.target not in order:
                return unknown("super receiver/start class has no reliable MRO")
            order = order[order.index(value.target) + 1 :]
        elif order is None:
            order = [receiver]  # own members precede every base, even an unknown one
        if (
            value.kind == "instance"
            and attr != "__getattribute__"
            and any("__getattribute__" in self.class_states.get(cls, State()).names for cls in order)
        ):
            return unknown("custom __getattribute__ controls instance attribute lookup")
        for cls in order:
            members = self.class_states.get(cls, State()).names
            overridden = state is not None and (cls, attr) in state.attrs
            if attr not in members and not overridden:
                continue
            member = state.attrs[(cls, attr)] if overridden else members[attr]
            symbol = self.symbols.get(member.target)
            if symbol is not None and member.kind == "function":
                if symbol.kind == "staticmethod":
                    return member
                if symbol.kind == "classmethod":
                    return Value("function", member.target, receiver, identity)
                if value.kind in {"instance", "super"}:
                    return Value("function", member.target, receiver, identity)
            return member
        if value.kind == "instance" and attr not in {"__init__", "__new__"} and self.ready:
            if any((cls, attr) in self.mutable_fields for cls in order):
                return unknown("instance field can be reassigned outside the constructor")
            if state is not None and (identity, "*") in state.attrs:
                return unknown("receiver attributes may have changed in a call")
            init = self.member(Value("instance", receiver), "__init__")
            if init.kind == "function":
                summary = self.function(init.target, receiver)
                result = summary.state.attrs.get((f"self:{receiver}", attr))
                if result is not None:
                    return result
        return unknown(self.mro_errors.get(receiver, "attribute binding is not established in the input"))

    def describe(self, value):
        is_symbol = value.kind in {"function", "class", "module", "builtin"}
        resolved = value.kind not in {"external", "unknown"}
        result = {"resolution": "resolved" if resolved else "unresolved", "target": value.target if is_symbol else None}
        if resolved and not is_symbol:
            result["value_kind"] = value.kind
        if value.receiver:
            result["receiver"] = value.receiver
            result["dispatch"] = "static_receiver"
        if value.kind == "external":
            result["import_target"] = value.target
        if not resolved:
            result["reason"] = value.reason or (
                "value is not a callable symbol" if value.kind != "unknown" else UNKNOWN.reason
            )
        if value.candidates:
            result["candidates"] = list(value.candidates)
        if value.kind == "instance":
            result["inferred_type"] = value.target
        if value.kind == "literal":
            result["literal"] = value.target
        if value.target in self.symbols and is_symbol:
            result["definition"] = self.symbols[value.target].record()
        return result

    def function(self, name, receiver="", arguments=None, closure=None, attributes=None):
        symbol = self.symbols[name]
        parent = self.symbols.get(symbol.parent)
        if (
            closure is None
            and parent is not None
            and isinstance(parent.node, FUNCTIONS)
            and not any(k[0] == parent.name for k in self.active)
        ):
            self.function(parent.name)
        receiver = receiver or (symbol.owner if symbol.kind != "staticmethod" else "") or ""
        bindings = dict(arguments or {})
        if symbol.owner and symbol.kind != "staticmethod":
            args = [*symbol.node.args.posonlyargs, *symbol.node.args.args]
            if args:
                bindings.setdefault(
                    args[0].arg,
                    Value(
                        "class" if symbol.kind == "classmethod" else "instance",
                        receiver,
                        identity=f"self:{receiver}",
                    ),
                )
        key = (
            name,
            receiver,
            tuple(sorted(bindings.items())),
            tuple(sorted((closure or {}).items())),
            tuple(sorted((attributes or {}).items())),
        )
        if key in self.cache:
            return self.cache[key]
        if key in self.active or len(self.active) >= 32:
            return Summary(returns=[unknown("recursive return inference")], state=State(control="return"))
        if len(self.cache) >= 4096:
            if not any(d.get("kind") == "limit" for d in self.diagnostics):
                self.diagnostics.append({"kind": "limit", "message": "4096 call contexts reached"})
            return Summary(returns=[unknown("call-context inference budget reached")], state=State(control="return"))
        self.active.add(key)
        try:
            state = State(attrs={**self.globals[symbol.source.module].attrs, **(attributes or {})})
            node = symbol.node
            params = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            params += [a for a in (node.args.vararg, node.args.kwarg) if a is not None]
            for param in params:
                state.names[param.arg] = bindings.get(
                    param.arg,
                    Value("unknown", identity=f"{name}.{param.arg}", reason="parameter value/type is not established"),
                )
            summary = self.walk(symbol, state, receiver, closure)
            self.cache[key] = summary
            return summary
        finally:
            self.active.remove(key)

    def walk(self, symbol, state, receiver="", closure=None):
        from .flow import Walker

        return Walker(self, symbol, receiver, closure).run(state)

    def summary(self, symbol, receiver=""):
        if isinstance(symbol.node, FUNCTIONS):
            return self.function(symbol.name, receiver)
        if symbol.kind == "class":
            if symbol.name not in self.class_summaries and symbol.parent in self.symbols:
                self.summary(self.symbols[symbol.parent])
            if symbol.name in self.class_summaries:
                return self.class_summaries[symbol.name]
            # A local class may remain unvisited when its enclosing function
            # reaches the call-context budget. Keep this boundary explicit.
            message = "class declaration was not analyzed in its enclosing scope"
            diagnostic = {**symbol.record(), "kind": "unresolved_scope", "message": message}
            if diagnostic not in self.diagnostics:
                self.diagnostics.append(diagnostic)
            return Summary(returns=[unknown(message)])
        return self.module_summaries[symbol.name]

    def all_summaries(self):
        summaries = [(symbol, self.summary(symbol)) for symbol in self.symbols.values()]
        # Include proven parameter/receiver contexts discovered at call sites.
        # The occurrence-level query deduplicates identical observations.
        known = {id(summary) for _, summary in summaries}
        summaries.extend(
            (self.symbols[key[0]], summary) for key, summary in list(self.cache.items()) if id(summary) not in known
        )
        yield from summaries

    def select(self, query, *, missing_ok=False):
        exact = self.symbols.get(query)
        if exact is not None:
            if query in self.duplicates:
                raise ValueError(f"ambiguous symbol (multiple definitions): {query}")
            return exact, ""
        matches = [s for name, s in self.symbols.items() if name.endswith("." + query)]
        if len(matches) == 1:
            if matches[0].name in self.duplicates:
                raise ValueError(f"ambiguous symbol (multiple definitions): {query}")
            return matches[0], ""
        if not matches and "." in query:
            class_query, attr = query.rsplit(".", 1)
            classes = [
                s
                for n, s in self.symbols.items()
                if s.kind == "class" and (n == class_query or n.endswith("." + class_query))
            ]
            if len(classes) > 1 or any(cls.name in self.duplicates for cls in classes):
                raise ValueError(f"ambiguous class for {query}: " + ", ".join(cls.name for cls in classes))
            if len(classes) == 1:
                value = self.member(Value("instance", classes[0].name), attr)
                if value.target in self.symbols:
                    return self.symbols[value.target], classes[0].name
                if missing_ok and self.mro(classes[0].name) is None:
                    raise ValueError(f"cannot establish whether {query} exists: unresolved inheritance")
        if matches:
            raise ValueError(f"ambiguous symbol: {query}; use one of: " + ", ".join(s.name for s in matches))
        if missing_ok:
            return None, ""
        raise ValueError(f"symbol not found in analysis input: {query}")
