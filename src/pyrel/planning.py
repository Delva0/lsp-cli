"""Select candidate files before constructing semantic catalogs.

File filtering is conservative: it cannot establish a symbol or a binding.
The regular matcher and resolvers still validate every returned result.
"""

from contextlib import suppress
from pathlib import PurePosixPath

from .fast_sources import NeedsBindingsError
from .selectors import braces


def lexical_candidate(source, patterns, matcher):
    """A textual mention alone cannot declare a symbol; retain all possible roots."""
    if source.parse_error or matcher.ignore_case or source.guards["star"]:
        return True
    for pattern, mode in patterns:
        for expression in braces(pattern) if mode == "glob" else [pattern]:
            if "/" in expression or expression.endswith((".py", ".pyi")):
                return True
            first = expression.partition(".")[0]
            if mode != "glob" or not first.isidentifier() or not first.isascii():
                return True
            if first in source.declared_names:
                return True
    return False


def candidate_paths(loader, pattern, mode, matcher, *, scopes=False):
    """Keep all possible matches; widen when a pattern has no safe file filter."""
    if mode == "glob" and (expanded := braces(pattern)) != [pattern]:
        return set().union(*(candidate_paths(loader, part, mode, matcher, scopes=scopes) for part in expanded))
    paths = list(loader.paths)
    if mode != "glob" or matcher.ignore_case or not pattern.isascii() or "[" in pattern:
        return set(paths)
    first = pattern.partition(".")[0]
    if not first.isidentifier():
        # File addresses are independent of Python identifier spelling.
        if "/" in pattern:
            return {paths[i] for i in matcher.indices(pattern, paths, domain="file")}
        return set(paths)
    # A matching qualified name starts in either its module path or a source
    # identifier. Include non-ASCII source too: Python normalizes identifiers.
    selected = set(loader.identifier_files(first))
    modules = list(loader.modules)
    parts = pattern.split(".")
    for size in range(1, len(parts) + 1):
        prefix = ".".join(parts[:size])
        for index in matcher.indices(prefix, modules):
            selected.update(loader.modules[modules[index]])
    selected.update(paths[i] for i in matcher.indices(pattern, paths, domain="file"))
    if scopes:
        for path, module in loader.paths.items():
            packages = [".".join(module.split(".")[:size]) for size in range(1, len(module.split(".")))]
            directories = [str(parent) for parent in PurePosixPath(path).parents if str(parent) != "."]
            if matcher.indices(pattern, packages) or matcher.indices(pattern, directories, domain="file"):
                selected.add(path)
    # No lexical candidate is not a semantic proof of absence. Preserve full
    # discovery (including syntax errors and budgets) before reporting no match.
    return selected or set(paths)


def inherited_inputs(ws, pattern, mode, matcher):
    """Load ancestors only when the selector can address inherited members."""
    if mode == "glob" and (expanded := braces(pattern)) != [pattern]:
        for part in expanded:
            inherited_inputs(ws, part, mode, matcher)
        return
    if not ws.loader or ws.selection_files or len(ws.loaded) == len(ws.loader.paths):
        return
    entries = ws.catalog.entries
    classes = {e.name: e for e in entries if e.kind == "class"}
    if mode != "glob" or "**" in pattern or "[" in pattern:
        ws.all()
        return
    parent, dot, member = pattern.rpartition(".")
    if not dot:
        # An unscoped member query also selects inherited addresses in other
        # classes. Its candidate scope is global, unlike a qualified member.
        if matcher.entries(pattern, [e for e in entries if e.parent in classes]):
            ws.all()
        return
    from .relations import type_order

    declared = {e.name for e in entries}
    for cls in matcher.entries(parent, list(classes.values())):
        if not cls.node.bases or (member.isidentifier() and cls.name + "." + member in declared):
            continue
        # The normal resolver retains its unresolved/flow fallback behavior.
        with suppress(NeedsBindingsError, ValueError):
            type_order(ws, cls)
