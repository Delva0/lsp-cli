"""Multi-hop call/type relations: ty edges, Python C3 and flow continuations."""

import ast
from collections import deque
from types import SimpleNamespace

from .fast_sources import NeedsBindingsError
from .queries import query as flow_query
from .semantics import calls

OBJECT = SimpleNamespace(name="builtins.object")


def type_order(ws, entry, active=frozenset()):
    cache = getattr(ws, "type_orders", None)
    if cache is None:
        ws.type_orders = cache = {}
    if entry.identity in cache:
        return cache[entry.identity]
    failures = getattr(ws, "type_failures", None)
    if failures is None:
        ws.type_failures = failures = {}
    key = entry.identity, tuple(id(source) for source in ws.sources)
    if key in failures:
        raise ValueError(failures[key])
    try:
        result = _type_order(ws, entry, active)
    except ValueError as error:
        # A broad member query asks about several members of the same classes.
        # Reuse the same unresolved proof, but retry after inputs grow. Transport
        # failures are deliberately not cached.
        key = entry.identity, tuple(id(source) for source in ws.sources)
        failures[key] = str(error)
        raise
    cache[entry.identity] = result
    return result


def _type_order(ws, entry, active):
    if entry.name in active:
        raise ValueError("Cyclic base classes; no reliable MRO")
    if any(keyword.arg == "metaclass" for keyword in entry.node.keywords):
        raise ValueError("A custom metaclass can change MRO")
    bases, edges = [], []
    for expression in entry.node.bases:
        node = expression.value if isinstance(expression, ast.Subscript) else expression
        span = (
            ws.catalog_for(entry.source).selection(entry.source, node, node.attr, last=True)
            if isinstance(node, ast.Attribute)
            else entry.source.span(node)
        )
        found = [candidate for candidate in ws.definition(entry.source, span) if candidate.kind == "class"]
        if len(found) == 1:
            base = found[0]
        elif isinstance(node, ast.Name) and node.id == "object":
            items = ws.request("textDocument/prepareTypeHierarchy", ws.params(entry.source, entry.identifier)) or []
            supers = ws.request("typeHierarchy/supertypes", {"item": items[0]}) if len(items) == 1 else []
            if not any(item["name"] == "object" and item["uri"].endswith("/stdlib/builtins.pyi") for item in supers):
                raise ValueError("The explicit object base is unresolved")
            base = OBJECT
        else:
            raise ValueError(f"Base {entry.source.segment(expression)} is unresolved or outside the analysis input")
        bases.append(base)
        edges.append(
            {
                **entry.source.span(expression),
                "symbol": entry.name,
                "target": base.name,
                "relation": "inherits",
                "resolution": "resolved",
            },
        )
    if not bases:
        bases = [OBJECT]
    if len({base.name for base in bases}) != len(bases):
        raise ValueError("Duplicate base class; no valid MRO")
    orders = []
    for base in bases:
        if base is OBJECT:
            orders.append([base])
        else:
            order, inherited = type_order(ws, base, active | {entry.name})
            orders.append(list(order))
            edges.extend(inherited)
    orders.append(list(bases))
    result = [entry]
    while pending := [items for items in orders if items]:
        head = next(
            (
                items[0]
                for items in pending
                if all(items[0].name not in {value.name for value in other[1:]} for other in pending)
            ),
            None,
        )
        if head is None:
            raise ValueError("Inconsistent C3 ordering; no valid MRO")
        result.append(head)
        for items in pending:
            if items[0].name == head.name:
                items.pop(0)
    return result, edges


def imported_member(ws, source, scope, expression, modules, members):
    node = expression.value if isinstance(expression, ast.Subscript) else expression
    name = (
        node.id
        if isinstance(node, ast.Name)
        else (node.value.id if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) else None)
    )
    binding = ws.catalog_for(source).lookup(scope, name) if name else None
    if not binding or binding.kind != "import" or len(binding.declarations) != 1:
        return False
    imported = binding.node
    if isinstance(node, ast.Name) and isinstance(imported, ast.ImportFrom) and imported.module in modules:
        return any(a.name in members and (a.asname or a.name) == name for a in imported.names)
    if isinstance(node, ast.Attribute) and node.attr in members and isinstance(imported, ast.Import):
        return any(a.name in modules and (a.asname or a.name) == name for a in imported.names)
    return False


def abstract_member(ws, entry):
    """Recognize actual abc imports; a coincidentally named decorator is not proof."""
    return any(
        imported_member(
            ws,
            entry.source,
            entry.scope,
            node,
            {"abc"},
            {"abstractmethod", "abstractproperty", "abstractclassmethod", "abstractstaticmethod"},
        )
        for node in getattr(entry.node, "decorator_list", [])
    )


