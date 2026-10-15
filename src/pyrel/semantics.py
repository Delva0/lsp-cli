"""Source occurrences selected by meaning, with ty proofs and flow fallback."""

from __future__ import annotations

import ast

from .fast_sources import NeedsBindingsError
from .lsp_queries import contains, contexts
from .queries import query as flow_query

ROLES = ("call", "read", "write", "import", "assign", "attr_write", "subscript_write", "return", "raise", "doc")
REF_ROLES = ("call", "read", "write", "import", "inherit", "implement", "override")
WRITE = {"ASSIGN", "ATTR_WRITE", "SUBSCRIPT_WRITE"}


def site_key(row):
    return tuple(row[k] for k in ("source", "line", "column", "end_line", "end_column"))


def statement(catalog, node):
    while not isinstance(node, ast.stmt) and id(node) in catalog.parents:
        node = catalog.parents[id(node)]
    return node


def selected(row, filters):
    if not filters:
        return True
    tags = set(row.get("roles", []))
    kind = row.get("kind", "").lower()
    tags.add(kind)
    if row.get("kind") in WRITE:
        tags.add("write")
    if kind == "call":
        tags.add("read")
    return bool(tags.intersection(filters))


def dead(catalog, node):
    """Prove simple unreachable syntax without interpreting user expressions."""
    child = node
    while id(child) in catalog.parents:
        parent = catalog.parents[id(child)]
        if isinstance(parent, (ast.If, ast.While)) and isinstance(parent.test, ast.Constant):
            if child in parent.body and not parent.test.value:
                return True
            if isinstance(parent, ast.If) and child in parent.orelse and parent.test.value:
                return True
        for _field, value in ast.iter_fields(parent):
            if isinstance(value, list) and child in value:
                previous = value[: value.index(child)]
                if any(isinstance(item, (ast.Return, ast.Raise, ast.Break, ast.Continue)) for item in previous):
                    return True
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            break
        child = parent
    return False


def occurrence(ws, source, node, kind, **extra):
    catalog = ws.catalog
    scope = catalog.node_scopes.get(id(node), catalog.roots[source.name])
    record = {
        "kind": kind,
        **source.span(node),
        "scope": scope.name,
        "text": source.segment(node),
        "context": contexts(source, scope.node, node),
        **extra,
    }
    owner = statement(catalog, node)
    record["statement"] = catalog.header(source, owner)
    if dead(catalog, node):
        record["unreachable"] = True
    return record


def target(ws, item):
    located = ws.location(item["uri"], item.get("selectionRange", item["range"]))
    if located:
        entries = ws.catalog.at(*located)
        return entries[0].name if len(entries) == 1 else None
    # This proof is a server-returned typeshed declaration, never a name guess.
    if item.get("detail") == "builtins" and item["uri"].endswith("/stdlib/builtins.pyi"):
        return "builtins." + item["name"]
    return None


def call_node(source, span):
    point = (span["line"], span["column"])
    candidates = [
        node for node in source.nodes_at(point)
        if isinstance(node, ast.Call) and contains(source.span(node.func), point)
    ]
    end = (span.get("end_line", span["line"]), span.get("end_column", span["column"]))
    covered = [
        node for node in candidates if (source.span(node.func)["end_line"], source.span(node.func)["end_column"]) <= end
    ]
    if covered:
        return max(covered, key=lambda n: len(source.segment(n.func)))
    return min(candidates, key=lambda n: len(source.segment(n.func))) if candidates else None


