"""Invocation-local lexical bindings, including parameters and local variables.

Bindings and their occurrences are distinct: repeated assignment is one binding;
two identically named functions have different lexical children. No code executes.
"""

from __future__ import annotations

import ast
import io
import symtable
import tokenize
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import islice

from .model import Symbol

FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
SCOPES = (*FUNCTIONS, ast.ClassDef)
KINDS = ("module", "class", "function", "method", "variable", "parameter", "field", "import")


@dataclass
class Binding:
    name: str
    kind: str
    node: ast.AST
    source: object
    parent: str | None
    scope: object
    identifier: dict
    declarations: list = field(default_factory=list)
    selections: list = field(default_factory=list)

    @property
    def owner(self):
        return self.parent if self.kind == "method" else None

    @property
    def decorators(self):
        return tuple(ast.unparse(x) for x in getattr(self.node, "decorator_list", []))

    @property
    def identity(self):
        return (self.source.name, self.scope.identity, self.name)

    def record(self):
        return {
            "symbol": self.name,
            "kind": self.kind,
            "parent": self.parent,
            **self.source.span(self.node),
            "selection": self.identifier,
        }

    def legacy(self):
        return Symbol(self.name, self.kind, self.node, self.source, self.parent, self.owner, self.decorators)


@dataclass
class Scope:
    name: str
    kind: str
    node: ast.AST
    parent: object
    table: object
    bindings: dict = field(default_factory=dict)

    @property
    def identity(self):
        return (self.name, getattr(self.node, "lineno", 0), getattr(self.node, "col_offset", 0))