def protocol_member(ws, entry):
    parents = ws.lookup(entry.parent, exact=True) if entry.parent else []
    return bool(
        len(parents) == 1
        and parents[0].kind == "class"
        and any(
            imported_member(
                ws, parents[0].source, parents[0].scope, node, {"typing", "typing_extensions"}, {"Protocol"},
            )
            for node in parents[0].node.bases
        ),
    )


def method_edge(ws, child, base, *, engine="ty-lsp"):
    row = {
        **child.identifier,
        "symbol": child.name,
        "target": base.name,
        "relation": "overrides",
        "resolution": "resolved",
        "engine": engine,
        "statement": ws.catalog_for(child.source).header(child.source, child.node),
    }
    rows = [row]
    if (abstract_member(ws, base) or protocol_member(ws, base)) and not abstract_member(ws, child):
        rows.append({**row, "relation": "implements"})
    return rows


def known_ancestors(ws, origin, max_depth):
    pending, entries, rows = deque([(origin, 0)]), {}, []
    while pending:
        current, depth = pending.popleft()
        if current.identity in entries:
            continue
        entries[current.identity] = current
        if depth >= max_depth:
            if current.node.bases:
                ws.diagnostics.append({"kind": "incomplete", "message": "Ancestor depth limit reached"})
            continue
        for expression in current.node.bases:
            node = expression.value if isinstance(expression, ast.Subscript) else expression
            span = (
                ws.catalog_for(current.source).selection(current.source, node, node.attr, last=True)
                if isinstance(node, ast.Attribute)
                else current.source.span(node)
            )
            targets = [e for e in ws.definition(current.source, span) if e.kind == "class"]
            if len(targets) == 1:
                base = targets[0]
                rows.append(
                    {
                        **current.source.span(expression),
                        "symbol": current.name,
                        "target": base.name,
                        "relation": "inherits",
                        "resolution": "resolved",
                    },
                )
                pending.append((base, depth + 1))
    return list(entries.values()), rows