def receiver_order(ws, source, node, rows):
    """Prove a lexical receiver before narrowing dynamic-attribute checks.

    Arbitrary typed expressions, factories, aliases and conditional assignments
    keep flow validation. Here only the method receiver or one direct local
    constructor assignment can establish the class.
    """
    from .effects import preserves_receiver
    from .relations import type_order

    catalog = ws.catalog
    scope = catalog.node_scopes[id(node)]
    if scope.kind != "function" or isinstance(scope.node, ast.Lambda):
        return None
    loops = []
    ancestor = catalog.parents.get(id(node))
    while ancestor is not None and ancestor is not scope.node:
        if isinstance(ancestor, (ast.For, ast.AsyncFor, ast.While)):
            # Effects later in a loop body can precede this call on the next turn.
            loops.append(ancestor)
        ancestor = catalog.parents.get(id(ancestor))
    receiver = node.func.value
    parameters = [*scope.node.args.posonlyargs, *scope.node.args.args]
    if isinstance(receiver, ast.Call) and scope.parent and scope.parent.kind == "class" and parameters:
        site = site_key(source.span(receiver))
        builtin_super = any(site_key(r) == site and r.get("target") == "builtins.super"
                            and r["resolution"] == "resolved" for r in rows)
        if not builtin_super or receiver.keywords:
            return None
        if receiver.args:
            if len(receiver.args) != 2 or not all(isinstance(arg, ast.Name) for arg in receiver.args):
                return None
            declared = catalog.lookup(scope, receiver.args[0].id)
            if not declared or declared.name != scope.parent.name or receiver.args[1].id != parameters[0].arg:
                return None
        receiver = ast.Name(id=parameters[0].arg, ctx=ast.Load())
    if not isinstance(receiver, ast.Name):
        return None
    binding = catalog.lookup(scope, receiver.id)
    if binding is None or binding.scope is not scope:
        return None
    classes = []
    if binding.kind == "parameter" and scope.parent and scope.parent.kind == "class":
        if any(ast.unparse(decorator) != "classmethod" for decorator in scope.node.decorator_list):
            return None
        if parameters and receiver.id == parameters[0].arg:
            classes = catalog.select(scope.parent.name)
    elif binding.kind == "variable" and len(binding.declarations) == 1:
        assignment = binding.node
        targets = assignment.targets if isinstance(assignment, ast.Assign) else [getattr(assignment, "target", None)]
        if len(targets) != 1 or not isinstance(targets[0], ast.Name) or targets[0].id != receiver.id:
            return None
        if (isinstance(assignment, (ast.Assign, ast.AnnAssign))
                and catalog.parents.get(id(assignment)) is scope.node
                and assignment.lineno < node.lineno and isinstance(assignment.value, ast.Call)):
            constructor = assignment.value.func
            declaration = catalog.lookup(scope, constructor.id) if isinstance(constructor, ast.Name) else None
            if (not declaration or declaration.kind not in {"class", "import"}
                    or len(declaration.declarations) != 1):
                return None
            site = site_key(source.span(assignment.value))
            targets = [r["target"] for r in rows if site_key(r) == site and r["resolution"] == "resolved"]
            if len(targets) == 1:
                classes = catalog.select(targets[0])
    if len(classes) != 1 or classes[0].kind != "class" or classes[0].decorators:
        return None
    if any(src.guards["dynamic_writes"] for src in ws.sources):
        return None
    # These operations can replace the lexical value or its lookup semantics.
    for item in ast.walk(scope.node):
        if isinstance(item, (ast.Global, ast.Nonlocal)) and receiver.id in item.names:
            return None
        if (isinstance(item, ast.Name) and item.id == receiver.id and isinstance(item.ctx, (ast.Store, ast.Del))
                and binding.kind == "parameter"):
            return None
        if (isinstance(item, ast.Attribute) and isinstance(item.ctx, (ast.Store, ast.Del))
                and item.attr in {node.func.attr, "__class__", "__getattr__", "__getattribute__"}):
            return None
        if isinstance(item, ast.Call) and isinstance(item.func, ast.Name) and item.func.id in {"setattr", "delattr"}:
            return None
    try:
        order, _edges = type_order(ws, classes[0])
    except (ValueError, NeedsBindingsError):
        return None
    if any(catalog.select(cls.name + "." + hook)
           for cls in order for hook in ("__new__", "__setattr__", "__delattr__")):
        return None
    if any(getattr(cls, "decorators", ()) for cls in order):
        return None
    if any(not preserves_receiver(ws, source, loop, receiver.id, node.func.attr, order) for loop in loops):
        return None
    if not preserves_receiver(ws, source, scope.node, receiver.id, node.func.attr, order,
                              before=(node.lineno, source.span(node)["column"]),
                              declaration=binding.node if binding.kind == "variable" else None):
        return None
    if binding.kind == "variable":
        for cls in order:
            initializers = catalog.select(cls.name + ".__init__")
            if initializers:
                init = initializers[0]
                args = [*init.node.args.posonlyargs, *init.node.args.args]
                if (len(initializers) != 1 or init.decorators or not args or not preserves_receiver(
                    ws, init.source, init.node, args[0].arg, node.func.attr, order,
                )):
                    return None
                break
    return order


