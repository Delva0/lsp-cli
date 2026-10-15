"""Source selection and per-input feedback for native Cypher queries."""

from __future__ import annotations

import json
import sys
import time
from collections import OrderedDict
from copy import copy
from pathlib import PurePosixPath
from types import SimpleNamespace

from . import cypher
from .code_graph import CodeGraph
from .discovery import Index, SelectionError, candidate
from .feedback import output


def parameters(specifications):
    result = {}
    for specification in specifications:
        name, separator, value = specification.partition("=")
        if not separator or not name.isidentifier():
            raise ValueError("--param requires NAME=JSON, e.g. --param 'field=\"cert_reqs\"'")
        if name in result:
            raise ValueError(f"duplicate query parameter: {name}")
        try:
            result[name] = json.loads(value)
        except ValueError as error:
            raise ValueError(f"invalid JSON for --param {name}: {error}") from error
    return result


def specifications(args):
    """Parse input addresses only. Cypher text stays entirely native."""
    git_mode = (args.repo is not None or args.rev is not None or any(":" in s for s in args.symbols)
                or bool(args.symbols and not args.source and not args.module and args.filename == "<stdin>"))
    values = args.symbols or [""]
    default_revision = args.rev or next((s.partition(":")[0] for s in args.symbols if ":" in s), None)
    result = []
    for value in values:
        revision, separator, selector = value.partition(":")
        if separator:
            if not revision or not selector:
                raise ValueError(f"expected REV:NAME, got {value!r}")
        elif git_mode:
            revision, selector = (default_revision, value) if default_revision else (value or "HEAD", "")
        else:
            revision, selector = "", value
        revisions = [revision]
        if ".." in revision:
            revisions = revision.split("..")
            if len(revisions) != 2 or not all(revisions) or any(r.startswith(".") for r in revisions):
                raise ValueError("a revision range requires OLD..NEW[:NAME]")
        for revision in revisions:
            address = f"{revision}:{selector}" if revision and selector else revision or selector or "<input>"
            result.append({"input": len(result), "address": address, "revision": revision, "selector": selector,
                           "status": "pending", "symbols": [], "candidates": []})
    return git_mode, result


def rejected(record, error, *, loader=None):
    record.update(status=getattr(error, "status", "error"), message=str(error))
    if record["status"] == "not_found":
        record["message"] = (f"address {record['address']!r} could not be resolved; "
                             "use a qualified Python name or exact repository-relative .py path. "
                             + str(error))
    if getattr(error, "binding", None):
        record["binding"] = error.binding
    alternatives = getattr(error, "candidates", [])
    # Offer actual declarations elsewhere; retain the failed requested identity.
    if loader and isinstance(error, SelectionError) and error.status == "not_found" and "/" not in record["selector"]:
        alternatives = loader.query_candidates(record["selector"].rsplit(".", 1)[-1]) or alternatives
    elif loader and isinstance(error, SelectionError) and "/" in record["selector"]:
        from .discovery import definitions
        alternatives = []
        for path in loader.paths:
            if PurePosixPath(path).name == PurePosixPath(record["selector"]).name:
                source = loader.source(path)
                if source:
                    alternatives.append(definitions(source)[0])
    if loader and (loader.omitted or loader.failed):
        record.update(status="incomplete", message=record["message"] + "; source discovery was incomplete")
        record["discovery_complete"] = False
    record["candidates"] = [candidate(s, record["revision"]) for s in alternatives]