def inheritance(ws, entry, max_depth):
    rows, mro, reason = [], None, None
    origin = entry
    if entry.identity in ws.receivers:
        parents = ws.lookup(ws.receivers[entry.identity], exact=True)
        origin = parents[0] if len(parents) == 1 else entry
    elif entry.kind == "method":
        parents = ws.lookup(entry.parent, exact=True)
        origin = parents[0] if len(parents) == 1 else entry
    if origin.kind != "class":
        raise ValueError("inheritance requires a class or method")
    try:
        order, edges = type_order(ws, origin)
        mro = [item.name for item in order]
        rows.extend(edges)
    except (ValueError, NeedsBindingsError) as error:
        reason = str(error)
        try:
            # Computing this class's MRO needs its bases, not every importer.
            # Descendants are queried independently through type hierarchy below.
            analysis = ws.analysis([origin.name])
            inherited = flow_query(analysis, "inherits", [origin.name])["results"][0]
            if inherited["mro"] and "metaclass" not in reason:
                mro, reason = inherited["mro"], None
                ws.diagnostics.append({"kind": "lsp_fallback", "message": str(error) + "; MRO from python-flow"})
        except (ValueError, KeyError, RecursionError):
            pass
    descendants = {origin.name: 0}
    try:
        ancestors, edges = known_ancestors(ws, origin, max_depth)
        rows.extend(edges)
        items = ws.request("textDocument/prepareTypeHierarchy", ws.params(origin.source, origin.identifier)) or []
        pending, visited = deque((item, 0) for item in items), set()
        while pending:
            item, depth = pending.popleft()
            key = (item["uri"], str(item["selectionRange"]))
            if key in visited:
                continue
            visited.add(key)
            children = ws.request("typeHierarchy/subtypes", {"item": item}) or []
            if depth >= max_depth:
                if children:
                    ws.diagnostics.append({"kind": "incomplete", "message": "Descendant depth limit reached"})
                continue
            for child in children:
                located = ws.location(child["uri"], child["selectionRange"])
                parent = ws.location(item["uri"], item["selectionRange"])
                candidates = ws.catalog_for(located[0]).at(*located) if located else []
                bases = ws.catalog_for(parent[0]).at(*parent) if parent else []
                if candidates and bases:
                    child_entry = candidates[0]
                    descendants[child_entry.name] = min(depth + 1, descendants.get(child_entry.name, depth + 1))
                    rows.append(
                        {
                            **child_entry.identifier,
                            "symbol": child_entry.name,
                            "target": bases[0].name,
                            "relation": "inherits",
                            "resolution": "resolved",
                            "statement": ws.catalog_for(child_entry.source).header(
                                child_entry.source, child_entry.node,
                            ),
                        },
                    )
                pending.append((child, depth + 1))
        # Prove member relations through implementation requests, including
        # outgoing overrides when an external ancestor prevents complete C3 MRO.
        ancestor_names = {a.name for a in ancestors}
        relevant = ancestor_names | set(descendants)
        members = ancestors if entry.kind == "class" else []
        leaf = entry.name.rsplit(".", 1)[-1]
        members.extend(
            e
            for source in {e.source.name: e.source for name in relevant for e in ws.lookup(name, exact=True)}.values()
            for e in ws.catalog_for(source).entries
            if e.kind == "method"
            and e.parent in relevant
            and (entry.kind == "class" or e.name.rsplit(".", 1)[-1] == leaf)
        )
        for member in members:
            implementations = (
                ws.request("textDocument/implementation", ws.params(member.source, member.identifier)) or []
            )
            for implementation in implementations:
                located = ws.location(
                    implementation.get("uri", implementation.get("targetUri")),
                    implementation.get("range", implementation.get("targetSelectionRange")),
                )
                for candidate in ws.catalog_for(located[0]).at(*located) if located else []:
                    if candidate.name == member.name or candidate.kind != member.kind:
                        continue
                    owner = candidate.name if candidate.kind == "class" else candidate.parent
                    if owner not in descendants:
                        continue
                    if member.kind == "method":
                        rows.extend(method_edge(ws, candidate, member))
                    else:
                        rows.append(
                            {
                                **candidate.identifier,
                                "symbol": candidate.name,
                                "statement": ws.catalog_for(candidate.source).header(candidate.source, candidate.node),
                                "target": member.name,
                                "relation": "implements",
                                "resolution": "resolved",
                                "engine": "ty-lsp",
                            },
                        )
    except NeedsBindingsError as error:
        ws.diagnostics.append({"kind": "lsp_fallback", "message": str(error)})
        analysis = ws.analysis([origin.name], reverse=True)
        inherited = flow_query(analysis, "inherits", [origin.name])["results"][0]
        infos = {
            name: flow_query(analysis, "inherits", [name])["results"][0]
            for name in {origin.name, *(s["symbol"] for s in inherited["subclasses"]), *(mro or [])}
            if name in analysis.symbols and analysis.symbols[name].kind == "class"
        }
        for _ in range(max_depth):
            children = {
                name for name, info in infos.items() if any(base.get("target") in descendants for base in info["bases"])
            }
            new = children - set(descendants)
            if not new:
                break
            descendants.update(
                {
                    name: min(
                        descendants[b["target"]] + 1 for b in infos[name]["bases"] if b.get("target") in descendants
                    )
                    for name in new
                },
            )
        if any(name not in descendants for name in infos if name not in (mro or [])):
            ws.diagnostics.append({"kind": "incomplete", "message": "Descendant depth limit reached"})
        for name, info in infos.items():
            if name not in descendants and name not in (mro or []):
                continue
            rows.extend(
                {**base, "symbol": name, "relation": "inherits"} for base in info["bases"] if base.get("target")
            )
            if name not in descendants or not info["mro"]:
                continue
            if entry.kind == "class":
                classes = ws.lookup(name, exact=True)
                if len(classes) == 1:
                    cls = classes[0]
                    rows.extend(
                        {
                            **cls.identifier,
                            "symbol": name,
                            "target": base,
                            "relation": "implements",
                            "resolution": "resolved",
                            "engine": "python-flow",
                            "statement": ws.catalog_for(cls.source).header(cls.source, cls.node),
                        }
                        for base in info["mro"][1:]
                        if base in infos
                    )
            members = [
                e
                for cls in ws.lookup(name, exact=True)
                for e in ws.catalog_for(cls.source).entries
                if e.parent == name
                and e.kind == "method"
                and (entry.kind == "class" or e.name.rsplit(".", 1)[-1] == entry.name.rsplit(".", 1)[-1])
            ]
            for member in members:
                for base in info["mro"][1:]:
                    targets = ws.lookup(base + "." + member.name.rsplit(".", 1)[-1], exact=True)
                    if len(targets) == 1:
                        rows.extend(method_edge(ws, member, targets[0], engine="python-flow"))
    unique = {(r["source"], r["symbol"], r["target"], r["relation"]): r for r in rows}
    ordered = sorted(unique.values(), key=lambda r: (r["source"], r["line"], r["column"], r["relation"], r["target"]))
    known = {origin.name}
    while True:
        parents = known | {r["target"] for r in ordered if r["relation"] == "inherits" and r["symbol"] in known}
        if parents == known:
            break
        known = parents
    result = ws.result("inheritance", ordered)
    result.update(
        symbol=entry.record(),
        mro=mro,
        resolution="resolved" if mro else "unresolved",
        reason=reason,
        ancestors=mro[1:] if mro else sorted(known - {origin.name}),
        descendants=sorted(set(descendants) - {origin.name}),
        overrides=[r for r in ordered if r["relation"] == "overrides"],
        implementations=[r for r in ordered if r["relation"] == "implements"],
        max_depth=max_depth,
        truncated=any(d["kind"] == "incomplete" and "depth limit" in d["message"] for d in ws.diagnostics),
    )
    return result