def flow_sensitive(ws, source, node, entry, rows):
    """LSP hierarchy gives declaration/type edges, not Python mutation proofs.

    Reject its answer at explicit dynamic constructs before flow validation.
    Straight calls to declarations stay entirely on the native path.
    """
    catalog = ws.catalog
    scope = catalog.node_scopes[id(node)]
    if ws.receivers.get(entry.identity):
        return "an inherited body requires the selected receiver's MRO"
    if source.guards["star"]:
        return "star imports may replace this binding"
    if isinstance(node.func, ast.Name):
        binding = catalog.lookup(scope, node.func.id)
        if binding and binding.kind not in {"function", "method", "class", "import"}:
            return "a callable value needs assignment/control-flow resolution"
        if (
            scope.kind in {"module", "class"}
            and binding
            and (node.lineno, source.span(node)["column"]) < (binding.identifier["line"], binding.identifier["column"])
        ):
            return "the declaration follows this eagerly evaluated use"
        if node.func.id in source.guards["globals"]:
            return "the global callable may be rebound"
    if isinstance(node.func, ast.Attribute):
        dynamic = any(src.guards["dynamic"] for src in ws.sources)
        metaclass = any(src.guards["metaclass"] for src in ws.sources)
        if dynamic or metaclass:
            order = receiver_order(ws, source, node, rows)
            if order is not None:
                dynamic = any(catalog.select(cls.name + "." + method)
                              for cls in order for method in ("__getattr__", "__getattribute__"))
                # type_order rejects a custom metaclass anywhere in the MRO.
                metaclass = False
        if dynamic:
            return "dynamic attribute access may replace this member"
        if metaclass:
            return "a custom metaclass may replace this member"
        if any(r["resolution"] == "unresolved" and r["scope"] == scope.name and r["line"] <= node.lineno for r in rows):
            return "a preceding unresolved call may mutate receiver bindings"
        if not hasattr(ws, "reassigned_receivers"):
            ws.reassigned_receivers = {}
        if id(scope.node) not in ws.reassigned_receivers:
            ws.reassigned_receivers[id(scope.node)] = any(
                isinstance(n, ast.Name) and n.id in {"self", "cls"} and isinstance(n.ctx, ast.Store)
                for n in ast.walk(scope.node)
            )
        if ws.reassigned_receivers[id(scope.node)]:
            return "the method receiver is reassigned"
    return None


def calls(ws, entry, *, fallback=True, fallback_sites=None):
    source, catalog = entry.source, ws.catalog
    nodes = [node for node in ast.walk(entry.node) if isinstance(node, ast.Call)]
    known, owners = {}, {}
    for node in nodes:
        scope = catalog.node_scopes[id(node)]
        owners[scope.identity] = scope
    for scope in owners.values():
        if scope.kind != "function" or isinstance(scope.node, ast.Lambda):
            continue
        try:
            location = catalog.selection(source, scope.node, scope.node.name)
            items = ws.request("textDocument/prepareCallHierarchy", ws.params(source, location)) or []
            if len(items) != 1:
                continue
            outgoing = ws.request("callHierarchy/outgoingCalls", {"item": items[0]}) or []
            for link in outgoing:
                name = target(ws, link["to"])
                for site in link["fromRanges"]:
                    located = ws.location(items[0]["uri"], site)
                    if located and (node := call_node(*located)) is not None:
                        known.setdefault(id(node), set()).add(name)
        except NeedsBindingsError as error:
            ws.diagnostics.append({"kind": "lsp_fallback", "message": str(error)})
    rows = []
    for node in nodes:
        names = known.get(id(node), set())
        if not names:
            func = node.func
            span = (
                catalog.selection(source, func, func.attr, last=True)
                if isinstance(func, ast.Attribute)
                else source.span(func)
            )
            found = ws.definition(source, span)
            names = {candidate.name for candidate in found if candidate.kind in {"function", "method", "class"}}
        name = next(iter(names)) if len(names) == 1 and None not in names else None
        row = occurrence(
            ws,
            source,
            node,
            "CALL",
            callee=source.segment(node.func),
            target=name,
            resolution="resolved" if name else "unresolved",
            engine="ty-lsp",
            reason=None if name else "ty did not establish a unique callable target",
            roles=["read", "call"],
        )
        if name:
            definitions = ws.catalog.select(name)
            if definitions and set(definitions[0].decorators) - {"staticmethod", "classmethod"}:
                row.update(target=None, resolution="unresolved", reason="a decorator may replace this callable")
        rows.append(row)
    # ty may choose one declaration for a union-valued alias. That is a useful
    # IDE location, but not proof of one runtime call edge.
    guards = [flow_sensitive(ws, source, node, entry, rows) for node in nodes]
    for row, reason in zip(rows, guards, strict=True):
        if reason:
            row.update(target=None, resolution="unresolved", reason=reason)
    if fallback and any(
        row["resolution"] == "unresolved"
        and not row.get("unreachable")
        and (fallback_sites is None or site_key(row) in fallback_sites)
        for row in rows
    ):
        try:
            analysis = ws.analysis([entry.name])
            recovered = {}
            for scope in owners.values():
                if scope.name in analysis.symbols:
                    receiver = ws.receivers.get(entry.identity, "") if scope.node is entry.node else ""
                    for event in flow_query(analysis, "callee", [scope.name], receiver=receiver)["results"]:
                        recovered[site_key(event)] = event
            for row in rows:
                event = recovered.get(site_key(row))
                if row["resolution"] == "unresolved" and event and event["resolution"] == "resolved":
                    row.update(
                        target=event["target"],
                        resolution="resolved",
                        reason=None,
                        engine="pyan3+flow",
                        **({"receiver": event["receiver"]} if event.get("receiver") else {}),
                    )
                elif row["resolution"] == "unresolved" and event:
                    row.update(reason=event.get("reason") or row["reason"], engine="pyan3+flow")
        except (ValueError, KeyError, RecursionError) as error:
            ws.diagnostics.append({"kind": "unresolved", "message": str(error)})
    return rows


