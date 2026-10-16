"""Select candidate files before constructing semantic catalogs.

File filtering is conservative: it cannot establish a symbol or a binding.
The regular matcher and resolvers still validate every returned result.
"""

import re
from contextlib import suppress
from pathlib import PurePosixPath

from .fast_sources import NeedsBindingsError
from .selectors import braces


def literal_regex(pattern, mode):
    """A safe candidate hint, not a replacement for rg's final regex matching."""
    if mode == "regex" and re.fullmatch(r"\^[A-Za-z_]\w*(?:\\\.[A-Za-z_]\w*)*\$", pattern, flags=re.ASCII):
        return pattern[1:-1].replace(r"\.", ".")
    return None


def regex_prefix(pattern, mode):
    """A mandatory qualified prefix; the original regex still selects results."""
    if mode != "regex" or "(?" in pattern:
        return None
    match = re.match(r"\^((?:[A-Za-z_]\w*\\\.)+)", pattern, re.ASCII)
    if not match or pattern[match.end():match.end() + 1] in {"?", "*", "{"}:
        return None
    depth, bracket, escaped = 0, False, False
    for char in pattern:
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif bracket:
            bracket = char != "]"
        elif char == "[":
            bracket = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "|" and depth == 0:
            return None
    return match[1][:-2].replace(r"\.", ".")


def lexical_candidate(source, patterns, matcher, scopes=False, *, declarations=False):
    """A textual mention alone cannot declare a symbol; retain all possible roots."""
    if source.parse_error or matcher.ignore_case or source.guards["star"]:
        return True
    for original, matching in patterns:
        pattern, mode = original, matching
        if hint := literal_regex(pattern, mode):
            pattern, mode = hint, "glob"
        elif prefix := regex_prefix(pattern, mode):
            pattern, mode = prefix + ".*", "glob"
        for expression in braces(pattern) if mode == "glob" else [pattern]:
            if not expression or "/" in expression or expression.endswith((".py", ".pyi")):
                return True
            first = expression.partition(".")[0]
            if mode != "glob" or not first.isascii() or "[" in first or "**" in first:
                return True
            names = source.scope_names if declarations and "." in expression else source.declared_names
            if matcher.indices(first, names):
                return True
            parts = expression.split(".")
            if any(matcher.indices(".".join(parts[:size]), [source.module]) for size in range(1, len(parts) + 1)):
                return True
            if scopes:
                packages = [".".join(source.module.split(".")[:size])
                            for size in range(1, len(source.module.split(".")))]
                if matcher.indices(expression, packages):
                    return True
    return False


def candidate_paths(loader, pattern, mode, matcher, *, scopes=False, bindings=False):
    """Keep all possible matches; widen when a pattern has no safe file filter."""
    if hint := literal_regex(pattern, mode):
        pattern, mode = hint, "glob"
    elif not matcher.ignore_case and (prefix := regex_prefix(pattern, mode)):
        pattern, mode = prefix + ".*", "glob"
    if mode == "glob" and (expanded := braces(pattern)) != [pattern]:
        return set().union(*(candidate_paths(loader, part, mode, matcher, scopes=scopes, bindings=bindings)
                             for part in expanded))
    paths = list(loader.paths)
    if mode != "glob" or matcher.ignore_case or not pattern.isascii() or "[" in pattern:
        return set(paths)
    first = pattern.partition(".")[0]
    if not first.isidentifier():
        # File addresses are independent of Python identifier spelling.
        if "/" in pattern:
            return {paths[i] for i in matcher.indices(pattern, paths, domain="file")}
        leaf = pattern.rsplit(".", 1)[-1]
        if scopes and "." in pattern and leaf.isidentifier():
            # list enumerates declarations, not inherited member projections.
            # A qualified glob with a literal leaf can only match declarations
            # of that leaf, module names or containing package/directory scopes.
            selected = set(loader.identifier_files(leaf))
            for path, module in loader.paths.items():
                packages = [".".join(module.split(".")[:size])
                            for size in range(1, len(module.split(".")) + 1)]
                directories = [str(parent) for parent in PurePosixPath(path).parents if str(parent) != "."]
                if (matcher.indices(pattern, packages)
                        or matcher.indices(pattern, [path, *directories], domain="file")):
                    selected.add(path)
            if not selected:
                loader.validate_absence()
            return selected
        # A single-component glob must contain its literal fragments in either
        # a source identifier or a module path. This is only a file prefilter;
        # the catalog still establishes every declaration and binding.
        fragments = re.findall(r"[A-Za-z0-9_]+", first) if "**" not in first else []
        if not fragments:
            return set(paths)
        selected = set(loader.identifier_files(first, glob=True))
    else:
        selected = set(loader.identifier_files(first, scope="." in pattern and not bindings))
    # A matching qualified name starts in either its module path or a source
    # identifier. Include non-ASCII source too: Python normalizes identifiers.
    # A lexical address with children starts at a module, function or class.
    # Import/assignment aliases are resolved separately if no declaration matches.
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
    if not selected and "." in pattern and not bindings and first.isidentifier():
        selected.update(loader.identifier_files(first))
    # An absent name contributes no catalogs to a batch, while the existing
    # syntax/discovery-limit checks still determine whether absence is reliable.
    if not selected:
        loader.validate_absence()
    return selected