class Catalog:
    def __init__(self, sources):
        self.sources = list(sources)
        self.entries, self.scopes, self.occurrences = [], [], []
        self.parents, self.node_scopes, self.tokens = {}, {}, {}
        self.name_tokens, self.token_starts = {}, {}
        self.roots = {}
        for source in self.sources:
            tokens = list(tokenize.generate_tokens(io.StringIO(source.text).readline))
            self.tokens[source.name] = tokens
            self.token_starts[source.name] = [token.start for token in tokens]
            names = defaultdict(list)
            for token in tokens:
                if token.type == tokenize.NAME:
                    names[token.string].append(token)
            self.name_tokens[source.name] = names
            for node in ast.walk(source.tree):
                self.parents.update((id(child), node) for child in ast.iter_child_nodes(node))
            table = symtable.symtable(source.text, source.name, "exec")
            scope = Scope(source.module, "module", source.tree, None, table)
            self.roots[source.name] = scope
            self.scopes.append(scope)
            module = Binding(source.module, "module", source.tree, source, None, scope, source.span(source.tree))
            module.declarations.append(source.tree)
            self.entries.append(module)
            self._walk(source.tree, scope, source)
        # Reads can precede declarations. Bind all occurrences after collection.
        for occurrence in self.occurrences:
            occurrence["entry"] = self.lookup(occurrence["scope"], occurrence["name"])
        self._index()

    @classmethod
    def combine(cls, parts):
        """Lexical scopes never cross files; reuse each immutable file catalog."""
        result = cls.__new__(cls)
        for key in ("sources", "entries", "scopes", "occurrences"):
            setattr(result, key, [item for part in parts for item in getattr(part, key)])
        for key in ("parents", "node_scopes", "tokens", "name_tokens", "token_starts", "roots"):
            setattr(result, key, {k: v for part in parts for k, v in getattr(part, key).items()})
        result._index()
        return result

    def _index(self):
        self.by_name, self.by_leaf, self.by_source = defaultdict(list), defaultdict(list), defaultdict(list)
        for entry in self.entries:
            self.by_name[entry.name].append(entry)
            self.by_leaf[entry.name.rsplit(".", 1)[-1]].append(entry)
            self.by_source[id(entry.source)].append(entry)
        self.path_modules = {}
        for source in self.sources:
            for path in (source.name, source.provenance.get("git_path")):
                if path is not None:
                    self.path_modules.setdefault(path, source.module)

    def selection(self, source, node, name, *, last=False):
        span = source.span(node)
        start, end = (span["line"], span["column"]), (span["end_line"], span["end_column"])
        tokens = self.name_tokens[source.name].get(name, ())
        index = (bisect_left(tokens, end, key=lambda t: t.start) - 1 if last
                 else bisect_left(tokens, start, key=lambda t: t.start))
        if 0 <= index < len(tokens) and start <= tokens[index].start < end:
            token = tokens[index]
            return {
                "source": source.name,
                "line": token.start[0],
                "column": token.start[1],
                "end_line": token.end[0],
                "end_column": token.end[1],
            }
        return span

    def owner(self, scope, name):
        try:
            info = scope.table.lookup(name) if scope.table else None
        except KeyError:
            info = None
        if info and scope.parent and info.is_global():
            while scope.parent:
                scope = scope.parent
            return scope
        if info and (info.is_nonlocal() or info.is_free()):
            parent = scope.parent
            while parent:
                if parent.kind != "class":
                    try:
                        if parent.table and parent.table.lookup(name).is_local():
                            return parent
                    except KeyError:
                        pass
                parent = parent.parent
        return scope

    def lookup(self, scope, name):
        owner = self.owner(scope, name)
        if name in owner.bindings:
            return owner.bindings[name]
        if owner is scope and scope.parent:
            if scope.table:
                try:
                    if scope.table.lookup(name).is_local():
                        return None
                except KeyError:
                    pass
            parent = scope.parent
            while parent:
                if parent.kind != "class" and name in parent.bindings:
                    return parent.bindings[name]
                parent = parent.parent
        return None

    def declare(self, source, scope, name, kind, node, token=None):
        owner = self.owner(scope, name)
        selection = self.selection(source, token or node, name, last=isinstance(token, ast.Attribute))
        entry = owner.bindings.get(name)
        if entry is None:
            entry = Binding(f"{owner.name}.{name}", kind, node, source, owner.name, owner, selection)
            owner.bindings[name] = entry
            self.entries.append(entry)
        elif kind in {"class", "function", "method"} and entry.kind == "variable":
            entry.kind, entry.node = kind, node
            entry.identifier = self.selection(source, node, name)
        if all(node is not old for old in entry.declarations):
            entry.declarations.append(node)
            entry.selections.append(selection)
        return entry

    def child(self, parent, node, name, kind, table_name):
        table = (
            next(
                (
                    t
                    for t in parent.table.get_children()
                    if t.get_name() == table_name and t.get_lineno() == node.lineno
                ),
                None,
            )
            if parent.table
            else None
        )
        scope = Scope(name, kind, node, parent, table)
        self.scopes.append(scope)
        return scope

    def _walk(self, node, scope, source):
        self.node_scopes[id(node)] = scope
        if isinstance(node, SCOPES):
            if isinstance(node, ast.Lambda):
                name = f"{scope.name}.<lambda>@{node.lineno}:{node.col_offset}"
                child = self.child(scope, node, name, "function", "lambda")
            else:
                kind = (
                    "class" if isinstance(node, ast.ClassDef) else "method" if scope.kind == "class" else "function"
                )
                self.declare(source, scope, node.name, kind, node)
                child = self.child(
                    scope,
                    node,
                    f"{scope.name}.{node.name}",
                    "class" if kind == "class" else "function",
                    node.name,
                )
                for decorator in node.decorator_list:
                    self._walk(decorator, scope, source)
            if isinstance(node, ast.ClassDef):
                for base in [*node.bases, *(k.value for k in node.keywords)]:
                    self._walk(base, scope, source)
            else:
                arguments = [
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                    *([node.args.vararg] if node.args.vararg else []),
                    *([node.args.kwarg] if node.args.kwarg else []),
                ]
                for arg in arguments:
                    self.node_scopes[id(arg)] = child
                    self.declare(source, child, arg.arg, "parameter", arg)
                    if arg.annotation:
                        self._walk(arg.annotation, scope, source)
                for default in [*node.args.defaults, *(d for d in node.args.kw_defaults if d)]:
                    self._walk(default, scope, source)
                if getattr(node, "returns", None):
                    self._walk(node.returns, scope, source)
            for item in node.body if isinstance(node.body, list) else [node.body]:
                self._walk(item, child, source)
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            table_name = {
                ast.ListComp: "listcomp",
                ast.SetComp: "setcomp",
                ast.DictComp: "dictcomp",
                ast.GeneratorExp: "genexpr",
            }[type(node)]
            self._walk(node.generators[0].iter, scope, source)
            child = self.child(
                scope,
                node,
                f"{scope.name}.<{table_name}>@{node.lineno}:{node.col_offset}",
                "comprehension",
                table_name,
            )
            for index, generator in enumerate(node.generators):
                if index:
                    self._walk(generator.iter, child, source)
                self._walk(generator.target, child, source)
                for condition in generator.ifs:
                    self._walk(condition, child, source)
            for expression in [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]:
                self._walk(expression, child, source)
            return
        if isinstance(node, ast.NamedExpr) and scope.kind == "comprehension":
            outer = scope
            while outer.kind == "comprehension":
                outer = outer.parent
            self._walk(node.target, outer, source)
            self._walk(node.value, scope, source)
            return
        if isinstance(node, ast.Name):
            role = "write" if isinstance(node.ctx, (ast.Store, ast.Del)) else "read"
            if role == "write":
                parent = self.parents.get(id(node), node)
                while isinstance(parent, (ast.Tuple, ast.List, ast.Starred)):
                    parent = self.parents.get(id(parent), parent)
                if not hasattr(parent, "lineno"):
                    parent = node
                self.declare(source, scope, node.id, "field" if scope.kind == "class" else "variable", parent, node)
            self.occurrences.append({"node": node, "source": source, "scope": scope, "name": node.id, "role": role})
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name != "*":
                    name = alias.asname or (alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name)
                    self.declare(source, scope, name, "import", node, alias)
                    self.occurrences.append(
                        {"node": alias, "source": source, "scope": scope, "name": name, "role": "import"},
                    )
        elif (isinstance(node, ast.ExceptHandler) and node.name) or (
            isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name
        ):
            self.declare(source, scope, node.name, "variable", node)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            self.declare(source, scope, node.rest, "variable", node)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
            if scope.parent and scope.parent.kind == "class" and isinstance(node.value, ast.Name):
                args = getattr(scope.node, "args", None)
                positional = [*args.posonlyargs, *args.args] if args else []
                decorators = {ast.unparse(d) for d in getattr(scope.node, "decorator_list", [])}
                if positional and node.value.id == positional[0].arg and "staticmethod" not in decorators:
                    self.declare(source, scope.parent, node.attr, "field", self.parents.get(id(node), node), node)
        for child in ast.iter_child_nodes(node):
            self._walk(child, scope, source)

    def select(self, selector):
        selector = self.path_modules.get(selector, selector)
        return self.by_name.get(selector, []) or [
            entry for entry in self.by_leaf.get(selector.rsplit(".", 1)[-1], ())
            if entry.name.endswith("." + selector)
        ]

    def scope_at(self, source, line, column=None):
        point = (line, column or 0)
        found = []
        for scope in self.scopes:
            if self.node_scopes.get(id(source.tree)) is not None:
                root = scope
                while root.parent:
                    root = root.parent
                if root is not self.roots[source.name]:
                    continue
            span = source.span(scope.node)
            decorators = getattr(scope.node, "decorator_list", [])
            if decorators:
                span = {**span, "line": min(item.lineno for item in decorators), "column": 0}
            if (
                (span["line"] <= line <= span["end_line"])
                if column is None
                else (span["line"], span["column"]) <= point < (span["end_line"], span["end_column"])
            ):
                found.append(scope)
        return found

    def at(self, source, span):
        point = (span["line"], span["column"])
        found = [
            entry
            for entry in self.by_source.get(id(source), ())
            if entry.kind != "module"
            and any(
                (site["line"], site["column"]) <= point < (site["end_line"], site["end_column"])
                for site in entry.selections
            )
        ]
        if not found and span == source.span(source.tree):
            return [entry for entry in self.entries if entry.source is source and entry.kind == "module"]
        return found

    def header(self, source, node):
        span = source.span(node)
        if not isinstance(
            node,
            (
                *SCOPES,
                ast.If,
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.With,
                ast.AsyncWith,
                ast.Try,
                ast.TryStar,
                ast.ExceptHandler,
                ast.Match,
            ),
        ):
            return span
        depth = 0
        first = bisect_left(self.token_starts[source.name], (span["line"], span["column"]))
        for token in islice(self.tokens[source.name], first, None):
            if token.type == tokenize.OP:
                if token.string in {"(", "[", "{"}:
                    depth += 1
                elif token.string in {")", "]", "}"}:
                    depth -= 1
                elif token.string == ":" and depth == 0:
                    return {**span, "end_line": token.end[0], "end_column": token.end[1]}
        return span
