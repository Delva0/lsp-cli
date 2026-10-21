"""Scope selection and lexical symbol listing, independent of kind filtering."""

from .symbols import Binding


def list_symbols(ws, scope, kinds, max_depth):
    root = scope if isinstance(scope, Binding) else None
    selector = root.name if root else scope
    package = directory_scope = None
    sources = ws.sources
    if selector and (root is None or root.kind == "module"):
        directory = selector.rstrip("/") + "/"
        scoped = [s for s in sources if s.provenance.get("git_path", s.name).startswith(directory)]
        modules = [s for s in sources if s.module == selector or s.module.startswith(selector + ".")]
        # A package/directory scope includes its modules, not merely __init__.py.
        if scoped or (modules and any(s.module != selector for s in modules)):
            root = None
            sources = scoped or modules
            if scoped:
                directory_scope = directory
            else:
                package = selector
        elif root is None:
            root = ws.select(selector, follow_import=False)
    if root:
        sources = [root.source]
    allowed = {s.name for s in sources}
    rows, matched = [], 0
    entries = ws.catalog.by_source[id(root.source)] if root else ws.catalog.entries
    for entry in entries:
        if entry.source.name not in allowed:
            continue
        base = root.name if root else entry.source.module
        if root and not (entry.name == base or entry.name.startswith(base + ".")):
            continue
        depth = entry.name[len(base) :].count(".")
        if not root and package:
            depth += entry.source.module[len(package) :].count(".")
        elif not root and directory_scope:
            path = entry.source.provenance.get("git_path", entry.source.name)
            depth += len(path.removeprefix(directory_scope).split("/"))
        # Kind selection affects output only. A method remains reachable even
        # when its containing module and class are not requested kinds.
        if kinds and entry.kind not in kinds:
            continue
        matched += 1
        if max_depth is None or depth <= max_depth:
            rows.append(entry.record())
    result = ws.result("list", rows)
    result["heads"] = [root.record()] if root else [
        entry.record() for entry in ws.catalog.entries if entry.kind == "module" and entry.source.name in allowed
    ]
    if root:
        result["symbol"] = root.record()
    result.update(
        scope=root.name if root else selector,
        kinds=list(kinds),
        max_depth=max_depth,
        recursive=ws.args.recursive,
        matched=matched,
        returned=len(rows),
        truncated=len(rows) < matched,
    )
    return result