def events(ws, entry, filters):
    source, catalog = entry.source, ws.catalog
    rows = calls(ws, entry) if {"call", "read"}.intersection(filters) else []
    for node in ast.walk(entry.node):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and "read" in filters:
            scope = catalog.node_scopes.get(id(node))
            binding = catalog.lookup(scope, node.id) if scope else None
            rows.append(
                occurrence(ws, source, node, "REF", roles=["read"], binding=binding.name if binding else None),
            )
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and "read" in filters:
            rows.append(occurrence(ws, source, node, "REF", roles=["read"]))
        elif isinstance(node, (ast.Import, ast.ImportFrom)) and "import" in filters:
            rows.append(occurrence(ws, source, node, "REF", roles=["import"]))
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for item in targets:
                for leaf in ast.walk(item):
                    if not isinstance(leaf, (ast.Name, ast.Attribute, ast.Subscript)) or not isinstance(
                        leaf.ctx,
                        ast.Store,
                    ):
                        continue
                    kind = (
                        "ATTR_WRITE"
                        if isinstance(leaf, ast.Attribute)
                        else "SUBSCRIPT_WRITE"
                        if isinstance(leaf, ast.Subscript)
                        else "ASSIGN"
                    )
                    row = occurrence(
                        ws,
                        source,
                        node,
                        kind,
                        target=source.segment(leaf),
                        roles=["write"],
                        selection=source.span(leaf),
                    )
                    if selected(row, filters):
                        rows.append(row)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            for leaf in ast.walk(node.target):
                if isinstance(leaf, ast.Name) and {"write", "assign"}.intersection(filters):
                    rows.append(occurrence(ws, source, leaf, "ASSIGN", roles=["write"]))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars and {"write", "assign"}.intersection(filters):
                    for leaf in ast.walk(item.optional_vars):
                        if isinstance(leaf, ast.Name):
                            rows.append(occurrence(ws, source, leaf, "ASSIGN", roles=["write"]))
        elif isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)) and isinstance(node.ctx, ast.Del):
            kind = (
                "ATTR_WRITE"
                if isinstance(node, ast.Attribute)
                else ("SUBSCRIPT_WRITE" if isinstance(node, ast.Subscript) else "ASSIGN")
            )
            row = occurrence(ws, source, node, kind, roles=["write"], operation="delete")
            if selected(row, filters):
                rows.append(row)
        elif isinstance(node, ast.ExceptHandler) and node.name and {"write", "assign"}.intersection(filters):
            rows.append(
                occurrence(
                    ws,
                    source,
                    node,
                    "ASSIGN",
                    roles=["write"],
                    selection=catalog.selection(source, node, node.name),
                ),
            )
        elif isinstance(node, ast.Return) and "return" in filters:
            rows.append(occurrence(ws, source, node, "RETURN"))
        elif isinstance(node, ast.Raise) and "raise" in filters:
            rows.append(occurrence(ws, source, node, "RAISE"))
        elif (
            "doc" in filters
            and isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and ast.get_docstring(node, clean=False) is not None
        ):
            rows.append(occurrence(ws, source, node.body[0], "DOC"))
    return unique(rows)


