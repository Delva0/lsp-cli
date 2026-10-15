"""Source identities, exact spans, and immutable abstract bindings."""

from __future__ import annotations

import ast
import hashlib
import re
from copy import deepcopy
from dataclasses import dataclass, field
from functools import cached_property
from urllib.parse import quote


def module_name(path):
    """Keep importable names intact and give other Python files a distinct scope.

    A file scope is not an inferred import alias. Escape literal dots and percent
    signs so e.g. ``a.b/task.py`` cannot collide with ``a/b/task.py``. Invalid
    identifier characters remain invalid; never normalize them to underscores.
    """
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(
        part if part.isidentifier() else quote(part, safe="-").replace(".", "%2E")
        for part in parts
    ) or "@__init__"


def source_lines(text, *, keepends=False):
    """Split Python physical lines; form feeds and Unicode separators stay in a line."""
    parts = re.split(r"(\r\n|\r|\n)", text)
    lines = [line + ending if keepends else line for line, ending in zip(parts[::2], parts[1::2], strict=False)]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


@dataclass
class Source:
    name: str
    module: str
    text: str
    is_package: bool = False
    module_origin: str = "path"
    provenance: dict = field(default_factory=dict)
    allow_incomplete: bool = False
    parse_error: str | None = field(default=None, init=False)
    tree: ast.Module = field(init=False, repr=False)
    lines: list[str] = field(init=False, repr=False)
    _encoded_lines: list[bytes] = field(init=False, repr=False)
    _segments: dict[ast.AST, str] = field(init=False, repr=False)
    _sha256: str = field(init=False, repr=False)

    def __post_init__(self):
        try:
            self.tree = ast.parse(self.text, filename=self.name)
        except SyntaxError as error:
            if not self.allow_incomplete:
                raise
            self.tree = ast.Module(body=[], type_ignores=[])
            self.parse_error = str(error)
        self.lines = source_lines(self.text, keepends=True)
        self._encoded_lines = [line.encode("utf-8") for line in self.lines]
        self._segments = {}
        self._sha256 = hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def span(self, node):
        if isinstance(node, ast.Module):
            end_line = max(1, len(self.lines))
            end_column = len(self.lines[-1].rstrip("\r\n")) if self.lines else 0
            return {"source": self.name, "line": 1, "column": 0, "end_line": end_line, "end_column": end_column}
        line, end_line = node.lineno, node.end_lineno or node.lineno

        def column(number, byte_offset):
            return len(self._encoded_lines[number - 1][:byte_offset].decode("utf-8"))

        return {
            "source": self.name,
            "line": line,
            "column": column(line, node.col_offset),
            "end_line": end_line,
            "end_column": column(end_line, node.end_col_offset or 0),
        }

    def segment(self, node):
        if node not in self._segments:
            positions = [getattr(node, field, None) for field in
                         ("lineno", "col_offset", "end_lineno", "end_col_offset")]
            value = None
            if all(position is not None for position in positions):
                first, column, last, end_column = positions
                if first == last:
                    value = self._encoded_lines[first - 1][column:end_column].decode("utf-8")
                else:
                    value = (self._encoded_lines[first - 1][column:].decode("utf-8")
                             + "".join(self.lines[first:last - 1])
                             + self._encoded_lines[last - 1][:end_column].decode("utf-8"))
            self._segments[node] = value or ast.unparse(node)
        return self._segments[node]

    @cached_property
    def syntax(self):
        return tuple(ast.walk(self.tree))

    @cached_property
    def imports(self):
        return tuple(n for n in self.syntax if isinstance(n, (ast.Import, ast.ImportFrom)))

    @cached_property
    def declarations(self):
        from .discovery import definitions

        return definitions(self)

    @cached_property
    def binding_names(self):
        """A conservative superset of keys the binding engine can establish."""
        names = set(self.module.split("."))
        for node in self.syntax:
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.arg):
                names.add(node.arg)
            elif isinstance(node, ast.alias):
                names.update(node.name.split("."))
                if node.asname:
                    names.add(node.asname)
            elif isinstance(node, ast.keyword) and node.arg:
                names.add(node.arg)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                names.update(node.value.split("."))
        return names

    @cached_property
    def declared_names(self):
        """Names that can begin a lexical address in this file (including locals)."""
        names = set(self.module.split("."))
        for node in self.syntax:
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                names.add(node.id)
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
                names.add(node.attr)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.arg):
                names.add(node.arg)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names.update(alias.asname or (alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name)
                             for alias in node.names)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                names.update(node.names)
            elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
                names.add(node.name)
            elif isinstance(node, ast.MatchMapping) and node.rest:
                names.add(node.rest)
        return names

    @cached_property
    def guards(self):
        return {
            "star": any(isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names) for n in self.syntax),
            "globals": {name for n in self.syntax if isinstance(n, ast.Global) for name in n.names},
            "dynamic": any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                           and n.name in {"__getattribute__", "__getattr__"} for n in self.syntax),
            "metaclass": any(isinstance(n, ast.ClassDef) and any(k.arg == "metaclass" for k in n.keywords)
                             for n in self.syntax),
            "dynamic_writes": any(isinstance(n, ast.Attribute) and isinstance(n.ctx, (ast.Store, ast.Del))
                                  and n.attr in {"__getattr__", "__getattribute__", "__new__"}
                                  for n in self.syntax),
        }

    @cached_property
    def positions(self):
        from .positions import Positions

        return Positions(self)

    def nodes_at(self, point):
        return self.positions.at(point)

    def identity(self):
        return {
            "source": self.name,
            "module": self.module,
            "module_origin": self.module_origin,
            "sha256": self._sha256,
            **self.provenance,
        }


