"""Adapt native LSP relationships to versioned CLI records and original spans."""

import ast
import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

from .fast_sources import Definitions, NeedsBindingsError, collect


def character(text, utf16):
    """Convert LSP UTF-16 positions to the CLI's Unicode character columns."""
    return len(text.encode("utf-16-le")[:utf16 * 2].decode("utf-16-le"))


def position(symbol):
    node, source = symbol.node, symbol.source
    if isinstance(node, ast.Module):
        raise NeedsBindingsError("a module has no call hierarchy item")
    row = source.lines[node.lineno - 1]
    start = source.span(node)["column"]
    name = re.search(r"(?:async\s+)?(?:def|class)\s+(\w+)", row[start:])
    if not name:
        raise NeedsBindingsError("no declaration token at the selected location")
    column = start + name.start(1)
    return {"line": node.lineno - 1, "character": len(row[:column].encode("utf-16-le")) // 2}


class Native:
    def __init__(self, loader, session):
        self.loader, self.session = loader, session
        self.root = None
        self.definitions = {}
        self.api_calls = []

    def request(self, method, params):
        response = self.session.native(self.loader, method, params)
        self.api_calls.append(method)
        if "error" in response:
            raise NeedsBindingsError("LSP " + method + ": " + response["error"]["message"])
        self.root = Path(unquote(urlsplit(response["commit"]["rootUri"]).path))
        return response["result"] or []

    def params(self, symbol):
        path = Path(self.loader.repository) / symbol.source.provenance["git_path"]
        return {"textDocument": {"uri": path.as_uri()}, "position": position(symbol)}

    def source(self, uri):
        parsed = urlsplit(uri)
        if parsed.scheme != "file":
            return None
        path = Path(unquote(parsed.path))
        if not path.is_relative_to(self.root):
            return None
        relative = path.relative_to(self.root).as_posix()
        return self.loader.source(relative) if relative in self.loader.paths else None

    def symbols(self, source):
        if source.name not in self.definitions:
            self.definitions[source.name] = Definitions([source])
        return self.definitions[source.name]

    def span(self, source, value):
        start, end = value["start"], value["end"]
        lines = [*source.lines, ""]
        return {"source": source.name, "line": start["line"] + 1,
                "column": character(lines[start["line"]], start["character"]),
                "end_line": end["line"] + 1,
                "end_column": character(lines[end["line"]], end["character"])}

    def symbol_at(self, source, value):
        span = self.span(source, value)
        point = (span["line"], span["column"])
        candidates = [s for s in self.symbols(source).symbols.values()
                      if contains(s.record(), point)]
        return max(candidates, key=lambda s: (s.record()["line"], s.record()["column"], len(s.name)))

    def item_symbol(self, item):
        source = self.source(item["uri"])
        if source:
            return self.symbol_at(source, item["selectionRange"])
        path = Path(unquote(urlsplit(item["uri"]).path))
        if item.get("detail") == "builtins" and path.name == "builtins.pyi" and path.parent.name == "stdlib":
            # The server resolved this occurrence to its typeshed declaration;
            # a same-named local definition takes the repository branch above.
            return SimpleNamespace(name="builtins." + item["name"])
        return None

    def call_at(self, source, value):
        span = self.span(source, value)
        point = (span["line"], span["column"])
        calls = [node for node in ast.walk(source.tree) if isinstance(node, ast.Call)
                 and contains(source.span(node.func), point)]
        return min(calls, key=lambda n: len(source.segment(n.func))) if calls else None

    def call(self, source, node, target, scope):
        return {"kind": "CALL", **source.span(node), "scope": scope.name,
                "callee": source.segment(node.func), "text": source.segment(node),
                "context": contexts(source, scope.node, node),
                "target": target.name if target else None,
                "resolution": "resolved" if target else "unresolved",
                "reason": None if target else "LSP did not establish one target inside the selected revision",
                "engine": "ty-lsp"}


def contains(span, point):
    return (span["line"], span["column"]) <= point < (span["end_line"], span["end_column"])


def contexts(source, root, target):
    """Preserve lexical control nesting; execution-sensitive traces use the flow engine."""
    cache = getattr(source, "_context_indices", None)
    if cache is None:
        source._context_indices = cache = {}
    if id(root) in cache:
        return [dict(part) for part in cache[id(root)].get(id(target), ())]
    indexed = {}
    pending = [(root, ())]
    while pending:
        node, stack = pending.pop()
        # Some AST operator/context nodes are shared. Preserve the old walk's
        # first occurrence rather than overwriting it with the last occurrence.
        if id(node) in indexed:
            continue
        indexed[id(node)] = stack
        children = []
        for field, value in ast.iter_fields(node):
            nested = stack
            kind, label = None, ""
            if isinstance(node, ast.If):
                kind = {"body": "if", "orelse": "else", "test": "if_test"}.get(field)
                label = source.segment(node.test)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                kind = {"body": "loop", "orelse": "loop_else"}.get(field)
                label = source.segment(node.test if isinstance(node, ast.While) else node.iter)
            elif isinstance(node, (ast.Try, ast.TryStar)):
                kind = {"body": "try", "orelse": "try_else", "finalbody": "finally"}.get(field)
            elif isinstance(node, ast.ExceptHandler) and field == "body":
                kind, label = "except", source.segment(node.type) if node.type else "*"
            elif isinstance(node, (ast.With, ast.AsyncWith)) and field == "body":
                kind = "async_with" if isinstance(node, ast.AsyncWith) else "with"
            if kind:
                nested = (*stack, {"kind": kind, "label": label, **source.span(node)})
            children.extend((child, nested) for child in (value if isinstance(value, list) else [value])
                            if isinstance(child, ast.AST))
        pending.extend(reversed(children))
    cache[id(root)] = indexed
    return [dict(part) for part in indexed.get(id(target), ())]


def relationship(args, loader, session):
    if args.import_root:
        raise NeedsBindingsError("explicit import roots require the binding engine's source scope")
    definitions, metadata, diagnostics = collect(args, loader)
    symbol, _ = definitions.select(args.symbols[0])
    if symbol.kind in {"module", "class"}:
        raise NeedsBindingsError("module/class execution uses the binding engine")
    native = Native(loader, session)
    records = []
    if args.command == "refs":
        locations = native.request("textDocument/references", {
            **native.params(symbol), "context": {"includeDeclaration": True}})
        for location in locations:
            source = native.source(location["uri"])
            if source is None:
                continue
            span = native.span(source, location["range"])
            scope = native.symbol_at(source, location["range"])
            point = (span["line"], span["column"])
            role, text = "read", source.lines[span["line"] - 1][span["column"]:span["end_column"]]
            matches = [node for node in ast.walk(source.tree)
                       if hasattr(node, "lineno") and contains(source.span(node), point)]
            assignment = None
            for node in matches:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    role = "import"
                elif isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, (ast.Store, ast.Del)):
                    role = "write"
                elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    assignment = node
            expressions = [node for node in matches if isinstance(node, (ast.Name, ast.Attribute))]
            if expressions:
                expression = min(expressions, key=lambda node: len(source.segment(node)))
                span, text = source.span(expression), source.segment(expression)
            if source is symbol.source and point == (symbol.record()["line"],
                    character(source.lines[symbol.node.lineno - 1], position(symbol)["character"])):
                role = "definition"
            row = {"kind": "REF", **span, "scope": scope.name, "expression": text, "text": text,
                   "role": role, "target": symbol.name, "binding": symbol.name,
                   "resolution": "resolved", "engine": "ty-lsp",
                   "context": contexts(source, scope.node, matches[-1]) if matches else []}
            if role == "write" and assignment is not None:
                row["value"] = source.segment(assignment.value) if assignment.value else None
                row["text"] = source.segment(assignment)
            records.append(row)
    else:
        items = native.request("textDocument/prepareCallHierarchy", native.params(symbol))
        if len(items) != 1:
            raise NeedsBindingsError("LSP did not return one call hierarchy item")
        incoming = args.command == "callers"
        links = native.request("callHierarchy/incomingCalls" if incoming else "callHierarchy/outgoingCalls",
                               {"item": items[0]})
        known = {}
        for link in links:
            other = native.item_symbol(link["from" if incoming else "to"])
            source = native.source(link["from"]["uri"]) if incoming else symbol.source
            if source is None:
                continue
            for occurrence in link["fromRanges"]:
                node = native.call_at(source, occurrence)
                if node is None:
                    continue
                owner = native.symbol_at(source, occurrence) if incoming else symbol
                row = native.call(source, node, symbol if incoming else other, owner)
                if incoming:
                    row["caller"] = owner.name
                key = (source.name, id(node))
                if key in known and known[key]["target"] != row["target"]:
                    row.update(target=None, resolution="unresolved", reason="LSP returned multiple possible callees")
                known[key] = row
        if not incoming:
            for node in own_calls(symbol.node):
                if isinstance(node, ast.Call):
                    known.setdefault((symbol.source.name, id(node)), native.call(symbol.source, node, None, symbol))
        records = list(known.values())
    records.sort(key=lambda row: (row["source"], row["line"], row["column"], row["end_line"], row["end_column"]))
    for index, row in enumerate(records):
        row["order"] = index
    # Native empty results describe this server's static coverage, never runtime absence.
    diagnostics = [*diagnostics, {"kind": "lsp_scope", "message":
        "ty LSP static relationships in the selected commit; "
        "dynamic dispatch and external dependencies may remain unresolved."}]
    if not records:
        diagnostics.append({"kind": "unresolved", "message":
                            "LSP returned no static relationships; "
                            "this does not establish that no runtime relationships exist."})
    if loader.omitted:
        diagnostics.append({"kind": "source_limit",
                            "message": "Some LSP locations exceed --max-files; results are incomplete."})
    metadata.update(scope="lsp_workspace", parsed_files=len(loader.parsed), read_files=len(loader.raw),
                    analyzed_files=len(loader.parsed), complete=False, omitted_files=sorted(loader.omitted))
    return {"schema_version": 1, "command": args.command, "symbol": symbol.record(), "results": records,
            "sources": [loader.parsed[path].identity() for path in sorted(loader.parsed)],
            "diagnostics": diagnostics, "git": metadata,
            "backend": {"name": "ty-lsp", "version": "0.0.84", "methods": native.api_calls, "coverage": "static"}}


def own_calls(node):
    for statement in node.body:
        if isinstance(statement, ast.Call):
            yield statement
        if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield from own_nodes(statement)


def own_nodes(node):
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            yield child
            yield from own_nodes(child)
