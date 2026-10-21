"""Queries over call occurrences, never over pyan's broader uses edges."""

from __future__ import annotations

from collections import deque

from .model import Value
from .source_view import show


def call_edges(summary):
    for event in summary.events:
        if event["kind"] != "CALL" or event.get("unreachable"):
            continue
        # A generator expression body is deferred until iteration. A source
        # occurrence remains visible, but is not an eager call-path edge.
        if any(c["kind"] == "comprehension" and c["label"] == "deferred" for c in event["context"]):
            continue
        yield event


def query(analysis, command, symbols=(), *, line=None, column=None, receiver="", max_depth=32):
    result = {
        "schema_version": 1,
        "command": command,
        "sources": [s.identity() for s in analysis.sources],
        "results": [],
        "diagnostics": list(analysis.diagnostics),
    }
    if command == "outline":
        result["results"] = [symbol.record() for symbol in analysis.symbols.values()]
        return result
    if command == "where":
        if line is None or line < 1 or (column is not None and column < 0):
            raise ValueError("where requires --line >= 1 and --column >= 0 (when supplied)")
        result["position"] = {"line": line, "column": column}
        for source in analysis.sources:
            # Symbols are collected in lexical preorder. Keep every containing
            # definition, rather than selecting a leaf and hiding its parents.
            for symbol in analysis.symbols.values():
                if symbol.source is not source:
                    continue
                span = symbol.record()
                for decorator in getattr(symbol.node, "decorator_list", []):
                    start = source.span(decorator)
                    position = (start["line"], max(0, start["column"] - 1))  # Include the @.
                    span["line"], span["column"] = min(position, (span["line"], span["column"]))
                if not span["line"] <= line <= span["end_line"]:
                    continue
                if column is not None and not (span["line"], span["column"]) <= (line, column) < (
                    span["end_line"],
                    span["end_column"],
                ):
                    continue
                result["results"].append(span)
        return result
    if command == "refs":
        return references(analysis, symbols[0], result)
    symbol, inherited_receiver = analysis.select(symbols[0])
    selected_receiver = inherited_receiver
    if receiver:
        cls, _ = analysis.select(receiver)
        if cls.kind != "class" or not symbol.owner or symbol.owner not in (analysis.mro(cls.name) or []):
            raise ValueError("--receiver must be a class whose MRO contains the method's defining class")
        selected_receiver = cls.name
    result["symbol"] = symbol.record()
    if command == "defs":
        result["results"] = [
            s.record()
            for s in analysis.symbols.values()
            if s.source is symbol.source and (s.name == symbol.name or s.name.startswith(symbol.name + "."))
        ]
        return result
    if command == "show":
        result["results"] = [show(symbol, selected_receiver)]
        # Put readable source ahead of the input provenance in this new view.
        keys = ("schema_version", "command", "symbol", "results", "sources", "diagnostics")
        return {key: result[key] for key in keys}
    if command in {"trace", "callee"}:
        summary = analysis.summary(symbol, selected_receiver)
        result["results"] = summary.events if command == "trace" else [e for e in summary.events if e["kind"] == "CALL"]
    elif command == "callers":
        records = {}
        for owner, summary in analysis.all_summaries():
            for event in call_edges(summary):
                if event.get("target") == symbol.name or event.get("constructor", {}).get("target") == symbol.name:
                    key = (event["source"], event["line"], event["column"], event["target"], event.get("receiver"))
                    records[key] = dict(event, caller=owner.name)
        result["results"] = list(records.values())
    elif command == "inherits":
        if symbol.kind != "class":
            raise ValueError("inherits requires a class symbol")
        order = analysis.mro(symbol.name)
        bases = analysis.base_bindings(symbol)
        descendants = [
            s.record()
            for s in analysis.symbols.values()
            if s.kind == "class" and s.name != symbol.name and symbol.name in (analysis.mro(s.name) or [])
        ]
        result["results"] = [
            {
                **symbol.record(),
                "resolution": "resolved" if order else "unresolved",
                "bases": [
                    {**analysis.describe(base), **symbol.source.span(node)}
                    for base, node in zip(bases, symbol.node.bases, strict=True)
                ],
                "mro": order,
                "mro_engine": analysis.mro_engines.get(symbol.name),
                "reason": analysis.mro_errors.get(symbol.name),
                "subclasses": descendants,
            },
        ]
    elif command == "path":
        destination, dest_receiver = analysis.select(symbols[1])
        return path(analysis, symbol, destination, selected_receiver, dest_receiver, max_depth, result)
    result["diagnostics"] = list(analysis.diagnostics)
    return result