@dataclass
class Symbol:
    name: str
    kind: str
    node: ast.AST
    source: Source
    parent: str | None
    owner: str | None = None
    decorators: tuple[str, ...] = ()

    def record(self):
        return dict(symbol=self.name, kind=self.kind, parent=self.parent, **self.source.span(self.node))


@dataclass(frozen=True)
class Value:
    """A binding, not a guess based on a matching attribute name.

    `identity` distinguishes object allocations; `receiver` carries method
    dispatch through aliases and inherited methods. Unknown values can retain
    proven import origins / candidate bindings without asserting a call edge.
    """

    kind: str
    target: str = ""
    receiver: str = ""
    identity: str = ""
    reason: str = ""
    candidates: tuple[str, ...] = ()
    items: tuple[Value, ...] = ()
    keys: tuple[str, ...] = ()
    environment: tuple[tuple[str, Value], ...] = ()

    @cached_property
    def _hash(self):
        return hash((self.kind, self.target, self.receiver, self.identity, self.reason,
                     self.candidates, self.items, self.keys, self.environment))

    def __hash__(self):
        return self._hash

    def __deepcopy__(self, _memo):
        # Every field is immutable, including nested values and closure tuples.
        # Query forks copy mutable states, not the facts those states refer to.
        return self


def unknown(reason, *values):
    candidates = set()
    for value in values:
        if value.target:
            candidates.add(value.target)
        candidates.update(value.candidates)
    return Value("unknown", reason=reason, candidates=tuple(sorted(candidates)))


UNKNOWN = unknown("binding is not statically known")


def merge(values, reason="different bindings across control-flow branches"):
    values = list(values)
    if not values:
        return UNKNOWN
    first = values[0]
    if all(value is first or value == first for value in values):
        return first
    # Different allocations with the same proven type still support method
    # lookup, but never share instance attribute state.
    if first.kind == "instance" and all(v.kind == "instance" and v.target == first.target for v in values):
        return Value("instance", first.target)
    return unknown(reason, *values)


@dataclass
class State:
    names: dict[str, Value] = field(default_factory=dict)
    attrs: dict[tuple[str, str], Value] = field(default_factory=dict)
    context: tuple[dict, ...] = ()
    control: str = ""

    def copy(self, context=None):
        return State(dict(self.names), dict(self.attrs), self.context if context is None else context, self.control)

    def __deepcopy__(self, memo):
        # Only Values occur in these two maps. Copy mutable containers, sharing
        # their immutable contents; context dictionaries still need isolation.
        result = State(dict(self.names), dict(self.attrs), deepcopy(self.context, memo), self.control)
        memo[id(self)] = result
        return result

    @classmethod
    def join(cls, states, context):
        live = [state for state in states if not state.control]
        if not live:
            result = states[0].copy(context)
            result.control = "terminated"
            return result
        if len(live) == 1:
            return live[0].copy(context)

        def joined(maps):
            if all(mapping == maps[0] for mapping in maps[1:]):
                return dict(maps[0])
            keys = set().union(*maps)
            return {k: merge(mapping.get(k, UNKNOWN) for mapping in maps) for k in keys}

        return cls(
            joined([s.names for s in live]),
            joined([s.attrs for s in live]),
            context,
        )
