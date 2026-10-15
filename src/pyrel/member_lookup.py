"""Bounded absence proofs; uncertainty retains the complete binding fallback."""

import ast
import unicodedata

from .effects import preserves_receiver


def static_class(entry):
    """The class declaration still denotes its original module binding."""
    return (entry.parent == entry.source.module and not entry.decorators
            and len(entry.declarations) == 1 and not entry.source.guards["star"]
            and entry.node.name not in entry.source.guards["globals"])


def annotation_nodes(source):
    nodes = set()
    for node in source.syntax:
        annotation = getattr(node, "annotation", None)
        returns = getattr(node, "returns", None)
        for value in (annotation, returns):
            if value is not None:
                nodes.update(ast.walk(value))
    return nodes


def readonly_class_references(source, name):
    """Allow annotations and direct bases, but no aliases or escaping class values.

    The flow engine does not execute annotations or implicit metaclass protocols.
    Renamed imports and value references require its normal alias/effect analysis.
    """
    allowed = annotation_nodes(source)
    for node in source.syntax:
        if isinstance(node, ast.ClassDef):
            for expression in node.bases:
                base = expression.value if isinstance(expression, ast.Subscript) else expression
                if isinstance(base, (ast.Name, ast.Attribute)):
                    allowed.update(ast.walk(base))
        elif (isinstance(node, ast.alias) and node.name.rsplit(".", 1)[-1] == name
                and node.asname and node.asname != name):
            return False
    return not any(
        node not in allowed and (
            (isinstance(node, ast.Name) and node.id == name)
            or (isinstance(node, ast.Attribute) and node.attr == name)
            or (isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names)
        )
        for node in source.syntax
    )


def absent_member(ws, entry, member):
    """Prove a static class cannot acquire this member before loading dependencies.

    Inspect every possible reference to the class across the selected repository,
    not only already loaded files. Class aliases/escapes, unresolved MRO, unusual
    constructors, incomplete inputs or an expensive proof all retain full flow.
    The bound limits this optional proof only; it never limits query coverage.
    """
    result = getattr(ws, "type_orders", {}).get(entry.identity)
    if not result or not ws.metadata()["complete"]:
        return False
    order = result[0]
    checked = static_member_proof(ws, member, order)
    if checked is None:
        return False
    ws.negative_member_proofs = [*getattr(ws, "negative_member_proofs", []), {
        "symbol": entry.name, "member": member, "sources": checked,
    }]
    return True


def unresolved_member(ws, entry, member, sources):
    """An external base cannot supply a resolved definition from this input.

    Still prove that the class/constructor cannot establish a known own binding.
    Unknown inheritance remains unresolved; it is never treated as an empty MRO.
    """
    if not static_class(entry) or not ws.metadata()["complete"]:
        return None
    source = entry.source
    catalog = ws.catalog_for(source)
    modules = ws.loader.module_prefixes if ws.loader else {
        item.module.split(".")[0] for item in ws.sources
    }
    for expression in entry.node.bases:
        node = expression.value if isinstance(expression, ast.Subscript) else expression
        name = node.id if isinstance(node, ast.Name) else (
            node.value.id if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) else None)
        binding = catalog.lookup(catalog.node_scopes[id(entry.node)], name) if name else None
        if (not binding or binding.kind != "import" or len(binding.declarations) != 1
                or name in source.guards["globals"]):
            continue
        imported = binding.node
        module = None
        if isinstance(node, ast.Name) and isinstance(imported, ast.ImportFrom) and imported.level == 0:
            module = imported.module
        elif isinstance(node, ast.Attribute) and isinstance(imported, ast.Import):
            module = next((alias.name for alias in imported.names if (alias.asname or alias.name) == name), None)
        if not module or module.split(".")[0] in modules:
            continue
        # Analysis.member consults own declarations and the own constructor when
        # a base is external. Its existing effect proof rejects receiver escapes,
        # callbacks, writes to this member and uncertain helper calls.
        checked = static_member_proof(ws, member, [entry], sources=sources)
        if checked is None:
            return None
        reason = f"Base {source.segment(expression)} is outside the analysis input; {member} is unresolved"
        ws.unresolved_member_proofs = [*getattr(ws, "unresolved_member_proofs", []), {
            "symbol": entry.name, "member": member, "external_module": module, "sources": checked,
        }]
        ws.diagnostics.append({"kind": "unresolved", "message": reason})
        return reason
    return None


def static_member_proof(ws, member, order, *, sources=None):
    classes = [cls for cls in order if cls.name != "builtins.object"]
    for cls in classes:
        if not static_class(cls) or ws.lookup(f"{cls.name}.{member}", exact=True):
            return None
    constructor = next((found for cls in classes if (found := ws.lookup(cls.name + ".__init__", exact=True))), [])
    if constructor:
        if len(constructor) != 1 or constructor[0].kind != "method" or constructor[0].decorators:
            return None
        init = constructor[0]
        args = [*init.node.args.posonlyargs, *init.node.args.args]
        if not args or not preserves_receiver(ws, init.source, init.node, args[0].arg, member, order):
            return None
    probe = ws.session.git(ws.args, ws.args.rev) if ws.loader and sources is None else None
    checked = {}
    for cls in classes:
        short = cls.node.name
        if probe:
            paths = probe.identifier_files(short)
            if len(paths) > min(16, ws.args.max_files):
                return None
            candidates = [cls.source, *(probe.source(path) for path in paths)]
        else:
            candidates = sources if sources is not None else ws.sources
        for source in candidates:
            if source is None or source.parse_error:
                return None
            if short not in unicodedata.normalize("NFKC", source.text):
                continue
            if not readonly_class_references(source, short):
                return None
            checked[source.name] = source.identity()
    return list(checked.values())
