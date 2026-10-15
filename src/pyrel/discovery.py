"""Source-backed names for query selection, including assignment declarations.

This index is invocation-local. A declaration is a source location, not a claim
about the runtime type of the value assigned there.
"""

from __future__ import annotations

import ast
from collections import defaultdict

from .model import Symbol

DECLARATIONS = {"binding", "import"}


def definitions(source):
    result = [Symbol(source.module, "module", source.tree, source, None)]

    def names(target):
        if isinstance(target, ast.Name):
            yield target.id
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                yield from names(element)
        elif isinstance(target, ast.Starred):
            yield from names(target.value)

    def own_nodes(node):
        for child in ast.iter_child_nodes(node):
            yield child
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                yield from own_nodes(child)

    def frame(node, name, kind, parents):
        own = list(own_nodes(node))
        globals_ = {n for child in own if isinstance(child, ast.Global) for n in child.names}
        nonlocals = {n for child in own if isinstance(child, ast.Nonlocal) for n in child.names}
        locals_ = {child.id for child in own if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)}
        locals_.update(child.arg for child in own if isinstance(child, ast.arg))
        locals_.update(child.name for child in own
                       if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
        locals_.update(alias.asname or alias.name.split(".")[0] for child in own
                       if isinstance(child, (ast.Import, ast.ImportFrom)) for alias in child.names)
        return {"name": name, "kind": kind, "parents": parents, "globals": globals_, "nonlocals": nonlocals,
                "locals": locals_ - globals_ - nonlocals}

    def owner(name, current):
        if name in current["globals"]:
            return source.module
        if name in current["nonlocals"]:
            return next((parent["name"] for parent in reversed(current["parents"])
                         if parent["kind"] == "function" and name in parent["locals"]), None)
        return current["name"]

    def walk(node, current):
        scope, class_scope = current["name"], current["kind"] == "class"
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = "class" if isinstance(child, ast.ClassDef) else "method" if class_scope else "function"
                name = f"{scope}.{child.name}"
                result.append(Symbol(name, kind, child, source, scope))
                walk(child, frame(child, name, "class" if kind == "class" else "function",
                                  [*current["parents"], current]))
                continue
            if isinstance(child, ast.Lambda):
                name = f"{scope}.<lambda>@{child.lineno}:{child.col_offset}"
                walk(child, frame(child, name, "function", [*current["parents"], current]))
                continue
            targets, kind = [], "binding"
            if isinstance(child, ast.Assign):
                targets = [name for target in child.targets for name in names(target)]
            elif isinstance(child, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                targets = list(names(child.target))
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                kind = "import"
                targets = [alias.asname or (alias.name.split(".")[0] if isinstance(child, ast.Import)
                                           else alias.name) for alias in child.names if alias.name != "*"]
            for name in dict.fromkeys(targets):
                binding_scope = owner(name, current)
                if binding_scope is not None:
                    result.append(Symbol(f"{binding_scope}.{name}", kind, child, source, scope))
            walk(child, current)

    walk(source.tree, frame(source.tree, source.module, "module", []))
    return result


def matching(definitions_, selector):
    exact = [symbol for symbol in definitions_ if symbol.name == selector]
    if exact:
        return exact
    matches = [symbol for symbol in definitions_ if symbol.name.endswith("." + selector)]
    return [symbol for symbol in matches if symbol.kind != "import"] or matches


def candidate(symbol, revision=""):
    return {"name": symbol.name, "kind": symbol.kind,
            "address": f"{revision}:{symbol.name}" if revision else symbol.name,
            **symbol.source.span(symbol.node)}


class SelectionError(ValueError):
    def __init__(self, message, status="not_found", candidates=(), binding=None):
        super().__init__(message)
        self.status = status
        self.candidates = list(candidates)
        self.binding = binding


class Index:
    def __init__(self, analysis):
        from .resolution import Bindings

        self.analysis = analysis
        self.definitions = list(analysis.symbols.values())
        self.declarations = defaultdict(list)
        for source in analysis.sources:
            for symbol in source.declarations:
                if symbol.kind in DECLARATIONS:
                    self.definitions.append(symbol)
                    self.declarations[symbol.parent].append(symbol)
        self.bindings = Bindings(analysis, self.definitions)
        self.resolutions = {}

    def select(self, selector):
        # File addresses are exact repository/input paths, with source identity
        # supplied by the loader. Never silently substitute a different file.
        paths = [s for s in self.analysis.sources
                 if selector in {s.name, s.provenance.get("git_path")}]
        if paths:
            selector = paths[0].module
        matches = matching(self.definitions, selector)
        names = {symbol.name for symbol in matches}
        if len(names) > 1:
            raise SelectionError(f"ambiguous name: {selector}; select one of the candidate addresses",
                                 "ambiguous", matches)
        if matches:
            real = [symbol for symbol in matches if symbol.kind not in DECLARATIONS]
            if any(symbol.name in self.analysis.duplicates for symbol in real):
                raise SelectionError(f"multiple definitions of {selector}", "ambiguous", matches)
            if any(symbol.kind in DECLARATIONS for symbol in matches):
                self.resolutions[selector] = self.bindings.describe(matches[0].name)
            return [(symbol, "") for symbol in matches]
        # Inherited methods and imported aliases retain the binding engine's
        # actual resolution; discovery never fabricates a same-named target.
        try:
            selected = self.bindings.select_member(selector)
            if selected is not None:
                matches, binding, alternatives = selected
                if not matches:
                    raise SelectionError(f"{selector}: {binding['reason']}", "unresolved", alternatives, binding)
                self.resolutions[selector] = binding
                return matches
            return [self.analysis.select(selector)]
        except SelectionError:
            raise
        except ValueError as error:
            alternatives = matching(self.definitions, selector.rsplit(".", 1)[-1])
            raise SelectionError(str(error), "ambiguous" if "ambiguous" in str(error) else "not_found",
                                 alternatives) from error