def expression_references(ws, selector, filters):
    """Retain explicit expression queries without pretending a resolved symbol."""
    analysis = ws.analysis([selector], reverse=True)
    result = flow_query(analysis, "refs", [selector])
    ws.diagnostics.extend(result["diagnostics"])
    rows = []
    for ref in result["results"]:
        if ref["role"] in {"definition", "parameter"}:
            continue
        source = next(s for s in ws.sources if s.name == ref["source"])
        point = (ref["line"], ref["column"])
        nodes = source.nodes_at(point)
        node = min(nodes, key=lambda n: len(source.segment(n))) if nodes else source.tree
        roles = [ref["role"]]
        if "read" in roles and call_node(source, ref):
            roles.append("call")
        row = {
            **ref,
            "roles": roles,
            "engine": "pyan3+flow",
            "statement": ws.catalog.header(source, statement(ws.catalog, node)),
        }
        if not row.get("target"):
            row.update(
                value_resolution=row["resolution"],
                resolution="unresolved",
                reason="The expression is present, but its receiver/definition is not established",
            )
        if selected(row, filters):
            rows.append(row)
    return unique(rows)


def unique(rows):
    found = {}
    for row in rows:
        key = tuple(row.get(k) for k in ("source", "line", "column", "end_line", "end_column", "kind", "target"))
        if key in found:
            found[key]["roles"] = sorted(set(found[key].get("roles", [])) | set(row.get("roles", [])))
        else:
            found[key] = row
    return sorted(found.values(), key=lambda r: (r["source"], r["line"], r["column"], r["end_line"], r["end_column"]))