def references(analysis, text, result):
    refs = [ref for _, summary in analysis.all_summaries() for ref in summary.refs]
    try:
        symbol, _ = analysis.select(text)
    except ValueError as error:
        if "ambiguous" in str(error):
            raise
        symbol = None
    if symbol:
        result["symbol"] = symbol.record()
        result["results"] = [r for r in refs if r["target"] == symbol.name or r["binding"] == symbol.name]
    else:
        bindings = {
            r["binding"]
            for r in refs
            if r["expression"] == text or r["binding"] == text or r.get("import_target") == text
        }
        result["results"] = [r for r in refs if r["binding"] in bindings]
        result["bindings"] = sorted(bindings)
    # Multiple receiver contexts may visit the same source occurrence.
    unique = {}
    for ref in result["results"]:
        key = (
            ref["source"],
            ref["line"],
            ref["column"],
            ref["end_line"],
            ref["end_column"],
            ref["role"],
            ref["target"],
        )
        unique[key] = ref
    result["results"] = sorted(unique.values(), key=lambda r: (r["source"], r["line"], r["column"], r["role"]))
    result["diagnostics"] = list(analysis.diagnostics)
    return result


def path(analysis, start, destination, receiver, dest_receiver, max_depth, result):
    return paths(analysis, start, [(destination, dest_receiver)], receiver, max_depth, result)[0]


def paths(analysis, start, destinations, receiver, max_depth, template):
    """One context-sensitive BFS from a start to every requested destination."""
    if max_depth < 1:
        raise ValueError("--max-depth must be positive")
    remaining = set(range(len(destinations)))
    by_name = {}
    for i, (symbol, _) in enumerate(destinations):
        by_name.setdefault(symbol.name, []).append(i)
    results = {}
    queue = deque([(start.name, receiver, {}, {}, {}, [])])
    seen, frontier, limited = set(), [], False

    def reached(name, current_receiver, steps, constructor=False):
        for i in by_name.get(name, []):
            _, wanted_receiver = destinations[i]
            if i in remaining and (constructor or not wanted_receiver or current_receiver == wanted_receiver):
                results[i] = dict(
                    template, status="found", results=steps, destination=destinations[i][0].record(), visited=len(seen),
                )
                remaining.remove(i)

    while queue and remaining:
        name, current_receiver, arguments, closure, attributes, steps = queue.popleft()
        key = (
            name,
            current_receiver,
            tuple(sorted(arguments.items())),
            tuple(sorted(closure.items())),
            tuple(sorted(attributes.items())),
        )
        if key in seen:
            continue
        seen.add(key)
        reached(name, current_receiver, steps)
        if not remaining:
            break
        if len(steps) >= max_depth or len(seen) >= 4096:
            limited = True
            continue
        symbol = analysis.symbols[name]
        summary = (
            analysis.function(name, current_receiver, arguments, closure, attributes)
            if symbol.kind not in {"class", "module"}
            else analysis.summary(symbol)
        )
        for event in call_edges(summary):
            if event["resolution"] != "resolved":
                frontier.append(dict(event, caller=name))
                continue
            target = event["target"]
            if target in analysis.symbols:
                if analysis.symbols[target].kind == "class":
                    reached(target, event.get("receiver", ""), [*steps, dict(event, caller=name)], True)
                context = summary.calls.get(event["order"], (target, event.get("receiver", ""), {}, {}, {}))
                if analysis.symbols[target].kind == "class":
                    init = analysis.member(Value("instance", target), "__init__")
                    if init.target in analysis.symbols:
                        target = init.target
                        context = summary.calls.get(event["order"], (target, init.receiver, {}, {}, {}))
                edge = dict(event, caller=name, target=target)
                queue.append((*context, [*steps, edge]))
    unique_frontier = {}
    for event in frontier:
        key = (event["source"], event["line"], event["column"], event["scope"], event.get("reason"))
        unique_frontier.setdefault(key, event)
    for i in remaining:
        results[i] = dict(
            template,
            status="incomplete" if limited else "no_resolved_path",
            results=[],
            destination=destinations[i][0].record(),
            unresolved_frontier=list(unique_frontier.values()),
            visited=len(seen),
            diagnostics=list(template.get("diagnostics", [])),
        )
        if limited:
            results[i]["diagnostics"].append({"kind": "limit", "message": "path depth or 4096-context budget reached"})
    return [results[i] for i in range(len(destinations))]