def load(args, session, records, git_mode):
    from .cli import inputs

    groups = OrderedDict()
    if not git_mode:
        if args.import_root or args.max_files != 128:
            raise ValueError("--import-root and --max-files require a Git revision")
        sources = inputs(args, sys.stdin, session)
        groups[""] = {"sources": {s.name: s for s in sources}, "records": records, "loaders": {}, "metadata": []}
        return groups
    if args.module or args.filename != "<stdin>" or args.root:
        raise ValueError("Git mode reads revision files; use --import-root for module identity")
    for record in records:
        revision, selector = record["revision"], record["selector"]
        try:
            loader = session.git(args, revision)
            record["git_commit"] = loader.commit
            if selector:
                selectors = [selector, args.receiver.split(":")[-1]] if args.receiver else [selector]
                seeds = loader.query_seeds(selectors, args.source)
                seeds = [path for path in sorted(seeds) if loader.source(path)]
                sources, metadata, diagnostics = loader.collect("path", [], seeds, missing_ok=True)
            else:
                sources, metadata, diagnostics = loader.collect("outline", [], args.source)
            record["discovery_complete"] = metadata["complete"]
            record["diagnostics"] = list(diagnostics)
            group = groups.setdefault(revision, {"sources": {}, "records": [], "loaders": {}, "metadata": []})
            group["sources"].update((source.name, source) for source in sources)
            group["records"].append(record)
            group["loaders"][record["input"]] = loader
            group["metadata"].append(metadata)
        except (ValueError, OSError, SyntaxError, UnicodeError) as error:
            rejected(record, error)
    return groups


def materialize(args, session, groups):
    combined = SimpleNamespace(nodes={}, edges={}, expanded=set(), unresolved={}, diagnostics=[])
    coverages, sources, git = [], [], []
    for revision, group in groups.items():
        selected_sources = list(group["sources"].values())
        sources.extend(source.identity() for source in selected_sources)
        if group["metadata"]:
            metadata = dict(group["metadata"][0])
            if len(group["metadata"]) > 1:
                metadata.update(scope="combined_inputs", analyzed_files=len(selected_sources),
                                complete=all(m["complete"] for m in group["metadata"]),
                                searches=sum(m["searches"] for m in group["metadata"]),
                                parsed_files=len(set().union(*(set(l.parsed) for l in group["loaders"].values()))),
                                read_files=len(set().union(*(set(l.raw) for l in group["loaders"].values()))))
                for field in ("seed_files", "omitted_files", "external_modules"):
                    metadata[field] = sorted(set().union(*(set(m[field]) for m in group["metadata"])))
            git.append(metadata)
        if not selected_sources:
            for record in group["records"]:
                rejected(record, SelectionError(f"no declaration found for {record['selector']!r} in {revision}"),
                         loader=group["loaders"].get(record["input"]))
            continue
        try:
            analysis = session.analysis(selected_sources)
        except (ValueError, OSError, SyntaxError, UnicodeError) as error:
            for record in group["records"]:
                rejected(record, error)
            continue
        index = Index(analysis)
        selections, whole_input = [], False
        for record in group["records"]:
            try:
                if record["selector"]:
                    matches = index.select(record["selector"])
                    loader = group["loaders"].get(record["input"])
                    if loader and loader.searches and (loader.omitted or loader.failed):
                        raise SelectionError("cannot establish a unique name with incomplete discovery; "
                                             "select a qualified candidate or increase --max-files",
                                             "incomplete", [s for s, _ in matches])
                else:
                    whole_input = True
                    matches = [(analysis.symbols[s.module], "") for s in selected_sources]
                if args.receiver:
                    if len(matches) != 1 or len(group["records"]) != 1:
                        raise ValueError("--receiver requires one method scope")
                    receiver_rev, sep, receiver = args.receiver.partition(":")
                    if not sep:
                        receiver = receiver_rev
                    elif revision and group["loaders"][record["input"]].resolve_revision(receiver_rev) != record["git_commit"]:
                        raise ValueError("--receiver must refer to the method's Git commit")
                    symbol, _ = matches[0]
                    cls, _ = analysis.select(receiver)
                    if cls.kind != "class" or not symbol.owner or symbol.owner not in (analysis.mro(cls.name) or []):
                        raise ValueError("--receiver must be a class whose MRO contains the method's defining class")
                    matches = [(symbol, cls.name)]
                selections.extend(matches)
                record.update(status="resolved", symbols=[candidate(s, revision) for s, _ in matches])
                if record["selector"] in index.resolutions:
                    record["binding"] = index.resolutions[record["selector"]]
            except (ValueError, OSError, SyntaxError, UnicodeError) as error:
                rejected(record, error, loader=group["loaders"].get(record["input"]))
        if not selections:
            continue
        graph = CodeGraph(analysis, max_work=args.max_work, declarations=index.declarations)
        roots = graph.materialize(selections)
        for entity in [*graph.nodes.values(), *graph.edges.values()]:
            entity.props["revision"] = revision
        complete = (not graph.diagnostics and not graph.unresolved and all(m["complete"] for m in group["metadata"])
                    and all(r["status"] == "resolved" for r in group["records"])
                    and not any(d["kind"] in {"limit", "unresolved_scope"} for d in analysis.diagnostics))
        coverages.append({"revision": revision, "scope": "all_input_symbols" if whole_input else "forward_from_symbols",
                          "complete": complete, "roots": list(dict.fromkeys(n.id for n in roots)),
                          "root_symbols": list(dict.fromkeys(n.props["name"] for n in roots)),
                          "nodes": len(graph.nodes), "relationships": len(graph.edges),
                          "expanded_contexts": len(graph.expanded), "max_work": args.max_work,
                          "unresolved_calls": len(graph.unresolved)})
        combined.nodes.update(graph.nodes)
        combined.edges.update(graph.edges)
        combined.expanded.update(graph.expanded)
        combined.unresolved.update(graph.unresolved)
        combined.diagnostics.extend([*analysis.diagnostics, *graph.diagnostics])
        for record in group["records"]:
            combined.diagnostics.extend(record.get("diagnostics", []))
    return combined, coverages, sources, git