def references(ws, entry, filters):
    rows = []
    try:
        sites = (
            ws.request(
                "textDocument/references",
                {**ws.params(entry.source, entry.identifier), "context": {"includeDeclaration": True}},
            )
            or []
        )
        for site in sites:
            located = ws.location(site["uri"], site["range"])
            if not located:
                ws.diagnostics.append(
                    {"kind": "unresolved", "message": "A reference is outside the available input."},
                )
                continue
            source, span = located
            catalog = ws.catalog
            point = (span["line"], span["column"])
            declarations = catalog.at(source, span)
            if any(e.kind in {"module", "class", "function", "method", "parameter"} for e in declarations):
                continue
            enclosing = source.nodes_at(point)
            expressions = [n for n in enclosing if isinstance(n, (ast.Name, ast.Attribute, ast.alias))]
            node = min(expressions, key=lambda n: len(source.segment(n))) if expressions else None
            if node is None:
                continue
            roles = ["read"]
            if any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in enclosing):
                roles = ["import"]
            elif isinstance(getattr(node, "ctx", None), (ast.Store, ast.Del)):
                roles = ["write"]
                if any(isinstance(n, ast.AugAssign) for n in enclosing):
                    roles.append("read")
            call = call_node(source, span)
            if call and "read" in roles:
                roles.append("call")
            row = occurrence(
                ws,
                source,
                node,
                "REF",
                roles=roles,
                target=entry.name,
                binding=entry.name,
                resolution="resolved",
                engine="ty-lsp",
            )
            if selected(row, filters):
                rows.append(row)
    except NeedsBindingsError as error:
        ws.diagnostics.append({"kind": "lsp_fallback", "message": str(error)})
        analysis = ws.analysis([entry.name], reverse=True)
        sources = {source.name: source for source in analysis.sources}
        for reference in flow_query(analysis, "refs", [entry.name])["results"]:
            if reference["role"] not in {"definition", "parameter"}:
                roles = [reference["role"]]
                if "read" in roles and call_node(sources[reference["source"]], reference):
                    roles.append("call")
                row = {**reference, "roles": roles, "engine": "pyan3+flow"}
                if selected(row, filters):
                    rows.append(row)
    # Incoming call hierarchy includes proven calls through callable aliases
    # that references() need not report as textual uses of the declaration.
    if (not filters or "call" in filters or "read" in filters) and entry.kind in {"function", "method", "class"}:
        try:
            items = ws.request("textDocument/prepareCallHierarchy", ws.params(entry.source, entry.identifier)) or []
            for item in items:
                links = ws.request("callHierarchy/incomingCalls", {"item": item}) or []
                for link in links:
                    for site in link["fromRanges"]:
                        located = ws.location(link["from"]["uri"], site)
                        if located and (node := call_node(*located)) is not None:
                            row = occurrence(
                                ws,
                                located[0],
                                node,
                                "CALL",
                                roles=["read", "call"],
                                target=entry.name,
                                resolution="resolved",
                                engine="ty-lsp",
                            )
                            if not row.get("unreachable"):
                                rows.append(row)
        except NeedsBindingsError as error:
            ws.diagnostics.append({"kind": "lsp_fallback", "message": str(error)})
    # Incoming call hierarchy may report possible calls of union-valued aliases.
    # Validate those occurrences through the same call resolver as show/path.
    pending = {}
    for row in rows:
        if "call" not in row.get("roles", []):
            continue
        source = next(s for s in ws.sources if s.name == row["source"])
        node = call_node(source, row)
        scope = ws.catalog.node_scopes[id(node)] if node else None
        owners = [e for e in ws.catalog.entries if scope and e.node is scope.node and e.source is source]
        if len(owners) != 1:
            row.update(resolution="unresolved", target=None, reason="The caller scope has no unique binding")
            continue
        owner = owners[0]
        pending.setdefault(owner.identity, (owner, []))[1].append((row, site_key(source.span(node))))
    for owner, uses in pending.values():
        # Keep the whole caller's native/flow guards, including preceding calls
        # that could mutate a receiver. Only requested reference sites can justify
        # an expensive flow fallback; unrelated unresolved calls are not evidence
        # that these references need it.
        events = {site_key(row): row for row in calls(ws, owner, fallback_sites={key for _, key in uses})}
        for row, key in uses:
            if event := events.get(key):
                row.update(
                    resolution=event["resolution"],
                    target=event["target"],
                    reason=event.get("reason"),
                    engine=event["engine"],
                )
    if entry.kind in {"class", "method"} and (
        not filters or {"inherit", "implement", "override"}.intersection(filters)
    ):
        from .relations import inheritance

        relations = inheritance(ws, entry, ws.args.max_depth)
        roles = {"inherits": "inherit", "implements": "implement", "overrides": "override"}
        for relation in relations["results"]:
            if relation.get("target") != entry.name:
                continue
            role = roles.get(relation["relation"])
            if role is None or (filters and role not in filters):
                continue
            source = next(s for s in ws.sources if s.name == relation["source"])
            declarations = ws.catalog.select(relation["symbol"])
            if len(declarations) != 1:
                continue
            declaration = declarations[0]
            span = relation
            # Native subtype locations are declarations, while a direct base is
            # a use of the selected class. Locate that expression exactly.
            if role == "inherit":
                for base in declaration.node.bases:
                    node = base.value if isinstance(base, ast.Subscript) else base
                    selected_span = (
                        ws.catalog.selection(source, node, node.attr, last=True)
                        if isinstance(node, ast.Attribute)
                        else source.span(node)
                    )
                    if any(candidate.identity == entry.identity for candidate in ws.definition(source, selected_span)):
                        span = source.span(base)
                        break
            else:
                span = declaration.identifier
            rows.append(
                {
                    **{k: span[k] for k in ("source", "line", "column", "end_line", "end_column")},
                    "kind": "REF",
                    "roles": [role],
                    "symbol": declaration.name,
                    "scope": declaration.parent,
                    "target": entry.name,
                    "binding": entry.name,
                    "resolution": relation.get("resolution", "resolved"),
                    "statement": ws.catalog.header(source, declaration.node),
                    "engine": relation.get("engine", "ty-lsp"),
                },
            )
        if relations["resolution"] == "unresolved":
            ws.diagnostics.append({"kind": "unresolved", "message": relations["reason"] or "MRO is unresolved"})
    # A base use can be both a read and an inheritance reference. Merge roles at
    # the same exact use site; rendering also coalesces overlapping source lines.
    combined = {}
    for row in rows:
        key = tuple(row.get(k) for k in ("source", "line", "column", "end_line", "end_column", "target"))
        if key in combined:
            combined[key]["roles"] = sorted(set(combined[key].get("roles", [])) | set(row.get("roles", [])))
        else:
            combined[key] = row
    return unique(
        [
            row
            for row in combined.values()
            if "call" not in row.get("roles", [])
            or row.get("resolution") == "unresolved"
            or row.get("target") == entry.name
        ],
    )
