"""Reuse source and native sessions while pinning inputs separately for each invocation.

A worker owns the shared session. Its invocation sessions retain independent
ref resolutions, file reads and discovery budgets; immutable source objects,
analyses and native snapshots live until the owning session closes.
"""

import sys
import tokenize
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .analysis import Analysis
from .git_sources import GitSources, decode
from .model import Source
from .symbols import Catalog


class Session:
    def __init__(self, shared=None):
        self.shared = shared
        self.revisions = {}
        self.files = {}
        self.sources = shared.sources if shared else {}
        self.analyses = shared.analyses if shared else {}
        self.catalogs = shared.catalogs if shared else OrderedDict()
        self.source_catalogs = shared.source_catalogs if shared else OrderedDict()
        self.inputs = shared.inputs if shared else {}
        self.stdin_text = None
        self.lsp_engine = self.runner = None
        self.resources = shared.resources if shared else []
        self.lanes = []
        self.assignments = {}

    def parallel(self, jobs):
        """Run revision jobs on bounded, persistent lanes; a Runner never changes threads.

        Return in submission order, independent of completion order. Each job gets
        fresh ref resolutions and discovery limits while its lane retains caches.
        """
        if self.shared:
            return self.shared.parallel(jobs)

        def run(owner, function):
            with Session(owner) as invocation:
                return function(invocation)

        pending = []
        for key, function in jobs:
            if key not in self.assignments:
                lane = len(self.assignments) % 4
                self.assignments[key] = lane
                if lane == len(self.lanes):
                    self.lanes.append((ThreadPoolExecutor(max_workers=1, thread_name_prefix="lsp-revision"), Session()))
            pool, owner = self.lanes[self.assignments[key]]
            pending.append(pool.submit(run, owner, function))
        return [future.result() for future in pending]

    def owners(self):
        yield self
        for _pool, owner in self.lanes:
            yield owner

    def native(self, loader, method, params):
        if self.shared:
            return self.shared.native(loader, method, params)
        if self.runner is None:
            import asyncio

            if __package__ == "pyrel":
                from graphub_lsp.lsp_worker import Engine
            else:
                from ...lsp.lsp_worker import Engine
            self.runner = asyncio.Runner()
            executable = Path(sys.executable).with_name("ty")
            self.lsp_engine = Engine(
                {
                    "command": [str(executable) if executable.is_file() else "ty", "server"],
                    "timeout": 30,
                    "concurrency": 8,
                    "server_info": {"name": "ty", "version": "0.0.84"},
                    "settings": {"ty": {"diagnosticMode": "openFilesOnly"}},
                },
            )
        return self.runner.run(
            self.lsp_engine.query(
                {"repo": loader.repository, "ref": loader.commit}, {"method": method, "params": params},
                resolved=True,
            ),
        )

    def close(self):
        if self.shared:
            return
        try:
            for pool, owner in self.lanes:
                try:
                    pool.submit(owner.close).result()
                finally:
                    pool.shutdown(wait=True)
            self.lanes.clear()
            self.assignments.clear()
            if self.runner:
                try:
                    self.runner.run(self.lsp_engine.close())
                finally:
                    self.runner.close()
                    self.runner = self.lsp_engine = None
        finally:
            for resource in reversed(self.resources):
                resource.cleanup()
            self.resources.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_error):
        self.close()

    def stdin(self, stream):
        if stream.isatty():
            raise ValueError("provide Python source on stdin or with --files PATH")
        if self.stdin_text is None:
            self.stdin_text = decode(stream.buffer.read()) if hasattr(stream, "buffer") else stream.read()
        return self.stdin_text

    def file(self, path):
        key = path.resolve()
        if key not in self.files:
            with tokenize.open(path) as stream:
                self.files[key] = stream.read()
        return self.files[key]

    def source(self, name, module, text, is_package, origin):
        key = (name, module, text, is_package, origin)
        if key not in self.sources:
            self.sources[key] = Source(name, module, text, is_package, origin)
        return self.sources[key]

    def git(self, args, revision):
        repository = args.repo or Path.cwd()
        key = (str(repository.resolve()), revision, tuple(args.import_root), args.max_files)
        if key not in self.revisions:
            cached = self.shared.revisions.get(key) if self.shared else None
            if cached and cached.resolve_revision(revision) == cached.commit:
                self.revisions[key] = cached
            else:
                self.revisions[key] = GitSources(
                    repository, revision, import_roots=args.import_root, max_files=args.max_files,
                )
            if self.shared:
                self.shared.revisions[key] = self.revisions[key]
        return self.revisions[key].fork()

    def analysis(self, sources):
        # Identical source objects mean identical source labels, identities and
        # discovery scope. Never union another query's inputs to improve reuse.
        key = tuple(id(source) for source in sources)
        if key not in self.analyses:
            self.analyses[key] = Analysis(sources)
        return self.analyses[key].fork()

    def catalog(self, sources):
        # Reuse only the identical input set, with its original revision labels.
        # Catalogs own source references, so object identities cannot be recycled.
        if len(sources) == 1:
            source = sources[0]
            identity = id(source)
            if identity not in self.source_catalogs:
                self.source_catalogs[identity] = Catalog([source])
            self.source_catalogs.move_to_end(identity)
            while len(self.source_catalogs) > 256:
                self.source_catalogs.popitem(last=False)
            return self.source_catalogs[identity]
        key = tuple(id(source) for source in sources)
        if key not in self.catalogs:
            parts = []
            for source in sources:
                identity = id(source)
                if identity not in self.source_catalogs:
                    self.source_catalogs[identity] = Catalog([source])
                self.source_catalogs.move_to_end(identity)
                parts.append(self.source_catalogs[identity])
            self.catalogs[key] = Catalog.combine(parts)
            while len(self.source_catalogs) > 256:
                self.source_catalogs.popitem(last=False)
        self.catalogs.move_to_end(key)
        while len(self.catalogs) > 8:
            self.catalogs.popitem(last=False)
        return self.catalogs[key]
