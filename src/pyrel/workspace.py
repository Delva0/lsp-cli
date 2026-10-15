"""Source selection and ty transport shared by the public actions."""

from __future__ import annotations

import ast
import io
import subprocess
import sys
import tempfile
import tokenize
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import cli as legacy
from .discovery import SelectionError
from .fast_sources import NeedsBindingsError
from .git_sources import GitSources, decode
from .lsp_queries import character
from .model import Source


class Workspace:
    def __init__(self, args, session, selectors=(), *, all_files=False, defer_load=False):
        from copy import copy

        from .file_selection import select_files

        args = copy(args)
        self.args, self.session = args, session
        self.loader = session.git(args, args.rev) if args.rev else None
        if args.source:
            args.source = select_files(args, self.loader.paths if self.loader else None)
        self.selection_files = set(args.source) if self.loader and args.source else None
        self.loaded = {}
        self.diagnostics, self.methods = [], []
        self._catalog, self._catalog_key = None, None
        self.native_loader = self.native_root = None
        self.native_paths = {}
        self.receivers, self.requests = {}, {}
        self.flow_analyses = {}
        if self.loader:
            if defer_load:
                return
            if all_files:
                paths = set(self.loader.paths)
                if selectors:
                    scope = selectors[0]
                    prefix_paths = {
                        p
                        for p, m in self.loader.paths.items()
                        if p == scope
                        or p.startswith(scope.rstrip("/") + "/")
                        or m == scope
                        or m.startswith(scope + ".")
                    }
                    paths = prefix_paths or self.loader.query_seeds(selectors, args.source)
            else:
                paths = (
                    set(args.source)
                    if args.source
                    else self.loader.query_seeds(selectors)
                    if selectors or args.source
                    else set(self.loader.paths)
                )
            if not paths and selectors and not all_files:
                # The old declaration prefilter intentionally omitted parameters
                # and comprehension locals. Lexical search is only a file filter;
                # the catalog still proves every returned symbol.
                paths = {
                    path
                    for selector in selectors
                    if selector.split(".")[0].isidentifier()
                    for path in self.loader.search(selector.split(".")[0])
                }
            for path in sorted(paths):
                self.load(path)
        elif args.command == "complete":
            self._incomplete_input()
        else:
            self.loaded = {source.name: source for source in legacy.inputs(args, sys.stdin, session)}

    def _incomplete_input(self):
        from .model import module_name

        modules = set()
        for spec in self.args.source or ["-"]:
            module, sep, name = spec.partition("=")
            if not sep:
                name, module = module, ""
            text = self.session.stdin(sys.stdin) if name == "-" else self.session.file(Path(name))
            label = self.args.filename if name == "-" else name
            path = Path(label)
            explicit = module or (self.args.module if name == "-" else None)
            if explicit:
                module, origin = explicit, "explicit"
                if not all(part.isidentifier() for part in module.split(".")):
                    raise ValueError(f"invalid module identity: {module}")
            else:
                if name != "-" and self.args.root:
                    path = path.resolve().relative_to(self.args.root.resolve())
                elif path.is_absolute():
                    path = path.relative_to(Path.cwd()) if path.is_relative_to(Path.cwd()) else Path(path.name)
                module = "__stdin__" if label == "<stdin>" else module_name(path)
                origin = "stdin" if name == "-" else "path"
            if module in modules:
                raise ValueError("duplicate module identity; assign distinct --files MODULE=PATH names")
            if label in self.loaded:
                raise ValueError("duplicate source label")
            modules.add(module)
            self.loaded[label] = Source(label, module, text, path.name == "__init__.py", origin, allow_incomplete=True)

    @property
    def sources(self):
        return list(self.loaded.values())

    @property
    def catalog(self):
        key = tuple(id(source) for source in self.sources)
        if self._catalog_key != key:
            self._catalog = self.session.catalog([source for source in self.sources if not source.parse_error])
            self._catalog_key = key
        return self._catalog

    def load(self, path):
        if not self.loader:
            return self.loaded.get(path)
        if path not in self.loaded:
            if self.args.command == "complete":
                if len(self.loader.parsed) + len(self.loader.failed) >= self.args.max_files:
                    self.loader.omitted.add(path)
                    return None
                source = Source(
                    f"{self.loader.revision}:{path}",
                    self.loader.paths[path],
                    decode(self.loader.read(path)),
                    provenance={
                        "git_path": path,
                        "git_commit": self.loader.commit,
                        "git_blob": self.loader.entries[path],
                    },
                    allow_incomplete=True,
                )
                self.loader.parsed[path] = source
            else:
                source = self.loader.source(path)
            if source:
                self.loaded[path] = source
        return self.loaded.get(path)

    def all(self):
        if self.loader:
            for path in sorted(self.loader.paths):
                self.load(path)

    def metadata(self):
        if not self.loader:
            return {"scope": "analysis_input", "complete": True, "analyzed_files": len(self.loaded)}
        loader = self.loader
        return {
            "repository": loader.repository,
            "revision": loader.revision,
            "commit": loader.commit,
            "scope": "selected_revision",
            "complete": not loader.omitted and not loader.failed,
            "discovered_files": len(loader.paths),
            "analyzed_files": len(self.loaded),
            "parsed_files": len(loader.parsed),
            "read_files": len(loader.raw),
            "omitted_files": sorted(loader.omitted),
            "failed_files": sorted(loader.failed),
            "import_roots": loader.roots,
        }

    def result(self, command, rows=()):
        metadata = self.metadata()
        diagnostics = [*self.diagnostics, *(self.loader.diagnostics if self.loader else [])]
        if not metadata["complete"]:
            diagnostics.append(
                {"kind": "incomplete", "message": "Source coverage is incomplete; increase --max-files."},
            )
        return {
            "schema_version": 2,
            "command": command,
            "results": list(rows),
            "sources": [source.identity() for source in self.sources],
            "diagnostics": diagnostics,
            "coverage": metadata,
            **({"git": metadata} if self.loader else {}),
            "backend": {
                "name": "ty-lsp+source" if self.methods else "source",
                "methods": list(dict.fromkeys(self.methods)),
                **({"version": "0.0.84"} if self.methods else {}),
            },
        }

    def select(self, selector, *, follow_import=True):
        matches = self.catalog.select(selector)
        if len(matches) > 1:
            raise SelectionError(f"Ambiguous symbol: {selector}", "ambiguous", matches)
        if matches:
            entry = matches[0]
            return self.follow_import(entry) if follow_import and entry.kind == "import" else entry
        # A member may have no declaration in this class (inherited method),
        # or may be reachable through an imported alias.
        for source in self.sources:
            for node in source.syntax:
                if not isinstance(node, ast.Attribute) or node.attr != selector.rsplit(".", 1)[-1]:
                    continue
                scope = self.catalog.node_scopes.get(id(node))
                expression = ast.unparse(node)
                possible = {expression, f"{scope.name}.{expression}" if scope else expression}
                if selector in possible or any(name.endswith("." + selector) for name in possible):
                    found = self.definition(source, self.catalog.selection(source, node, node.attr, last=True))
                    if len(found) == 1:
                        return found[0]
        if "." in selector:
            parent, member = selector.rsplit(".", 1)
            parents = self.binding_prefix(parent)
            if len(parents) == 1 and parents[0].kind == "class":
                found = self.inherited(parents[0], member)
                if found:
                    return found
        # The flow engine establishes imports, aliases and receiver-sensitive
        # relationships unavailable from a source-only lookup.
        try:
            sources = self.analysis_sources([selector])
            if not self.flow_candidate(selector, sources):
                raise SelectionError(f"No binding can establish {selector!r}")
            analysis = self.flow_analysis(sources)
            from .discovery import Index

            selected = Index(analysis).select(selector)
            if len(selected) == 1:
                symbol, _receiver = selected[0]
                for source in analysis.sources:
                    self.loaded.setdefault(source.provenance.get("git_path", source.name), source)
                found = self.catalog.select(symbol.name)
                if len(found) == 1:
                    return found[0]
        except (ValueError, NeedsBindingsError) as error:
            if isinstance(error, SelectionError) and error.status == "ambiguous":
                raise
        alternatives = self.catalog.select(selector.rsplit(".", 1)[-1])
        status = "not_found" if self.metadata()["complete"] else "unresolved"
        error = SelectionError(f"Symbol {selector!r} was not resolved in the selected input", status, alternatives)
        error.context = self.result(self.args.command)
        raise error

    def follow_import(self, entry):
        """Resolve this import occurrence, without reselecting its non-unique name."""
        resolved = self.definition(entry.source, entry.identifier)
        if len(resolved) == 1 and resolved[0].kind != "import":
            return resolved[0]
        self.diagnostics.append(
            {
                "kind": "unresolved",
                "message": f"The imported definition of {entry.name} is unresolved or outside this input; "
                "showing its import.",
            },
        )
        return entry

    def analysis(self, selectors=(), *, reverse=False):
        return self.flow_analysis(self.analysis_sources(selectors, reverse=reverse))

    def flow_analysis(self, sources):
        key = tuple(id(source) for source in sources)
        if key not in self.flow_analyses:
            # One invocation-local fork can evaluate multiple caller scopes,
            # just like a multi-hop traversal. Never mutate the session baseline.
            self.flow_analyses[key] = self.session.analysis(sources)
        return self.flow_analyses[key]

    def flow_candidate(self, selector, sources):
        """Reject only names the existing flow selector cannot possibly return.

        Index.select starts at a declaration prefix and follows binding keys.
        Check that necessary condition after loading the same full dependency
        closure. A positive result still needs the normal binding engine.
        """
        parts = selector.split(".")
        names = set().union(*(source.binding_names for source in sources))
        catalog = self.catalog
        return any(
            catalog.select(".".join(parts[:length])) and all(part in names for part in parts[length:])
            for length in range(len(parts), 0, -1)
        )

    def binding_prefix(self, selector):
        """Use the flow selector's exact-name and declaration-over-import precedence."""
        from .discovery import matching

        return matching(self.catalog.select(selector), selector)

    def analysis_sources(self, selectors=(), *, reverse=False):
        if self.loader:
            loader = self.session.git(self.args, self.args.rev)
            files = [s.provenance["git_path"] for s in self.sources]
            if selectors and not self.selection_files:
                selected = set()
                for selector in selectors:
                    parts = selector.split(".")
                    for length in range(len(parts), 0, -1):
                        entries = self.binding_prefix(".".join(parts[:length]))
                        if entries:
                            selected.update(entry.source.provenance["git_path"] for entry in entries)
                            break
                if selected:
                    # Source discovery and semantic evaluation have different
                    # scopes. Evaluate the selected definitions' full import
                    # closure, not every file examined while matching names.
                    files = sorted(selected)
            sources, _metadata, diagnostics = loader.collect("refs" if reverse else "path", [], files)
            self.diagnostics.extend(diagnostics)
            self.loader.omitted.update(loader.omitted)
            self.loader.failed.update(loader.failed)
            for source in sources:
                self.loaded.setdefault(source.provenance["git_path"], source)
            return sources
        return self.sources

    def _native(self):
        if self.native_loader:
            return
        if self.loader:
            self.native_loader = self.loader
            return
        self.native_paths = {
            source.module.replace(".", "/") + ("/__init__.py" if source.is_package else ".py"): source
            for source in self.sources
        }
        key = (tuple(sorted((path, source.text) for path, source in self.native_paths.items())), self.args.max_files)
        if key in self.session.inputs:
            self.native_loader = self.session.inputs[key]
            return
        temporary = tempfile.TemporaryDirectory(prefix="lsp-input-")
        self.session.resources.append(temporary)
        root = Path(temporary.name)
        for path, source in self.native_paths.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            encoding, _ = tokenize.detect_encoding(io.BytesIO(source.text.encode()).readline)
            target.write_bytes(source.text.encode(encoding))

        def git(*args):
            subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-C", str(root), *args],
                capture_output=True,
                check=True,
            )

        git("init", "-q")
        git("add", ".")
        git("-c", "user.name=lsp", "-c", "user.email=lsp@example.invalid", "commit", "-qm", "analysis input")
        self.native_loader = GitSources(root, "HEAD", max_files=self.args.max_files)
        self.session.inputs[key] = self.native_loader

    def request(self, method, params):
        self._native()
        import json

        key = (method, json.dumps(params, sort_keys=True))
        if key in self.requests:
            return self.requests[key]
        result = self.session.native(self.native_loader, method, params)
        self.methods.append(method)
        if "error" in result:
            raise NeedsBindingsError(f"{method}: {result['error']['message']}")
        self.native_root = Path(unquote(urlsplit(result["commit"]["rootUri"]).path))
        self.requests[key] = result.get("result")
        return self.requests[key]

    def params(self, source, span):
        self._native()
        if self.loader:
            path = source.provenance["git_path"]
        else:
            path = next(key for key, value in self.native_paths.items() if value is source)
        line = span["line"] - 1
        text = [*source.lines, ""][line]
        column = len(text[: span["column"]].encode("utf-16-le")) // 2
        return {
            "textDocument": {"uri": (Path(self.native_loader.repository) / path).as_uri()},
            "position": {"line": line, "character": column},
        }

    def location(self, uri, value):
        path = Path(unquote(urlsplit(uri).path))
        if not self.native_root or not path.is_relative_to(self.native_root):
            return None
        relative = path.relative_to(self.native_root).as_posix()
        source = (
            self.load(relative) if self.loader and relative in self.loader.paths else self.native_paths.get(relative)
        )
        if source is None:
            return None
        start, end = value["start"], value["end"]
        lines = [*source.lines, ""]
        span = {
            "source": source.name,
            "line": start["line"] + 1,
            "column": character(lines[start["line"]], start["character"]),
            "end_line": end["line"] + 1,
            "end_column": character(lines[end["line"]], end["character"]),
        }
        return source, span

    def definition(self, source, span):
        try:
            locations = self.request("textDocument/definition", self.params(source, span)) or []
        except NeedsBindingsError as error:
            self.diagnostics.append({"kind": "lsp_fallback", "message": str(error)})
            return []
        if isinstance(locations, dict):
            locations = [locations]
        found = {}
        for location in locations:
            located = self.location(
                location.get("uri", location.get("targetUri")),
                location.get("range", location.get("targetSelectionRange")),
            )
            if located:
                target, span = located
                # ty denotes a module definition with an empty range at the
                # file origin, rather than a declaration's identifier range.
                if (span["line"], span["column"], span["end_line"], span["end_column"]) == (1, 0, 1, 0):
                    entries = [entry for entry in self.catalog.by_source[id(target)] if entry.kind == "module"]
                else:
                    entries = self.catalog.at(target, span)
                for entry in entries:
                    found[entry.identity] = entry
        return list(found.values())

    def inherited(self, entry, member):
        # Resolve bases with ty; perform Python C3 ordering locally. Importing the
        # target code or treating a same-named method as a binding is unnecessary.
        from .relations import type_order

        try:
            order, _relations = type_order(self, entry)
        except (NeedsBindingsError, ValueError):
            return None
        for cls in order[1:]:
            found = self.catalog.select(cls.name + "." + member)
            if len(found) == 1:
                self.receivers[found[0].identity] = entry.name
                return found[0]
        return None