def execute(arguments, session):
    args = copy(arguments)
    if args.schema:
        if args.symbols or args.param:
            raise ValueError("--schema does not accept a query, scope or parameters")
        return cypher.schema(), None
    if not args.symbols:
        raise ValueError("Cypher text is required: pysym REV[:NAME] 'MATCH ... RETURN ...'; "
                         "use pysym --schema for the graph schema")
    text, args.symbols = args.symbols[-1], args.symbols[:-1]
    params = parameters(args.param)
    if args.max_work < 1 or args.timeout_ms < 1 or args.max_rows < 1:
        raise ValueError("--max-work, --timeout-ms and --max-rows must be positive")
    git_mode, records = specifications(args)
    if args.receiver and len(records) != 1:
        raise ValueError("--receiver requires one method scope")
    groups = load(args, session, records, git_mode)
    inputs_done = time.perf_counter()
    graph, coverages, sources, git = materialize(args, session, groups)
    analysis_done = time.perf_counter()
    failed = [record for record in records if record["status"] != "resolved"]
    complete = bool(coverages) and not failed and all(c["complete"] for c in coverages)
    scope = (coverages[0]["scope"] if len(coverages) == 1 else
             "multiple_revisions" if coverages else "no_resolved_inputs")
    coverage = {"scope": scope,
                "complete": complete, "roots": [root for c in coverages for root in c["roots"]],
                "root_symbols": [name for c in coverages for name in c["root_symbols"]],
                "nodes": len(graph.nodes), "relationships": len(graph.edges),
                "expanded_contexts": len(graph.expanded), "max_work": args.max_work,
                "unresolved_calls": len(graph.unresolved), "versions": coverages}
    result = {
        "schema_version": 1, "command": "query", "engine": cypher.ENGINE, "query": text,
        "parameters": params, "inputs": records, "sources": sources, "coverage": coverage,
        "diagnostics": graph.diagnostics, "unresolved_frontier": list(graph.unresolved.values()),
        "columns": [], "results": [],
        "status": "error" if len(failed) == len(records) else "partial" if failed else "ok" if complete else "incomplete",
    }
    if git:
        result["versions"] = git
        if len({m["revision"] for m in git}) == 1:
            result["git"] = git[0]
    try:
        native = cypher.run(graph, text, params, timeout_ms=args.timeout_ms, max_rows=args.max_rows)
        result.update(native)
        if native["truncated"]:
            if result["status"] == "ok":
                result["status"] = "incomplete"
            result["diagnostics"].append({"kind": "result_limit", "message": f"--max-rows {args.max_rows} reached; "
                                          "add a Cypher LIMIT or raise --max-rows"})
    except cypher.CypherError as error:
        result["error"] = {"kind": "CypherError", "message": str(error),
                           "help": "Native Ladybug Cypher; pysym --schema lists code properties. "
                                   "https://docs.ladybugdb.com/cypher/difference/"}
        result["status"] = "error"
    result["output"] = output(result)
    return result, (inputs_done, analysis_done)