def path(ws, start, destination, max_depth):
    return paths(ws, start, [(destination, ws.receivers.get(destination.identity, ""))], max_depth)[0]


def paths(ws, start, destinations, max_depth):
    """One native traversal per start; at most one shared flow traversal as fallback."""
    remaining = {(entry.identity, receiver): entry for entry, receiver in destinations}
    keys = list(remaining)
    found = {}
    receiver = ws.receivers.get(start.identity, "")
    pending = deque([(start, receiver, [])])
    visited, frontier, limited = set(), [], False
    edge_cache = getattr(ws, "path_edges", None)
    if edge_cache is None:
        ws.path_edges = edge_cache = {}
    flow_results = {}

    def flow_search():
        if flow_results:
            return
        from .queries import paths as flow_paths

        analysis = ws.analysis([start.name, *(entry.name for entry in remaining.values())])
        source, inherited = analysis.select(receiver + "." + start.name.rsplit(".", 1)[-1] if receiver else start.name)
        unresolved = list(remaining.items())
        targets = [analysis.select(context + "." + entry.name.rsplit(".", 1)[-1] if context else entry.name)
                   for (_identity, context), entry in unresolved]
        fallback = flow_paths(analysis, source, targets, inherited, max_depth, {"diagnostics": []})
        for source in analysis.sources:
            ws.loaded.setdefault(source.provenance.get("git_path", source.name), source)
        for (key, entry), row in zip(unresolved, fallback, strict=True):
            result = ws.result("path", row["results"])
            result.update(status=row["status"], destination=entry.record(), destination_receiver=key[1] or None,
                          visited=row["visited"], unresolved_frontier=row.get("unresolved_frontier", frontier))
            result["diagnostics"].extend(row.get("diagnostics", []))
            result["backend"]["fallback"] = "python-flow"
            flow_results[key] = result

    def reached(entry, actual_receiver, steps):
        for key in list(remaining):
            identity, wanted_receiver = key
            if identity == entry.identity and (not wanted_receiver or wanted_receiver == actual_receiver):
                found[key] = ws.result("path", steps)
                found[key].update(status="found", visited=len(visited), destination=remaining.pop(key).record(),
                                  destination_receiver=wanted_receiver or None)

    while pending and remaining:
        current, current_receiver, steps = pending.popleft()
        key = current.identity, current_receiver
        if key in visited:
            continue
        visited.add(key)
        reached(current, current_receiver, steps)
        if not remaining:
            break
        if len(steps) >= max_depth or len(visited) >= ws.args.max_work:
            limited = True
            continue
        if current.kind not in {"function", "method", "module"}:
            continue
        if key not in edge_cache:
            if current_receiver:
                ws.receivers[current.identity] = current_receiver
            else:
                ws.receivers.pop(current.identity, None)
            edge_cache[key] = calls(ws, current, fallback=False)
        direct_candidate = False
        next_steps = []
        for event in edge_cache[key]:
            if event.get("unreachable") or event["scope"] != current.name:
                continue
            if event["resolution"] != "resolved":
                frontier.append(event)
                direct_candidate |= not steps and any(
                    event.get("callee", "").rsplit(".", 1)[-1] == dest.name.rsplit(".", 1)[-1]
                    for dest in remaining.values()
                )
                continue
            candidates = ws.lookup(event["target"], exact=True)
            if len(candidates) != 1:
                continue
            target = candidates[0]
            edge = {**event, "caller": current.name}
            target_receiver = event.get("receiver", "")
            reached(target, target_receiver, [*steps, edge])
            next_steps.append((target, target_receiver, [*steps, edge]))
        if remaining and direct_candidate:
            # A spelling match is only a scheduling hint, never an edge. Prove
            # the candidate now with full binding/argument flow before expanding
            # unrelated native branches; if it fails, native traversal continues.
            flow_search()
            for key in list(remaining):
                if flow_results[key]["status"] == "found":
                    found[key] = flow_results[key]
                    remaining.pop(key)
        if remaining:
            for target, target_receiver, continuation in next_steps:
                callee = target
                if target.kind == "class":
                    try:
                        callee = ws.select(target.name + ".__init__")
                    except ValueError:
                        continue
                pending.append((callee, target_receiver, continuation))
    if remaining:
        # Argument, closure and receiver continuations require flow analysis.
        flow_search()
        for key in remaining:
            result = flow_results[key]
            if (not result["coverage"]["complete"] or limited) and result["status"] != "found":
                result["status"] = "incomplete"
            found[key] = result
    for result in found.values():
        result["symbol"] = start.record()
        result["receiver"] = receiver or None
    return [found[key] for key in keys]
