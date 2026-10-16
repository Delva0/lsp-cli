"""Read one immutable Git revision and discover Python inputs without a checkout.

Forward queries follow repository-local imports. Reverse queries inspect import
dependencies across the revision before selecting importers for semantic analysis.
No target module is imported, no worktree file is read, and nothing is persisted.
"""

from __future__ import annotations

import ast
import configparser
import io
import os
import re
import subprocess
import tokenize
import tomllib
import unicodedata
from collections import OrderedDict, defaultdict, deque
from copy import copy
from difflib import get_close_matches
from pathlib import Path, PurePosixPath
from threading import Lock

from .model import Source, module_name


def decode(raw):
    encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    return raw.decode(encoding)


def relative(path):
    value = PurePosixPath(path)
    if value.is_absolute() or ".." in value.parts:
        raise ValueError(f"Git input paths must be repository-relative: {path}")
    return value.as_posix()


class SyntaxChecks:
    """Bounded, content-addressed syntax proofs shared by revision jobs."""

    def __init__(self):
        self.results = OrderedDict()
        self.lock = Lock()

    def check(self, oid, raw):
        # Parsing holds the GIL anyway. Serialize identical misses so two sides
        # of a diff do not parse the same immutable blob twice.
        with self.lock:
            if oid not in self.results:
                try:
                    ast.parse(decode(raw))
                    error = None
                except (SyntaxError, UnicodeError) as failure:
                    error = failure.with_traceback(None)
                self.results[oid] = error
            self.results.move_to_end(oid)
            error = self.results[oid]
            while len(self.results) > 8192:
                self.results.popitem(last=False)
            return copy(error) if error else None