def binding_inputs(ws, pattern, matcher):
    """Load possible aliases only when a literal address needs binding resolution."""
    if not ws.loader or ws.selection_files:
        return
    parts = pattern.split(".")
    if any(ws.binding_prefix(".".join(parts[:size])) for size in range(len(parts), 0, -1)):
        return
    paths = candidate_paths(ws.loader, pattern, "glob", matcher, bindings=True)
    ws.loader.read_many(paths)
    for path in sorted(paths):
        source = ws.load(path)
        if source and not lexical_candidate(source, [(pattern, "glob")], matcher):
            ws.loaded.pop(path, None)
    if ws.metadata()["complete"]:
        # This search covered every possible declaration/import alias for the
        # address, including module prefixes and normalized identifiers. A later
        # dependency traversal cannot invent an absent declaration prefix.
        if not hasattr(ws, "binding_searches"):
            ws.binding_searches = set()
        ws.binding_searches.add(pattern)


def descendant_candidates(ws, roots):
    """Ask ty for descendants of member owners, retaining exhaustive fallback.

    A literal unqualified member can only be inherited by descendants of classes
    that declare it. ty sees the complete revision, including files not yet
    parsed by this query. Every candidate still passes normal C3/member checks.
    """
    pending, visited, names = [], set(), set()
    try:
        for entry in roots:
            items = ws.request("textDocument/prepareTypeHierarchy", ws.params(entry.source, entry.identifier)) or []
            if len(items) != 1:
                return None
            pending.extend(items)
            names.add(entry.name)
        while pending:
            item = pending.pop()
            key = item["uri"], str(item["selectionRange"])
            if key in visited:
                continue
            visited.add(key)
            for child in ws.request("typeHierarchy/subtypes", {"item": item}) or []:
                located = ws.location(child["uri"], child["selectionRange"])
                candidates = [entry for entry in ws.catalog_for(located[0]).at(*located)
                              if entry.kind == "class"] if located else []
                if len(candidates) != 1:
                    return None
                names.add(candidates[0].name)
                pending.append(child)
    except (NeedsBindingsError, ValueError):
        return None
    return names if ws.metadata()["complete"] else None


