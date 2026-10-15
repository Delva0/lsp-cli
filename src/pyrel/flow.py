"""Ordered source events and conservative, occurrence-level value propagation."""

from __future__ import annotations

import ast
import builtins
from dataclasses import replace

from .analysis import DEFINITIONS, FUNCTIONS, Summary
from .model import UNKNOWN, State, Value, merge, unknown


class Walker:
    def __init__(self, analysis, symbol, receiver="", closure=None):
        self.a = analysis
        self.symbol = symbol
        self.source = symbol.source
        self.receiver = receiver
        self.closure = closure or analysis.closures.get(symbol.name, {})
        self.out = Summary()
        self.record = True
        self.comprehensions = []
        self.loop_exits = []
        self.receivers = {}
        if symbol.name not in analysis.scopes:
            self.locals = analysis.facts.local_names(symbol.name)
            self.globals = set()
            self.nonlocals = set()
            self._scopes(symbol.node, root=True)
            self.locals -= self.globals | self.nonlocals
            # Lexical scopes are invariant across call arguments and query forks.
            analysis.scopes[symbol.name] = tuple(map(frozenset, (self.locals, self.globals, self.nonlocals)))
        self.locals, self.globals, self.nonlocals = analysis.scopes[symbol.name]

    def _scopes(self, node, root=False):
        if not root and isinstance(node, DEFINITIONS):
            if hasattr(node, "name"):
                self.locals.add(node.name)
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            for child in ast.walk(node):
                if isinstance(child, ast.NamedExpr):
                    self.locals.add(child.target.id)
            return
        if isinstance(node, ast.Global):
            self.globals.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            self.nonlocals.update(node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            self.locals.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            self.locals.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            self.locals.add(node.name)
        for child in ast.iter_child_nodes(node):
            self._scopes(child)

    def run(self, state):
        node = self.symbol.node
        if isinstance(node, FUNCTIONS):
            for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
                self.ref(
                    arg,
                    arg.arg,
                    "parameter",
                    state.names.get(arg.arg, UNKNOWN),
                    state,
                    binding=f"{self.symbol.name}.{arg.arg}",
                )
        if isinstance(node, ast.Lambda):
            self.out.returns.append(self.expr(node.body, state))
            state.control = "return"
        else:
            state = self.block(node.body, state)
        self.out.state = state
        if isinstance(node, FUNCTIONS):
            for symbol in self.a.symbols.values():
                if symbol.parent == self.symbol.name and isinstance(symbol.node, FUNCTIONS):
                    self.a.closures[symbol.name] = dict(state.names)
        return self.out

    def text(self, node):
        return self.source.segment(node)

    def context(self, state, kind, node, label=""):
        return (*state.context, dict(kind=kind, label=label, **self.source.span(node)))

    def event(self, kind, node, state, **details):
        event = dict(
            kind=kind,
            scope=self.symbol.name,
            **self.source.span(node),
            text=self.text(node),
            context=list(state.context),
            **details,
        )
        if state.control:
            event["unreachable"] = True
        if self.record:
            event["order"] = len(self.out.events) + 1
            self.out.events.append(event)
        return event

    def ref(self, node, expression, role, value, state, binding=""):
        if not self.record:
            return
        record = dict(
            kind="REF",
            scope=self.symbol.name,
            expression=expression,
            role=role,
            binding=binding or self.binding(node, state),
            **self.source.span(node),
            context=list(state.context),
            **self.a.describe(value),
        )
        if state.control:
            record["unreachable"] = True
        self.out.refs.append(record)

    def lookup(self, name, state):
        if name in state.names:
            value = state.names[name]
            target = self.a.symbols.get(value.target)
            if value.kind == "function" and target is not None and target.parent == self.symbol.name:
                environment = tuple(
                    sorted((key, val) for key, val in state.names.items() if val.target != value.target),
                )
                return replace(value, environment=environment)
            return value
        if isinstance(self.symbol.node, FUNCTIONS) and name in self.locals:
            return unknown("local is not bound at this occurrence")
        if name in self.closure and name not in self.globals:
            return self.closure[name]
        module_names = self.a.globals[self.source.module].names
        if self.symbol.kind not in {"module", "class"} and name in module_names:
            if name in self.a.global_writes[self.source.module]:
                return unknown("global binding can be reassigned by another function", module_names[name])
            return module_names[name]
        if "*" in state.names or (self.symbol.kind != "module" and "*" in module_names):
            return unknown("unanalyzed wildcard import may bind this name")
        if name == "object":
            return Value("class", "builtins.object")
        if hasattr(builtins, name):
            return Value("builtin", f"builtins.{name}")
        return unknown("name is not bound in the lexical scope")

    def binding(self, node, state):
        if isinstance(node, ast.Name):
            for scope, names in reversed(self.comprehensions):
                if node.id in names:
                    return f"{scope}.{node.id}"
            if name := (node.id if node.id in state.names or node.id in self.locals else ""):
                owner = self.source.module if node.id in self.globals else self.symbol.name
                return f"{owner}.{name}"
            if node.id in self.closure:
                return f"{self.symbol.parent}.{node.id}"
            return f"{self.source.module}.{node.id}"
        if isinstance(node, ast.Attribute):
            old = self.record
            self.record = False
            try:
                value = self.expr(node.value, state.copy())
            finally:
                self.record = old
            if value.kind in {"instance", "class"}:
                return f"{value.target}.{node.attr}"
            return f"{value.identity or self.text(node.value)}.{node.attr}"
        return self.text(node)

    def expr(self, node, state):
        if node is None:
            return Value("literal", "None")
        if isinstance(node, ast.Name):
            value = self.lookup(node.id, state)
            self.ref(node, node.id, "read", value, state)
            return value
        if isinstance(node, ast.Attribute):
            receiver = self.expr(node.value, state)
            self.receivers[id(node)] = receiver
            value = self.a.member(receiver, node.attr, state)
            identity = (
                receiver.target
                if receiver.kind in {"class", "instance"}
                else (receiver.identity or self.text(node.value))
            )
            self.ref(
                node,
                self.text(node),
                "read",
                value,
                state,
                binding=f"{identity}.{node.attr}",
            )
            return value
        if isinstance(node, ast.Call):
            return self.call(node, state)
        if isinstance(node, ast.Constant):
            return Value("literal", repr(node.value))
        if isinstance(node, (ast.Tuple, ast.List)):
            return Value("sequence", items=tuple(self.expr(e, state) for e in node.elts))
        if isinstance(node, ast.Set):
            return Value("set", items=tuple(self.expr(e, state) for e in node.elts))
        if isinstance(node, ast.Dict):
            entries = {}
            uncertain = False
            for key, value in zip(node.keys, node.values, strict=True):
                resolved_key = self.expr(key, state) if key else UNKNOWN
                resolved_value = self.expr(value, state)
                if resolved_key.kind != "literal":
                    uncertain = True
                entries[resolved_key.target] = resolved_value
            if uncertain:
                return unknown("dynamic mapping keys or unpacking can replace entries")
            return Value("mapping", items=tuple(entries.values()), keys=tuple(entries))
        if isinstance(node, ast.Subscript):
            value = self.expr(node.value, state)
            key = self.expr(node.slice, state)
            if value.kind == "sequence" and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, int):
                try:
                    return value.items[node.slice.value]
                except IndexError:
                    return unknown("subscript is out of range")
            if value.kind == "mapping" and key.target in value.keys:
                return value.items[value.keys.index(key.target)]
            return unknown("subscript value is not established")
        if isinstance(node, ast.NamedExpr):
            value = self.expr(node.value, state)
            self.assign(node.target, value, state, node, node.value)
            return value
        if isinstance(node, ast.IfExp):
            self.expr(node.test, state)
            left, right = (
                state.copy(self.context(state, "if", node, self.text(node.test))),
                state.copy(self.context(state, "else", node, self.text(node.test))),
            )
            result = merge([self.expr(node.body, left), self.expr(node.orelse, right)])
            merged = State.join([left, right], state.context)
            state.names, state.attrs = merged.names, merged.attrs
            return result
        if isinstance(node, ast.BoolOp):
            values = []
            outcomes = []
            for index, part in enumerate(node.values):
                branch = (
                    state
                    if index == 0
                    else state.copy(
                        self.context(
                            state,
                            "short_circuit",
                            node,
                            f"{type(node.op).__name__.lower()} operand {index + 1}",
                        ),
                    )
                )
                values.append(self.expr(part, branch))
                outcomes.append(branch.copy())
            merged = State.join(outcomes, state.context)
            state.names, state.attrs = merged.names, merged.attrs
            return merge(values, "short-circuit expression has multiple values")
        if isinstance(node, ast.Lambda):
            symbol = self.a.by_node[id(node)]
            self.a.closures[symbol.name] = dict(state.names)
            return self.a.value(symbol.name)
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            branch = state.copy(
                self.context(
                    state,
                    "comprehension",
                    node,
                    "deferred" if isinstance(node, ast.GeneratorExp) else "iteration",
                ),
            )
            names = {
                n.id for generator in node.generators for n in ast.walk(generator.target) if isinstance(n, ast.Name)
            }
            scope = f"{self.symbol.name}.<{type(node).__name__.lower()}>@{node.lineno}:{node.col_offset}"
            self.comprehensions.append((scope, names))
            for generator in node.generators:
                iterable = self.expr(generator.iter, branch)
                element = merge(iterable.items) if iterable.kind in {"sequence", "set"} else UNKNOWN
                self.assign(generator.target, element, branch, generator.target, generator.iter)
                for test in generator.ifs:
                    self.expr(test, branch)
                    branch.context = self.context(branch, "if", test, self.text(test))
            if isinstance(node, ast.DictComp):
                self.expr(node.key, branch)
                self.expr(node.value, branch)
            else:
                self.expr(node.elt, branch)
            self.comprehensions.pop()
            return unknown("comprehension result is not statically enumerated")
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            self.expr(node.value, state)
            return unknown("generator yield/resume value")
        if isinstance(node, ast.Await):
            value = self.expr(node.value, state)
            return value.items[0] if value.kind == "coroutine" else unknown("await result is not established")
        # Operators and formatting may invoke dynamic protocols; visit every
        # explicit subexpression without inventing implicit call targets.
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                self.expr(child, state)
        return unknown(f"value of {type(node).__name__} is not established")

    def call(self, node, state):
        value = self.expr(node.func, state)
        args = [self.expr(arg, state) for arg in node.args]
        kwargs = {kw.arg: self.expr(kw.value, state) for kw in node.keywords}
        if value.kind == "instance":
            value = self.a.member(value, "__call__", state)
        if value.kind not in {"function", "class", "builtin", "external", "unknown"}:
            value = unknown("value is not a known callable")
        event = self.event("CALL", node, state, callee=self.text(node.func), **self.a.describe(value))
        upstream = self.a.facts.binding(self.source, node)
        if event["resolution"] == "resolved":
            event["binding_engine"] = "pyan3+flow" if upstream == value.target else "pyrel"
        if value.kind == "builtin" and value.target == "builtins.super":
            if not args:
                owner = self.symbol.owner
                params = [*self.symbol.node.args.posonlyargs, *self.symbol.node.args.args] if owner else []
                first = state.names.get(params[0].arg, UNKNOWN) if params else UNKNOWN
                if owner and first.kind in {"instance", "class"}:
                    return Value("super", owner, first.target, first.identity or first.target)
                return unknown("zero-argument super has no bound method receiver")
            if len(args) == 2 and args[0].kind == "class" and args[1].kind in {"instance", "class"}:
                return Value("super", args[0].target, args[1].target, args[1].identity or args[1].target)
            return unknown("super arguments do not establish a start class and receiver")
        if value.kind == "builtin" and value.target in {"builtins.setattr", "builtins.delattr"} and len(args) >= 2:
            if args[1].kind == "literal":
                try:
                    attr = ast.literal_eval(args[1].target)
                except (ValueError, SyntaxError):
                    attr = None
                identity = args[0].identity or args[0].target
                if isinstance(attr, str) and identity:
                    state.attrs[(identity, attr)] = args[2] if len(args) == 3 else unknown("attribute was deleted")
            return Value("literal", "None")
        if value.kind == "class":
            symbol = self.a.symbols.get(value.target)
            if symbol is not None and any(kw.arg == "metaclass" for kw in symbol.node.keywords):
                return unknown("custom metaclass may change the constructor result")
            instance = Value("instance", value.target, identity=f"{self.symbol.name}@{node.lineno}:{node.col_offset}")
            init = self.a.member(instance, "__init__", state)
            if init.kind == "function":
                arguments = self.bind_arguments(init, node, args, kwargs)
                event["constructor"] = self.a.describe(init)
                if self.record:
                    self.out.calls[event["order"]] = (init.target, init.receiver, arguments, {}, dict(state.attrs))
                summary = self.a.function(init.target, init.receiver, arguments, attributes=state.attrs)
                self.apply_effects(summary, state, init)
            return instance
        if value.kind != "function" or value.target not in self.a.symbols:
            receiver = self.receivers.get(id(node.func))
            values = [*args, *kwargs.values(), *([receiver] if receiver else [])]
            # A small set of builtins is known not to replace object attributes.
            if value.target in {"builtins.isinstance", "builtins.issubclass", "builtins.type", "builtins.id"}:
                values = []
            for arg in values:
                if arg.identity:
                    for key in list(state.attrs):
                        if key[0] == arg.identity:
                            state.attrs[key] = unknown("receiver attributes may have changed in a call")
                    state.attrs[(arg.identity, "*")] = UNKNOWN
            return unknown("call result is not established")
        target = self.a.symbols[value.target]
        arguments = self.bind_arguments(value, node, args, kwargs)
        closure = state.names if target.parent == self.symbol.name else dict(value.environment) or None
        if self.record:
            self.out.calls[event["order"]] = (
                value.target,
                value.receiver,
                arguments,
                dict(closure or {}),
                dict(state.attrs),
            )
        summary = self.a.function(value.target, value.receiver, arguments, closure, state.attrs)
        self.apply_effects(summary, state, value)
        # An async function call creates a coroutine. Only await unwraps it;
        # direct attribute dispatch on that coroutine must remain unresolved.
        if isinstance(target.node, ast.AsyncFunctionDef):
            return Value("coroutine", items=(summary.result(),))
        if target.name in self.a.generators:
            return unknown("call returns a generator")
        result = summary.result()
        if (
            result.kind == "instance"
            and result.identity
            and result.identity not in {v.identity for v in arguments.values()}
        ):
            identity = f"{self.symbol.name}@{node.lineno}:{node.col_offset}"
            for (old_identity, attr), field in list(state.attrs.items()):
                if old_identity == result.identity:
                    state.attrs[(identity, attr)] = field
            result = replace(result, identity=identity)
        return result

    def bind_arguments(self, value, node, args, kwargs):
        target = self.a.symbols[value.target]
        params = [*target.node.args.posonlyargs, *target.node.args.args]
        arguments = {}
        if value.receiver and params:
            arguments[params[0].arg] = Value(
                "class" if target.kind == "classmethod" else "instance",
                value.receiver,
                identity=value.identity or f"self:{value.receiver}",
            )
            params = params[1:]
        if any(isinstance(arg, ast.Starred) for arg in node.args) or None in kwargs:
            arguments.update({p.arg: unknown("expanded arguments are not statically bound") for p in params})
        else:
            arguments.update({p.arg: arg for p, arg in zip(params, args, strict=False)})
            arguments.update(kwargs)
            for name, default in self.a.defaults.get(value.target, {}).items():
                arguments.setdefault(name, default)
            if target.node.args.vararg:
                arguments[target.node.args.vararg.arg] = Value("sequence", items=tuple(args[len(params) :]))
            if target.node.args.kwarg:
                declared = {p.arg for p in [*params, *target.node.args.kwonlyargs]}
                extra = {name: val for name, val in kwargs.items() if name not in declared}
                arguments[target.node.args.kwarg.arg] = Value(
                    "mapping",
                    items=tuple(extra.values()),
                    keys=tuple(repr(k) for k in extra),
                )
        return arguments

    @staticmethod
    def apply_effects(summary, state, callable_value):
        for (identity, attr), result in summary.state.attrs.items():
            destination = (
                callable_value.identity or identity
                if (callable_value.receiver and identity == f"self:{callable_value.receiver}")
                else identity
            )
            state.attrs[(destination, attr)] = result

    @staticmethod
    def own_nodes(node):
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, DEFINITIONS):
                yield child
                yield from Walker.own_nodes(child)

    def assign(self, target, value, state, statement, rhs=None, operation="assign", evaluated=False):
        if isinstance(target, (ast.Tuple, ast.List)):
            values = list(value.items) if value.kind == "sequence" else []
            starred = next((i for i, t in enumerate(target.elts) if isinstance(t, ast.Starred)), None)
            for index, child in enumerate(target.elts):
                item = UNKNOWN
                if starred is None and len(values) == len(target.elts):
                    item = values[index]
                elif starred is not None and len(values) >= len(target.elts) - 1:
                    if index < starred:
                        item = values[index]
                    elif index == starred:
                        stop = len(values) - (len(target.elts) - index - 1)
                        item = Value("sequence", items=tuple(values[index:stop]))
                    else:
                        item = values[len(values) - len(target.elts) + index]
                self.assign(child, item, state, statement, rhs, operation)
            return
        if isinstance(target, ast.Starred):
            self.assign(target.value, value, state, statement, rhs, operation)
            return
        kind = "ASSIGN"
        reference = value
        binding = self.binding(target, state)
        if isinstance(target, ast.Name):
            if value.kind == "unknown" and not value.identity:
                value = Value("unknown", identity=binding, reason=value.reason, candidates=value.candidates)
            state.names[target.id] = value
        elif isinstance(target, ast.Attribute):
            receiver = self.receivers[id(target)] if evaluated else self.expr(target.value, state)
            reference = self.a.member(receiver, target.attr, state)
            identity = receiver.identity or receiver.target
            if identity:
                state.attrs[(identity, target.attr)] = value
            kind = "ATTR_WRITE"
        elif isinstance(target, ast.Subscript):
            if not evaluated:
                self.expr(target.value, state)
                self.expr(target.slice, state)
            # Mutating a container invalidates its element facts, including
            # aliases. Do not reuse the pre-write literal on subsequent reads.
            if isinstance(target.value, ast.Name):
                previous = state.names.get(target.value.id)
                for name, old in list(state.names.items()):
                    if old is previous:
                        state.names[name] = unknown("container was mutated")
            kind = "SUBSCRIPT_WRITE"
        self.ref(target, self.text(target), "write", reference, state, binding)
        if self.record:
            self.out.refs[-1].update(
                text=self.text(statement),
                statement_span=self.source.span(statement),
                value_binding=self.a.describe(value),
            )
        self.event(
            kind,
            statement,
            state,
            target=self.text(target),
            binding=binding,
            target_span=self.source.span(target),
            value=self.text(rhs) if rhs is not None else None,
            value_binding=self.a.describe(value),
            operation=operation,
        )

    def block(self, nodes, state):
        for node in nodes:
            if state.control:
                # Preserve source evidence after a terminator, while keeping it
                # out of summaries and path searches.
                saved = len(self.out.returns)
                self.stmt(node, state.copy())
                del self.out.returns[saved:]
            else:
                state = self.stmt(node, state)
        return state

    def stmt(self, node, state):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            if isinstance(node, ast.AnnAssign) and node.value is None:
                # A bare annotation does not replace an existing runtime value.
                if isinstance(node.target, ast.Attribute):
                    self.expr(node.target.value, state)
                elif isinstance(node.target, ast.Subscript):
                    self.expr(node.target.value, state)
                    self.expr(node.target.slice, state)
                return state
            if isinstance(node, ast.AugAssign):
                self.expr(node.target, state)
            value = self.expr(node.value, state) if node.value is not None else UNKNOWN
            if isinstance(node, ast.AugAssign):
                value = unknown("augmented assignment changes the value")
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                self.assign(
                    target,
                    value,
                    state,
                    node,
                    node.value,
                    type(node.op).__name__ if isinstance(node, ast.AugAssign) else "assign",
                    evaluated=isinstance(node, ast.AugAssign),
                )
        elif isinstance(node, ast.Expr):
            self.expr(node.value, state)
        elif isinstance(node, ast.Return):
            value = self.expr(node.value, state)
            self.out.returns.append(value)
            self.event("RETURN", node, state, value_binding=self.a.describe(value))
            state.control = "return"
        elif isinstance(node, ast.Raise):
            self.expr(node.exc, state)
            self.expr(node.cause, state)
            self.event("RAISE", node, state)
            state.control = "raise"
        elif isinstance(node, ast.If):
            test = state.copy(self.context(state, "if_test", node.test, self.text(node.test)))
            self.expr(node.test, test)
            left, right = (
                test.copy(self.context(state, "if", node, self.text(node.test))),
                test.copy(self.context(state, "else", node, self.text(node.test))),
            )
            if isinstance(node.test, ast.Constant):
                (right if node.test.value else left).control = "unreachable"
            left, right = self.block(node.body, left), self.block(node.orelse, right)
            state = State.join([left, right], state.context)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            branch = state.copy(
                self.context(state, "loop", node, self.text(node.test if isinstance(node, ast.While) else node.iter)),
            )
            if isinstance(node, ast.While):
                self.expr(node.test, branch)
            else:
                iterable = self.expr(node.iter, branch)
                element = merge(iterable.items) if iterable.kind in {"sequence", "set"} else UNKNOWN
                self.assign(node.target, element, branch, node.target, node.iter)
            # A loop body can run repeatedly. Kill values written in it before
            # resolving body calls, avoiding first-iteration-only false edges.
            for item in self.own_nodes(node):
                if (
                    isinstance(item, ast.Name)
                    and isinstance(item.ctx, ast.Store)
                    and (isinstance(node, ast.While) or item not in ast.walk(node.target))
                ):
                    branch.names[item.id] = unknown("binding may change across loop iterations")
                if isinstance(item, ast.Attribute) and isinstance(item.ctx, ast.Store):
                    old = self.record
                    self.record = False
                    try:
                        value = self.expr(item.value, branch)
                    finally:
                        self.record = old
                    identity = value.identity or value.target
                    if identity:
                        branch.attrs[(identity, item.attr)] = unknown("attribute may change across loop iterations")
            self.loop_exits.append([])
            branch = self.block(node.body, branch)
            exits = self.loop_exits.pop()
            if branch.control in {"break", "continue"}:
                branch.control = ""
            state = State.join([state, branch, *exits], state.context)
            otherwise = self.block(node.orelse, state.copy(self.context(state, "loop_else", node, "no break")))
            state = State.join([state, otherwise], state.context)
        elif isinstance(node, (ast.Try, ast.TryStar)):
            original = state.copy()
            body = self.block(node.body, state.copy(self.context(state, "try", node)))
            branches = []
            if not body.control:
                branches.append(self.block(node.orelse, body.copy(self.context(state, "try_else", node))))
            else:
                branches.append(body)
            # Any statement in try can raise before or after rebinding. Merge
            # both boundaries and invalidate body-written names for handlers.
            caught = State.join([original, State(dict(body.names), dict(body.attrs))], state.context)
            for item in self.own_nodes(node):
                if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store):
                    caught.names[item.id] = unknown("try may raise before or after a binding changes")
            for handler in node.handlers:
                branch = caught.copy(
                    self.context(state, "except", handler, self.text(handler.type) if handler.type else "*"),
                )
                if handler.type:
                    self.expr(handler.type, branch)
                if handler.name:
                    branch.names[handler.name] = unknown("exception instance")
                branch = self.block(handler.body, branch)
                if handler.name:
                    branch.names[handler.name] = unknown("exception binding is deleted after handler")
                branches.append(branch)
            state = State.join(branches, state.context)
            if node.finalbody:
                # finally runs even on return/raise; its own terminator wins.
                control = state.control
                state.control = ""
                state.context = self.context(state, "finally", node)
                state = self.block(node.finalbody, state)
                state.control = state.control or control
                state.context = original.context
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            previous = state.context
            for item in node.items:
                value = self.expr(item.context_expr, state)
                if item.optional_vars:
                    enter = self.a.member(
                        value,
                        "__aenter__" if isinstance(node, ast.AsyncWith) else "__enter__",
                        state,
                    )
                    result = unknown("context manager enter result is not established")
                    if enter.kind == "function":
                        method = self.a.symbols[enter.target].node
                        params = [*method.args.posonlyargs, *method.args.args]
                        arguments = {params[0].arg: value} if params else {}
                        result = self.a.function(
                            enter.target,
                            enter.receiver,
                            arguments,
                            attributes=state.attrs,
                        ).result()
                    self.assign(item.optional_vars, result, state, item.optional_vars, item.context_expr)
            state.context = self.context(state, "async_with" if isinstance(node, ast.AsyncWith) else "with", node)
            state = self.block(node.body, state)
            state.context = previous
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            self.imports(node, state)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for expr in node.decorator_list:
                self.expr(expr, state)
            symbol = self.a.by_node[id(node)]
            positional = [*node.args.posonlyargs, *node.args.args]
            defaults = {
                param.arg: self.expr(expr, state)
                for param, expr in zip(
                    positional[len(positional) - len(node.args.defaults) :],
                    node.args.defaults,
                    strict=True,
                )
            }
            defaults.update(
                {
                    param.arg: self.expr(expr, state)
                    for param, expr in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True)
                    if expr is not None
                },
            )
            self.a.defaults[symbol.name] = defaults
            value = self.a.value(symbol.name)
            self.a.closures[symbol.name] = dict(state.names) if isinstance(self.symbol.node, FUNCTIONS) else {}
            state.names[node.name] = value
            self.ref(node, node.name, "definition", value, state, f"{self.symbol.name}.{node.name}")
        elif isinstance(node, ast.ClassDef):
            symbol = self.a.by_node[id(node)]
            for decorator in node.decorator_list:
                self.expr(decorator, state)
            self.a.bases[symbol.name] = [self.expr(base, state) for base in node.bases]
            self.a.orders.clear()
            self.a.mro_errors.clear()
            for keyword in node.keywords:
                self.expr(keyword.value, state)
            closure = self.closure if self.symbol.kind == "class" else dict(state.names)
            summary = self.a.walk(symbol, State(), closure=closure)
            self.a.class_summaries[symbol.name] = summary
            self.a.class_states[symbol.name] = summary.state
            state.names[node.name] = self.a.value(symbol.name)
            self.ref(node, node.name, "definition", state.names[node.name], state, symbol.name)
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                self.assign(target, unknown("binding was deleted"), state, node, operation="delete")
        elif isinstance(node, (ast.Break, ast.Continue)):
            if self.loop_exits and not state.control:
                self.loop_exits[-1].append(state.copy())
            state.control = "break" if isinstance(node, ast.Break) else "continue"
        elif isinstance(node, ast.Assert):
            self.expr(node.test, state)
            if node.msg:
                self.expr(node.msg, state.copy(self.context(state, "assert_failure", node)))
        elif isinstance(node, ast.Match):
            self.expr(node.subject, state)
            branches = [state.copy()]
            for case in node.cases:
                branch = state.copy(self.context(state, "case", case.pattern, self.text(case.pattern)))
                for pattern in ast.walk(case.pattern):
                    capture = getattr(pattern, "name", None)
                    if capture:
                        branch.names[capture] = unknown("pattern capture type is not established")
                    if isinstance(pattern, ast.MatchMapping) and pattern.rest:
                        branch.names[pattern.rest] = unknown("mapping pattern capture")
                    if isinstance(pattern, ast.MatchValue):
                        self.expr(pattern.value, branch)
                    if isinstance(pattern, ast.MatchClass):
                        self.expr(pattern.cls, branch)
                if case.guard:
                    self.expr(case.guard, branch)
                branches.append(self.block(case.body, branch))
            state = State.join(branches, state.context)
        elif isinstance(node, ast.TypeAlias):
            self.assign(node.name, unknown("lazy type alias"), state, node, node.value)
        return state

    def imports(self, node, state):
        for alias in node.names:
            if isinstance(node, ast.Import):
                name = alias.asname or alias.name.split(".")[0]
                value = self.a.imported(alias.name if alias.asname else name)
            else:
                module = self.a.import_module(self.source, node)
                if alias.name == "*":
                    self.star_import(module, state)
                    continue
                name = alias.asname or alias.name
                value = (
                    self.a.imported(module, alias.name)
                    if module
                    else unknown("relative import has no package identity")
                )
            state.names[name] = value
            self.ref(alias, name, "import", value, state, f"{self.symbol.name}.{name}")

    def star_import(self, module, state):
        if module not in self.a.globals:
            state.names.update(
                dict.fromkeys(state.names, unknown("unanalyzed wildcard import may replace this binding")),
            )
            state.names["*"] = UNKNOWN
            return
        imported = self.a.globals[module].names
        exports = imported.get("__all__")
        if exports is not None:
            if exports.kind != "sequence" or any(v.kind != "literal" for v in exports.items):
                return
            try:
                names = [ast.literal_eval(v.target) for v in exports.items]
            except (ValueError, SyntaxError):
                return
        else:
            names = [name for name in imported if not name.startswith("_")]
        for name in names:
            if isinstance(name, str):
                state.names[name] = imported.get(name, UNKNOWN)