class GitSources:
    """Resolve a revision once, then read all content by immutable object IDs."""

    def __init__(self, repository, revision, *, import_roots=(), max_files=128, syntax_checks=None):
        if max_files < 1:
            raise ValueError("--max-files must be positive")
        self.repository = str(Path(repository).resolve())
        bare = self.git("rev-parse", "--is-bare-repository").strip() == b"true"
        # Search from the repository root even when cwd has a different layout in this revision.
        self.repository = self.git("rev-parse", "--absolute-git-dir" if bare else "--show-toplevel").decode().strip()
        self.revision = revision
        self.commit = self.resolve_revision(revision)
        self.max_files = max_files
        self.entries = {}
        self._blob_cache = {}
        self._source_cache = {}
        self._search_cache = {}
        self.syntax_checks = syntax_checks if syntax_checks is not None else SyntaxChecks()
        self.diagnostics = []
        self.raw = {}
        self.parsed = {}
        self.failed = set()
        self.omitted = set()
        self.external = set()
        self.loaded = {}
        self.searches = 0
        for record in self.git("ls-tree", "-rz", "--full-tree", self.commit).split(b"\0"):
            if record:
                header, path = record.split(b"\t", 1)
                mode, kind, oid = header.decode().split()
                if kind == "blob" and mode in {"100644", "100755"}:
                    self.entries[path.decode("utf-8", errors="surrogateescape")] = oid
        self.roots = sorted({relative(r) for r in import_roots} if import_roots else self.default_roots())
        self.modules = defaultdict(list)
        self.paths = {}
        for path in sorted(self.entries):
            if not path.endswith(".py"):
                continue
            roots = [r for r in self.roots if r == "." or path.startswith(r + "/")]
            if not roots:
                continue
            root = max(roots, key=lambda r: len(PurePosixPath(r).parts))
            module = module_name(PurePosixPath(path if root == "." else path[len(root) + 1 :]))
            self.paths[path] = module
            self.modules[module].append(path)
        self.module_prefixes = {".".join(module.split(".")[:size])
                                for module in self.modules for size in range(1, len(module.split(".")) + 1)}

    def fork(self):
        """Share immutable revision data while keeping each query's discovery budget."""
        result = copy(self)
        result.raw = dict(self.raw)
        result.diagnostics = list(self.diagnostics)
        result.parsed, result.loaded = {}, {}
        result.failed, result.omitted, result.external = set(), set(), set()
        result.searches = 0
        return result

    def resolve_revision(self, revision):
        try:
            return self.git("rev-parse", "--verify", "--end-of-options", revision + "^{commit}").decode().strip()
        except ValueError as error:
            # A bad ref is not a symbol-resolution failure. Offer actual ref
            # names for an explicit retry; never silently change the revision.
            try:
                refs = self.git("for-each-ref", "--format=%(refname:short)",
                                "refs/tags", "refs/heads", "refs/remotes").decode().splitlines()
            except ValueError:
                refs = []
            matches = get_close_matches(revision, refs, n=3, cutoff=0.6)
            hint = ("Similar existing refs: " + ", ".join(repr(ref) for ref in matches) + ". "
                    if matches else "List available refs with git tag --list or git branch --all. ")
            raise ValueError(
                f"Cannot resolve Git revision {revision!r} to a commit in {self.repository!r}. "
                + hint + "Retry the same symbol query with an exact revision. " + str(error),
            ) from error

    def git(self, *arguments, allow_no_match=False, env=None, input_data=None):
        result = subprocess.run(
            ["git", "--no-pager", "-C", self.repository, *arguments],
            capture_output=True,
            check=False,
            env=env,
            input=input_data,
        )
        if result.returncode and not (allow_no_match and result.returncode == 1):
            raise ValueError("git: " + result.stderr.decode(errors="replace").strip())
        return result.stdout

    def read(self, path):
        if path not in self.raw:
            if path not in self._blob_cache:
                self._blob_cache[path] = self.git("cat-file", "blob", self.entries[path])
            self.raw[path] = self._blob_cache[path]
        return self.raw[path]

    def read_many(self, paths):
        """Read immutable blobs in one Git process, without parsing unrelated files."""
        paths = tuple(paths)
        missing = sorted({path for path in paths if path not in self._blob_cache})
        if missing:
            by_oid = defaultdict(list)
            for path in missing:
                by_oid[self.entries[path]].append(path)
            data = self.git("cat-file", "--batch", input_data="".join(oid + "\n" for oid in by_oid).encode())
            offset = 0
            for oid, names in by_oid.items():
                end = data.index(b"\n", offset)
                actual, kind, size = data[offset:end].split()
                if actual.decode() != oid or kind != b"blob":
                    raise ValueError(f"Git did not return blob {oid}")
                offset = end + 1
                content = data[offset:offset + int(size)]
                offset += int(size) + 1
                for path in names:
                    self._blob_cache[path] = content
        for path in paths:
            self.raw[path] = self._blob_cache[path]

    def default_roots(self):
        roots = {"."}
        if "src/__init__.py" not in self.entries and any(p.startswith("src/") for p in self.entries):
            roots.add("src")
        try:
            if "pyproject.toml" in self.entries:
                tool = tomllib.loads(self.read("pyproject.toml").decode()).get("tool", {})
                setuptools = tool.get("setuptools", {})
                roots.add(setuptools.get("package-dir", {}).get("", "."))
                packages = setuptools.get("packages", {})
                if isinstance(packages, dict):
                    roots.update(packages.get("find", {}).get("where", []))
                roots.update(p.get("from", ".") for p in tool.get("poetry", {}).get("packages", []))
            if "setup.cfg" in self.entries:
                cfg = configparser.ConfigParser(interpolation=None)
                cfg.read_string(decode(self.read("setup.cfg")))
                roots.update(cfg.get("options.packages.find", "where", fallback="").split())
                for row in cfg.get("options", "package_dir", fallback="").splitlines():
                    key, sep, value = row.partition("=")
                    if sep and not key.strip():
                        roots.add(value.strip())
            if "setup.py" in self.entries:
                tree = ast.parse(decode(self.read("setup.py")))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call):
                        for keyword in node.keywords:
                            if keyword.arg == "package_dir" and isinstance(keyword.value, ast.Dict):
                                mapping = ast.literal_eval(keyword.value)
                                if "" in mapping:
                                    roots.add(mapping[""])
        except (KeyError, ValueError, TypeError, SyntaxError, configparser.Error) as error:
            self.diagnostics.append({"kind": "import_roots", "message": f"Static package configuration: {error}; "
                                     "using discovered roots; --import-root can set them explicitly."})
        return {relative(r) for r in roots if isinstance(r, str) and r}

    def source(self, path, *, required=False):
        if path in self.parsed:
            return self.parsed[path]
        if path in self.failed:
            return None
        if len(self.parsed) + len(self.failed) >= self.max_files:
            self.omitted.add(path)
            if required:
                raise ValueError(f"--max-files {self.max_files} exhausted while selecting {path}; increase --max-files")
            return None
        try:
            raw = self.read(path)
            if path not in self._source_cache:
                self._source_cache[path] = Source(
                    f"{self.revision}:{path}", self.paths[path], decode(raw),
                    PurePosixPath(path).name == "__init__.py",
                    "git" if all(part.isidentifier() for part in self.paths[path].split(".")) else "file",
                    provenance={"git_commit": self.commit, "git_blob": self.entries[path], "git_path": path},
                )
            source = self._source_cache[path]
        except (SyntaxError, UnicodeError) as error:
            if required:
                raise
            self.failed.add(path)
            self.diagnostics.append({"kind": "unparsed_source", "message": f"{self.revision}:{path}: {error}"})
            return None
        self.parsed[path] = source
        return source

    def module_paths(self, module, *, required=False):
        paths = self.modules.get(module, [])
        if len(paths) > 1:
            message = f"ambiguous module {module}: {', '.join(paths)}; set --import-root"
            if required:
                raise ValueError(message)
            diagnostic = {"kind": "ambiguous_module", "message": message}
            if diagnostic not in self.diagnostics:
                self.diagnostics.append(diagnostic)
            return []
        return paths

    def with_packages(self, module):
        parts = module.split(".")
        return [p for i in range(1, len(parts) + 1) for p in self.module_paths(".".join(parts[:i]))]

    def dependencies(self, source, nodes=None):
        paths = set()
        for node in source.imports if nodes is None else nodes:
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                package = source.module.split(".") if source.is_package else source.module.split(".")[:-1]
                if node.level > len(package):
                    continue
                prefix = package[: len(package) - node.level + 1] if node.level else []
                module = ".".join([*prefix, *([node.module] if node.module else [])])
                modules = [module, *(f"{module}.{a.name}" for a in node.names if a.name != "*")]
            else:
                continue
            for module in modules:
                paths.update(self.with_packages(module))
            bases = modules if isinstance(node, ast.Import) else modules[:1]
            for base in bases:
                if base and base not in self.module_prefixes:
                    self.external.add(base)
        return paths

    def search(self, text, *, definition=False):
        self.searches += 1
        key = (text, definition)
        if key in self._search_cache:
            return self._search_cache[key]
        if definition:
            # Python identifiers cannot contain regular-expression metacharacters.
            pattern = rf"^[[:space:]]*(async[[:space:]]+def|def|class)[[:space:]]+{text}([[:space:](:]|$)"
            flags = ["-E"]
        else:
            pattern, flags = text, ["-F"]
        matches = self.git("grep", "-l", "-z", *flags, "-e", pattern, self.commit, "--", "*.py", allow_no_match=True)
        prefix = self.commit + ":"
        result = sorted({
            row.decode("utf-8", errors="surrogateescape").removeprefix(prefix)
            for row in matches.split(b"\0") if row
        } & self.paths.keys())
        self._search_cache[key] = result
        return result

    def seeds(self, symbols, files, command, *, missing_ok=False):
        selected = set()
        if command in {"outline", "where"} and symbols:
            paths = self.module_paths(symbols[0], required=True)
            if not paths:
                raise ValueError(f"{command} target must be a module in this revision: {symbols[0]}")
            for path in paths:
                self.source(path, required=True)
            selected.update(paths)
        for spec in files:
            module, sep, path = spec.partition("=")
            if not sep:
                path, module = module, ""
            path = relative(path)
            if path not in self.paths:
                raise ValueError(f"Python source not found in {self.revision}: {path}")
            if module and module != self.paths[path]:
                raise ValueError(
                    "Git module identity comes from import roots; use --import-root instead of MODULE=PATH",
                )
            self.module_paths(self.paths[path], required=True)
            self.source(path, required=True)
            selected.add(path)
        if command == "where" and not selected:
            raise ValueError("where in Git mode needs --file PATH to identify the line's source")
        if command == "outline" and not selected:
            return set(self.paths)
        if selected:
            return selected
        for symbol in symbols:
            parts = symbol.split(".")
            module = next((".".join(parts[:i]) for i in range(len(parts), 0, -1)
                           if ".".join(parts[:i]) in self.modules), None)
            if module:
                selected.update(self.module_paths(module, required=True))
                continue
            if not parts[0].isidentifier():
                raise ValueError(f"invalid symbol: {symbol}")
            candidates = self.search(parts[0], definition=True)
            for path in candidates:
                source = self.source(path)
                if source and any(isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                                  and n.name == parts[0] for n in ast.walk(source.tree)):
                    self.module_paths(source.module, required=True)
                    selected.add(path)
            if command == "refs":
                selected.update(self.search(parts[-1]))
        if not selected and not missing_ok:
            raise ValueError("no input found for these symbols in the revision; use a qualified symbol, "
                             "--file PATH, or --import-root PATH")
        return selected

    def identifier_files(self, name, *, scope=False, glob=False):
        """Prefilter ASCII names without dropping normalized Unicode identifiers."""
        key = (name, "glob" if glob else "scope" if scope else "identifier")
        self.searches += 1
        if key not in self._search_cache:
            glob_pattern = re.escape(name).replace(r"\*", ".*").replace(r"\?", ".") if glob else None
            expression = glob_pattern if glob else (
                rf"(def|class)[[:space:]]+{name}([^[:alnum:]_]|$)|\\$" if scope else name)
            matches = self.git(
                "grep", "-l", "-z", "-E", "-e", expression + "|[^ -~[:space:]]",
                self.commit, "--", "*.py", allow_no_match=True,
                env={**os.environ, "LC_ALL": "C"},
            )
            prefix = self.commit + ":"
            candidates = sorted({
                row.decode("utf-8", errors="surrogateescape").removeprefix(prefix)
                for row in matches.split(b"\0") if row
            } & self.paths.keys())
            self.read_many(candidates)
            # Non-ASCII comments must not send hundreds of unrelated modules
            # through AST/catalog construction. Normalize exactly as Python does
            # for identifiers; false positives in strings/comments are harmless.
            pattern = re.compile(glob_pattern if glob else
                                 (r"\b(?:def|class)\s+" if scope else r"(?<!\w)")
                                 + re.escape(name) + r"(?!\w)")
            selected = []
            for path in candidates:
                try:
                    text = unicodedata.normalize("NFKC", decode(self.raw[path]))
                except (SyntaxError, UnicodeError):
                    selected.append(path)  # The normal loader reports this error.
                    continue
                if scope:
                    text = text.replace("\\\r\n", "").replace("\\\n", "")
                if pattern.search(text):
                    selected.append(path)
            self._search_cache[key] = selected
        return self._search_cache[key]

    def validate_absence(self):
        """Preserve global no-match diagnostics without building symbol catalogs.

        The text filter proves that no declaration can match. Syntax errors and
        the explicit discovery budget still prevent a reliable absence. Parse
        each candidate transiently; do not retain unrelated ASTs, lexical scopes
        or binding data in the query. Immutable revision checks can be reused.
        """
        paths = sorted(self.paths)
        checked, omitted = paths[:self.max_files], paths[self.max_files:]
        self.omitted.update(omitted)
        self.read_many(checked)
        for path in checked:
            error = self.syntax_checks.check(self.entries[path], self.raw[path])
            if error and path not in self.failed:
                if isinstance(error, SyntaxError):
                    error.filename = f"{self.revision}:{path}"
                self.failed.add(path)
                self.diagnostics.append({"kind": "unparsed_source", "message": f"{self.revision}:{path}: {error}"})

    def query_candidates(self, selector):
        """Find actual declarations by name, including bindings and imports.

        Lexical Git search only reduces candidate files. AST names establish
        every returned candidate. Budget omissions remain visible to callers.
        """
        from .discovery import matching

        tail = selector.rsplit(".", 1)[-1]
        if not tail.isidentifier():
            return []
        # A module need not mention its own name in its source. Discover module
        # suffixes from the revision's file tree before the lexical prefilter;
        # keep declaration matches too so collisions are reported as ambiguous.
        paths = {path for module in self.modules
                 if module == selector or module.endswith("." + selector)
                 for path in self.module_paths(module)}
        paths.update(self.search(selector.split(".")[0]))
        found = []
        for path in sorted(paths):
            source = self.source(path)
            if source:
                found.extend(matching(source.declarations, selector))
        return matching(found, selector)

    def query_seeds(self, selectors, files=()):
        """Load selected source files without requiring a declaration kind."""
        selected = set()
        for path in files:
            selected.update(self.seeds([], [path], "path"))
        for selector in selectors:
            if selector in self.paths:
                selected.add(selector)
                continue
            if "/" in selector or selector.endswith(".py"):
                continue
            parts = selector.split(".")
            module = next((".".join(parts[:i]) for i in range(len(parts), 0, -1)
                           if ".".join(parts[:i]) in self.modules), None)
            if module:
                selected.update(self.module_paths(module))
            else:
                selected.update(s.source.provenance["git_path"] for s in self.query_candidates(selector))
                # Member scopes may use a class or an assignment/import alias.
                if len(parts) > 1:
                    selected.update(s.source.provenance["git_path"]
                                    for s in self.query_candidates(parts[0]))
        return selected

    def collect(self, command, symbols, files=(), receiver="", *, missing_ok=False, eager=False):
        seeds = self.seeds([*symbols, *([receiver] if receiver else [])], files, command, missing_ok=missing_ok)
        pending = deque(sorted(seeds))
        reverse = command in {"callers", "refs", "inherits"}
        importers = defaultdict(set)
        if reverse:
            # Imports are inspected per invocation. Only relevant importers enter
            # the expensive binding engine; no repository index is retained.
            candidates = [*sorted(seeds), *sorted(self.paths.keys() - seeds)]
            self.read_many(candidates[:self.max_files])
            for path in candidates:
                source = self.source(path)
                if source:
                    for dependency in self.dependencies(source):
                        importers[dependency].add(path)
        while pending:
            path = pending.popleft()
            if path in self.loaded:
                continue
            if path not in self._blob_cache:
                # Preserve discovery/budget order; batch the known frontier's
                # blobs without parsing or adding any unrelated input.
                available = self.max_files - len(self.parsed) - len(self.failed)
                self.read_many(list(dict.fromkeys([path, *pending]))[:max(0, available)])
            source = self.source(path)
            if source is None:
                continue
            if len(self.modules[source.module]) != 1:
                self.module_paths(source.module)
                continue
            self.loaded[path] = source
            if command not in {"outline", "where"}:
                pending.extend(sorted(self.dependencies(source, source.eager_imports if eager else None)
                                      | set(self.with_packages(source.module))))
                if reverse:
                    pending.extend(sorted(importers[path]))
        if not self.loaded and not missing_ok:
            raise ValueError("no analyzable Python sources selected from the revision")
        if self.omitted:
            self.diagnostics.append({"kind": "source_limit", "message": f"--max-files {self.max_files} reached; "
                                     f"{len(self.omitted)} discovered files not parsed. "
                                     "Increase --max-files for more coverage."})
        complete = not self.omitted and not self.failed and not any(
            d["kind"] == "ambiguous_module" for d in self.diagnostics
        )
        metadata = {
            "repository": self.repository, "revision": self.revision, "commit": self.commit,
            "import_roots": self.roots, "scope": "repository_importers" if reverse else "import_dependencies",
            "discovered_files": len(self.paths), "read_files": len(self.raw), "parsed_files": len(self.parsed),
            "analyzed_files": len(self.loaded), "searches": self.searches, "complete": complete,
            "seed_files": sorted(seeds), "omitted_files": sorted(self.omitted),
            "external_modules": sorted(self.external),
        }
        return [self.loaded[p] for p in sorted(self.loaded)], metadata, self.diagnostics