def inherited_inputs(ws, pattern, mode, matcher):
    """Load ancestors only when the selector can address inherited members."""
    if hint := literal_regex(pattern, mode):
        pattern, mode = hint, "glob"
    elif not matcher.ignore_case and (prefix := regex_prefix(pattern, mode)):
        from .relations import type_order

        for entry in list(ws.catalog.entries):
            if entry.kind == "class" and (entry.name == prefix or entry.name.startswith(prefix + ".")):
                with suppress(NeedsBindingsError, ValueError):
                    type_order(ws, entry)
        return
    if mode == "glob" and (expanded := braces(pattern)) != [pattern]:
        for part in expanded:
            inherited_inputs(ws, part, mode, matcher)
        return
    if not ws.loader or ws.selection_files:
        return
    entries = ws.query_catalog(pattern, mode, matcher).entries
    classes = {e.name: e for e in entries if e.kind == "class"}
    if mode != "glob" or "**" in pattern or "[" in pattern:
        ws.all()
        return
    parent, dot, member = pattern.rpartition(".")
    if not dot:
        # An unscoped member query also selects inherited addresses in other
        # classes. Its candidate scope is global, unlike a qualified member.
        matches = matcher.entries(pattern, [e for e in entries if e.parent in classes])
        if matches:
            if not matcher.ignore_case:
                roots = {entry.parent: classes[entry.parent] for entry in matches}
                names = descendant_candidates(ws, list(roots.values()))
                if names is not None:
                    if not hasattr(ws, "inherited_owners"):
                        ws.inherited_owners = {}
                    ws.inherited_owners[pattern, mode] = names
                    return
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


def inherited_matches(ws, pattern, mode, matcher, kinds):
    """Match real MRO members, never a repository-wide class/member product."""
    from .relations import type_order

    entries = ws.query_catalog(pattern, mode, matcher).entries
    classes = {entry.name: entry for entry in entries if entry.kind == "class"}
    by_owner = {}
    seen_sources = set()

    def members(source):
        if source.name not in seen_sources:
            seen_sources.add(source.name)
            for entry in ws.catalog_for(source).entries:
                by_owner.setdefault(entry.parent, {}).setdefault(entry.name.rsplit(".", 1)[-1], entry)

    for source in {entry.source.name: entry.source for entry in entries}.values():
        members(source)
    filters = []
    hint = literal_regex(pattern, mode)
    if mode == "glob" or hint:
        class_names = list(classes)
        for expression in braces(hint or pattern):
            parent, dot, member = expression.rpartition(".")
            broad = "[" in expression or "**" in member
            allowed_classes = {class_names[i] for i in matcher.indices(parent, class_names)} \
                if dot and not broad and "**" not in parent else set(class_names)
            if not dot and not broad and not matcher.indices(
                member, [name for owner in classes for name in by_owner.get(owner, {})],
            ):
                allowed_classes = set()
            filters.append((allowed_classes, None if broad else member))
    projected = []
    prefix = regex_prefix(pattern, mode) if not matcher.ignore_case else None
    possible_owners = getattr(ws, "inherited_owners", {}).get((pattern, mode))
    for name, cls in classes.items():
        if possible_owners is not None and name not in possible_owners:
            continue
        if prefix and name != prefix and not name.startswith(prefix + "."):
            continue
        if not cls.node.bases or (
            ws.selection_files and cls.source.provenance.get("git_path") not in ws.selection_files
        ):
            continue
        allowed = [member for owners, member in filters if name in owners]
        if filters and not allowed:
            continue
        seen = set(by_owner.get(name, {}))
        if allowed and all(member is not None and member.isidentifier() and member in seen for member in allowed):
            continue
        try:
            order, _edges = type_order(ws, cls)
        except (NeedsBindingsError, ValueError):
            continue
        for base in order[1:]:
            if base.name == "builtins.object":
                continue
            members(base.source)
            for member, entry in by_owner.get(base.name, {}).items():
                matches = not filters or any(
                    pattern is None or matcher.indices(pattern, [member]) for pattern in allowed)
                if member not in seen and matches and (not kinds or entry.kind in kinds):
                    projected.append((entry, name, member))
                seen.add(member)
    options = [value for _entry, name, member in projected for value in (name + "." + member, member)]
    for index in sorted({i // 2 for i in matcher.indices(pattern, options, mode=mode)}):
        entry, receiver, _member = projected[index]
        proven = ws.lookup(entry.name, exact=True)
        if len(proven) == 1 and proven[0].identity == entry.identity:
            yield entry, receiver
