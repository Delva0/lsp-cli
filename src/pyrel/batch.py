"""Expand selectors, reuse revision workspaces and emit stable, deduplicated batches."""

import json
from copy import copy
from pathlib import PurePosixPath

from . import cli as legacy
from .discovery import SelectionError
from .evidence import fragments, head, render
from .git_queries import compare, history, view
from .listing import list_symbols
from .model import source_lines
from .planning import candidate_paths, inherited_inputs, lexical_candidate
from .relations import paths
from .selectors import Matcher, Selector, braces, expand, inputs, literal_address
from .semantics import expression_references
from .workspace import Workspace

ERRORS = (ValueError, OSError, SyntaxError, UnicodeError)


def failure(error, args, spec=None, ws=None, *, scope="query"):
    result = ws.result(args.command) if ws else getattr(error, "context", {})
    result = {
        **legacy.failure(error, args.command),
        **result,
        "results": [],
        "error": legacy.failure(error, args.command)["error"],
    }
    if isinstance(error, SelectionError):
        result["error"].update(status=error.status, candidates=[e.record() for e in error.candidates])
    if scope == "input":
        result["error"]["scope"] = scope
    result["diagnostics"] = list(result.get("diagnostics", []))
    if spec:
        result.update(input=spec.input, revision=spec.revision, _spec=spec)
    return result


def workspace(args, session, revision, specs):
    if args.blame and not revision:
        raise ValueError("show --blame requires a Git revision: REV:SYMBOL or --rev REV")
    selected = copy(args)
    selected.rev = revision
    selected.symbols = [
        literal
        for spec in specs
        for pattern in (braces(spec.pattern) if spec.mode == "glob" else [spec.pattern])
        if (literal := literal_address(pattern, spec.mode))
    ]
    if args.command in {"where", "complete"}:
        return Workspace(selected, session, defer_load=True)
    if revision and not args.source:
        ws = Workspace(selected, session, defer_load=True)
        matcher = Matcher(args)
        patterns = [(spec.pattern, spec.mode) for spec in specs]
        if args.command == "path":
            patterns += [(spec.destination, spec.destination_mode) for spec in specs]
        paths = set().union(*(
            candidate_paths(ws.loader, pattern, mode, matcher, scopes=args.command == "list")
            for pattern, mode in patterns
        ))
        # Prefer explicitly addressed files if the candidate set exceeds budget.
        # The other candidates remain visible as omitted, never as absent.
        for hint in selected.symbols if len(paths) > args.max_files else []:
            parts = hint.split(".")
            module = next((".".join(parts[:i]) for i in range(len(parts), 0, -1)
                           if ".".join(parts[:i]) in ws.loader.modules), None)
            preferred = (ws.loader.module_paths(module) if module else
                         ws.loader.search(parts[0], definition=True))
            for path in preferred:
                if path in paths:
                    ws.load(path)
        for path in sorted(paths):
            ws.load(path)
        if args.command not in {"list", "refs"}:
            ws.loaded = {path: source for path, source in ws.loaded.items()
                         if lexical_candidate(source, patterns, matcher)}
        return ws
    return Workspace(selected, session, selected.symbols[:1])


def ordered(entries):
    return sorted(entries, key=lambda e: (e.source.name, e.identifier["line"], e.identifier["column"], e.name))


def matches(ws, pattern, matcher, *, kinds=(), follow_import=True, mode="glob"):
    if follow_import:
        inherited_inputs(ws, pattern, mode, matcher)
    entries = [
        e
        for e in ws.catalog.entries
        if (not kinds or e.kind in kinds)
        and (not ws.selection_files or e.source.provenance.get("git_path") in ws.selection_files)
    ]
    candidates = matcher.entries(pattern, entries, mode=mode)
    # A module also has a file address, kept in the same matching/dedup pipeline.
    modules = [e for e in entries if e.kind == "module"]
    file_names = [e.source.provenance.get("git_path", e.source.name) for e in modules]
    if mode == "glob":
        candidates += [modules[i] for i in matcher.indices(pattern, file_names, domain="file")]
    resolved = {}
    for candidate in ordered(candidates):
        entry = ws.follow_import(candidate) if follow_import and candidate.kind == "import" else candidate
        resolved.setdefault((entry.identity, ""), (entry, ""))
    if follow_import:
        # Project possible member addresses cheaply, then prove only those that
        # match the pattern. A projection alone is never a returned binding.
        classes = {e.name: e for e in ws.catalog.entries if e.kind == "class"}
        members = {e.name.rsplit(".", 1)[-1] for e in ws.catalog.entries if e.parent in classes}
        declared = {e.name for e in ws.catalog.entries}
        eligible = set()
        if mode == "glob":
            # Filter before the class/member product. A precise method request
            # should not materialize every possible inherited name in a repo.
            member_names = sorted(members)
            class_names = list(classes)
            for expression in braces(pattern):
                parent, dot, member = expression.rpartition(".")
                # ** can span components; bracket expressions may contain a dot.
                # Keep these broader candidates for the final full-name matcher.
                broad = "[" in expression or "**" in member
                selected_members = (member_names if broad else
                                    [member_names[i] for i in matcher.indices(member, member_names)])
                selected_classes = ([class_names[i] for i in matcher.indices(parent, class_names)]
                                    if dot and not broad and "**" not in parent else class_names)
                eligible.update((name, member) for name in selected_classes for member in selected_members)
        else:
            eligible.update((name, member) for name in classes for member in members)
        projected = [
            (cls, member)
            for cls, member in ((classes[name], member) for name, member in sorted(eligible))
            if cls.node.bases
            and (not ws.selection_files or cls.source.provenance.get("git_path") in ws.selection_files)
            if cls.name + "." + member not in declared
        ]
        options = [name for cls, member in projected for name in (cls.name + "." + member, member)]
        for index in sorted({i // 2 for i in matcher.indices(pattern, options, mode=mode)}):
            cls, member = projected[index]
            inherited = ws.inherited(cls, member)
            if inherited and (not kinds or inherited.kind in kinds):
                receiver = ws.receivers.pop(inherited.identity, cls.name)
                resolved.setdefault((inherited.identity, receiver), (inherited, receiver))
    # Explicit inherited/aliased addresses keep their receiver, rather than
    # pretending their implementation is a declaration in the subclass.
    for expression in braces(pattern) if mode == "glob" else [pattern]:
        literal = literal_address(expression, mode)
        if not literal or "." not in literal or any(
            matcher.indices(literal, [entry.name, receiver + "." + entry.name.rsplit(".", 1)[-1]])
            for entry, receiver in resolved.values()
        ):
            continue
        try:
            entry = ws.select(literal)
            receiver = ws.receivers.pop(entry.identity, "")
            if not kinds or entry.kind in kinds:
                resolved[entry.identity, receiver] = entry, receiver
        except SelectionError as error:
            if error.status not in {"not_found", "unresolved"}:
                raise
    return list(resolved.values())


def scopes(ws, pattern, matcher, mode="glob"):
    if not pattern:
        return [None]
    # Preserve the selected binding: its qualified name can also name an
    # import in another file. Listing is plural, not a single-binding lookup.
    scopes = [entry for entry, _ in matches(ws, pattern, matcher, follow_import=False, mode=mode)]
    module_names = {entry.name for entry in scopes if entry.kind == "module"}
    directories = set()
    packages = set()
    for source in ws.sources:
        path = source.provenance.get("git_path", source.name)
        if ws.selection_files and path not in ws.selection_files:
            continue
        directories.update(str(p) for p in PurePosixPath(path).parents if str(p) != ".")
        parts = source.module.split(".")
        packages.update(".".join(parts[:i]) for i in range(1, len(parts)))
    for extra, domain in ((sorted(directories), "file"), (sorted(packages), "symbol")):
        if domain == "symbol" or mode == "glob":
            scopes += [
                extra[i]
                for i in matcher.indices(
                    pattern.rstrip("/") if domain == "file" else pattern, extra, mode=mode, domain=domain,
                )
                if domain != "symbol" or extra[i] not in module_names
            ]
    return scopes


def no_match(ws, spec, target="symbols"):
    if ws.metadata()["complete"]:
        return {**ws.result(ws.args.command), "status": "no_match"}
    error = SelectionError(
        f"Cannot establish whether {target} match {spec.pattern!r}: source coverage is incomplete", "incomplete", [],
    )
    return failure(error, ws.args, spec, ws)


def at_positions(ws, spec, matcher):
    from .commands import locate

    names = list(ws.loader.paths) if ws.loader else list(ws.loaded)
    if ws.selection_files:
        names = [name for name in names if name in ws.selection_files]
    selected = [names[i] for i in matcher.indices(spec.pattern, names, domain="file")]
    if not selected:
        return [no_match(ws, spec, "files")]
    results = []
    args = copy(ws.args)
    args.line, args.column = spec.line, spec.column
    for name in sorted(selected):
        try:
            source = ws.load(name)
            if source is None:
                raise ValueError(f"{name}: source could not be loaded; coverage is incomplete")
            result = locate(ws, args, source)
            result["matched"] = [source.identity()]
            result["heads"] = [{
                "source": source.name, "line": spec.line, "column": spec.column or 0,
                "end_line": spec.line, "end_column": spec.column or 0,
            }]
        except ERRORS as error:
            result = failure(error, args, spec, ws)
            result["error"]["file"] = name
            result["error"]["message"] = f"{name}:{spec.line}:{spec.column}: {error}"
        results.append(result)
    return results


def run_selector(ws, spec, matcher):
    from .commands import selected

    args = ws.args
    if args.command in {"where", "complete"}:
        return at_positions(ws, spec, matcher)
    if args.command == "list":
        roots = scopes(ws, spec.pattern, matcher, spec.mode)
        if not roots:
            return [no_match(ws, spec, "scopes")]
        return [list_symbols(ws, root, args.kinds or [], args.max_depth) for root in roots]
    kinds = ("class", "method") if args.command == "inheritance" else ()
    if args.command == "path":
        kinds = ("module", "class", "function", "method")
    entries = matches(ws, spec.pattern, matcher, kinds=kinds, mode=spec.mode)
    destinations = []
    if args.command == "path":
        destinations = matches(ws, spec.destination, matcher, kinds=kinds, mode=spec.destination_mode)
        if not entries or not destinations:
            from dataclasses import replace

            missing = replace(spec, pattern=spec.destination) if entries else spec
            result = no_match(ws, missing)
            result["heads"] = [entry.record() for entry, _ in entries + destinations]
            return [result]
    if not entries:
        if args.command == "refs" and (literal := literal_address(spec.pattern, spec.mode)):
            occurrences = expression_references(ws, literal, args.roles or [])
            if occurrences:
                result = ws.result("refs", fragments(ws, occurrences))
                result.update(selector=literal, roles=args.roles or [], occurrences=occurrences)
                return [result]
        return [no_match(ws, spec)]
    if args.command == "path":
        result = []
        for start, receiver in entries:
            ws.receivers.clear()
            if receiver:
                ws.receivers[start.identity] = receiver
            try:
                for row in paths(ws, start, destinations, args.max_depth):
                    if row["status"] == "no_resolved_path" and row.get("unresolved_frontier"):
                        row["status"] = "unresolved"
                    row["evidence"] = fragments(ws, row["results"])
                    result.append(row)
            except ERRORS as error:
                row = failure(error, args, spec, ws)
                row["symbol"] = start.record()
                result.append(row)
        return result
    results, cache = [], getattr(ws, "selected_results", {})
    ws.selected_results = cache
    for entry, receiver in entries:
        key = entry.identity, receiver
        try:
            if key not in cache:
                ws.receivers.clear()
                if receiver:
                    ws.receivers[entry.identity] = receiver
                if args.command == "history":
                    item = view(ws, entry)
                    item["results"] = [history(ws.loader, item["results"][0], args.max_count or 20)]
                    if item["results"][0]["truncated"]:
                        item["diagnostics"].append(
                            {"kind": "history_limit", "message": f"{entry.name}: increase -n for more history"},
                        )
                elif args.command == "diff":
                    item = view(ws, entry)
                else:
                    item = selected(ws, entry, args)
                item["selected_as"] = receiver + "." + entry.name.rsplit(".", 1)[-1] if receiver else entry.name
                cache[key] = item
            results.append(copy(cache[key]))
        except ERRORS as error:
            item = failure(error, args, spec, ws)
            item["symbol"] = entry.record()
            results.append(item)
    return results


def revision_job(args, specs, session):
    try:
        ws = workspace(args, session, specs[0].revision, specs)
    except ERRORS as error:
        return [(spec, [failure(error, args, spec, scope="input")]) for spec in specs]
    matcher = Matcher(args)
    result = []
    for spec in specs:
        try:
            parts = run_selector(ws, spec, matcher)
        except ERRORS as error:
            parts = [failure(error, args, spec, ws)]
        for part in parts:
            part.update(input=spec.input, revision=spec.revision, _spec=spec)
        result.append((spec, parts))
    return result


def diff_batches(args, specs, session):
    from dataclasses import replace

    groups = {}
    for spec in specs:
        for revision in spec.revision.split(".."):
            item = replace(spec, revision=revision)
            groups.setdefault(revision, []).append(item)
    jobs = [(revision, lambda s, group=group: revision_job(args, group, s)) for revision, group in groups.items()]
    sides = dict(pair for group in session.parallel(jobs) for pair in group)
    result = []
    for spec in specs:
        before_rev, after_rev = spec.revision.split("..")
        before = sides[replace(spec, revision=before_rev)]
        after = sides[replace(spec, revision=after_rev)]
        parts = [p for p in before + after if "error" in p]
        old = {p["symbol"]["symbol"]: p for p in before if p.get("symbol") and "error" not in p}
        new = {p["symbol"]["symbol"]: p for p in after if p.get("symbol") and "error" not in p}
        old_complete = all(p.get("coverage", {}).get("complete", False) for p in before)
        new_complete = all(p.get("coverage", {}).get("complete", False) for p in after)
        for name in sorted(old.keys() | new.keys()):
            if (name not in old and not old_complete) or (name not in new and not new_complete):
                item = failure(
                    SelectionError(
                        f"Cannot establish absence of {name}: source coverage is incomplete", "incomplete", [],
                    ),
                    args,
                    spec,
                )
                item["sources"] = unique([s for p in before + after for s in p.get("sources", [])])
                item["diagnostics"] = unique([d for p in before + after for d in p.get("diagnostics", [])])
                parts.append(item)
                continue
            empty_old = {**before[0], "results": [], "symbol": None}
            empty_new = {**after[0], "results": [], "symbol": None}
            empty_old.pop("error", None)
            empty_new.pop("error", None)
            parts.append(compare(args, old.get(name, empty_old), new.get(name, empty_new)))
        if not parts:
            parts = [{
                **before[0], "status": "no_match",
                "sources": unique([s for p in before + after for s in p.get("sources", [])]),
                "diagnostics": unique([d for p in before + after for d in p.get("diagnostics", [])]),
            }]
        for part in parts:
            part.update(input=spec.input, revision=spec.revision, _spec=spec)
        result.append((spec, parts))
    return result


def unique(rows):
    return list({json.dumps(row, sort_keys=True, ensure_ascii=False): row for row in rows}.values())


def merge_source(rows):
    """Union overlapping source intervals; retain exact code and all semantic metadata."""
    groups = {}
    for row in rows:
        groups.setdefault(row["source"], []).append(row)
    merged = []
    for group in groups.values():
        for row in sorted(group, key=lambda r: (r["line"], r["end_line"])):
            if not merged or merged[-1]["source"] != row["source"] or row["line"] > merged[-1]["end_line"]:
                merged.append(dict(row))
                continue
            previous = merged[-1]
            # A signature can end before a one-line suite. Another selection
            # may include that suite; retain the union, not the shorter prefix.
            if (row["end_line"], row["end_column"]) > (previous["end_line"], previous["end_column"]):
                lines = source_lines(previous["code"] + "\n", keepends=True)
                incoming = source_lines(row["code"] + "\n", keepends=True)
                for offset, line in enumerate(incoming, row["line"] - previous["line"]):
                    if offset >= len(lines):
                        lines.append(line)
                    else:
                        left, right = lines[offset].rstrip("\r\n"), line.rstrip("\r\n")
                        ending = line[len(right):] if offset == len(lines) - 1 else lines[offset][len(left):]
                        lines[offset] = (right if len(right) > len(left) else left) + ending
                previous["code"] = "".join(lines).removesuffix("\n")
                previous["end_line"], previous["end_column"] = row["end_line"], row["end_column"]
            for field in ("matches", "blame", "context_lines"):
                if field in previous or field in row:
                    previous[field] = unique([*previous.get(field, []), *row.get(field, [])])
            # A merged source interval may represent several declarations.
            symbols = [*previous.get("symbols", []), *row.get("symbols", [])]
            symbols += [r["symbol"] for r in (previous, row) if r.get("symbol")]
            if symbols:
                previous["symbols"] = list(dict.fromkeys(symbols))
    return merged


def pack(args, pairs):
    items = [part for _, parts in pairs for part in parts]
    for item in items:
        if "heads" not in item:
            item["heads"] = [
                {**item[key], **(
                    {"selected_as": item["selected_as"]} if key == "symbol" and item.get("selected_as") else {}
                )}
                for key in ("symbol", "destination") if item.get(key)
            ]
    rows = unique([row for item in items for row in item["results"]])
    if args.command in {"show", "refs"}:
        rows = merge_source(rows)
    # Revision input order is retained; within it source positions are stable.
    revisions = list(dict.fromkeys(spec.revision for spec, _ in pairs))
    if args.command in {"list", "show", "refs", "diff"}:

        def order(row):
            prefix = row["source"].partition(":")[0]
            index = next((i for i, rev in enumerate(revisions) if rev and prefix in rev.split("..")), 0)
            return index, row["source"], row["line"], row["column"]

        rows.sort(key=order)
    lookup = {json.dumps(row, sort_keys=True): i for i, row in enumerate(rows)}

    def indices(part):
        if args.command in {"show", "refs"}:
            return list(
                dict.fromkeys(
                    i
                    for row in part["results"]
                    for i, candidate in enumerate(rows)
                    if row["source"] == candidate["source"]
                    and candidate["line"] <= row["line"] <= row["end_line"] <= candidate["end_line"]
                ),
            )
        return [lookup[json.dumps(row, sort_keys=True)] for row in part["results"]]

    queries, diagnostics = [], []
    for spec, parts in pairs:
        errors = [part["error"] for part in parts if "error" in part]
        ok = [part for part in parts if "error" not in part]
        coverage = all(part.get("coverage", {}).get("complete", True) for part in parts)
        status = (
            "partial"
            if errors and ok
            else errors[0].get("status", "error")
            if errors
            else "incomplete"
            if not coverage
            else "no_match"
            if all(p.get("status") == "no_match" for p in parts)
            else "ok"
            if any(p["results"] and p.get("status") != "empty" for p in parts)
            else "empty"
        )
        query = {
            "input": spec.input,
            "revision": spec.revision,
            "pattern": spec.pattern,
            "matching": spec.mode,
            "status": status,
            "matches": unique([p["symbol"] for p in parts if p.get("symbol")]),
            "result_indices": list(dict.fromkeys(i for part in parts for i in indices(part))),
            "coverage": unique([p["coverage"] for p in parts if "coverage" in p]),
            "backends": unique([p["backend"] for p in parts if "backend" in p]),
        }
        if spec.destination is not None:
            query["destination_pattern"] = spec.destination
            query["destination_matching"] = spec.destination_mode
        if spec.line is not None:
            query["position"] = {"line": spec.line, "column": spec.column}
        if errors:
            query["errors"] = errors
        # All per-target metadata uses indices, never duplicated source bodies.
        metadata = []
        for part in parts:
            fields = (
                "symbol",
                "heads",
                "selected_as",
                "destination",
                "receiver",
                "destination_receiver",
                "status",
                "mro",
                "resolution",
                "reason",
                "scope",
                "kinds",
                "truncated",
                "matched",
                "returned",
                "max_depth",
                "position",
                "targets",
                "incomplete",
                "visited",
                "unresolved_frontier",
            )
            item = {key: part[key] for key in fields if key in part}
            item["result_indices"] = indices(part)
            metadata.append(item)
            diagnostics += [
                {**d, "query_index": len(queries), "input": spec.input, "revision": spec.revision}
                for d in part.get("diagnostics", [])
            ]
        query["selections"] = metadata
        queries.append(query)
    result = {
        "schema_version": 3,
        "command": args.command,
        "queries": queries,
        "results": rows,
        "heads": unique([h for p in items for h in p["heads"]]),
        "sources": unique([s for p in items for s in p.get("sources", [])]),
        "diagnostics": unique(diagnostics),
        "status": "partial" if any("error" in p for p in items) else (
            "no_match" if all(p.get("status") == "no_match" for p in items) else "ok"
        ),
    }
    # Keep useful metadata at top level when unambiguous; the batch schema is
    # otherwise identical for one and many inputs.
    if len(items) == 1:
        result.update(
            {
                key: value
                for key, value in items[0].items()
                if key not in {"schema_version", "command", "results", "sources", "diagnostics", "documents", "_spec"}
            },
        )
    else:
        occurrences = [e for p in items for e in p.get("occurrences", []) if isinstance(e, dict)]
        if occurrences:
            result["occurrences"] = sorted(unique(occurrences), key=lambda e: (e["source"], e["line"], e["column"]))
    return result, items


def execute(args, session):
    """Keep input failures local, then execute independent revision groups."""
    specs, prefailed = [], {}
    targets = inputs(args)
    batches = [targets] if args.command == "path" or not targets else [[value] for value in targets]
    for batch in batches:
        try:
            specs.extend(expand(args, batch))
        except ERRORS as error:
            spec = Selector(" ".join(value for value, _mode in batch), None, "")
            specs.append(spec)
            prefailed[spec] = [failure(error, args, spec)]
    valid = list(dict.fromkeys(spec for spec in specs if spec not in prefailed))
    if args.command == "diff":
        by_spec = dict(diff_batches(args, valid, session))
    else:
        groups = {}
        for spec in valid:
            groups.setdefault(spec.revision, []).append(spec)
        jobs = [(rev, lambda s, group=group: revision_job(args, group, s)) for rev, group in groups.items() if rev]
        by_spec = dict(pair for result in session.parallel(jobs) for pair in result)
        if None in groups:
            by_spec.update(revision_job(args, groups[None], session))
    by_spec.update(prefailed)
    return pack(args, [(spec, by_spec[spec]) for spec in dict.fromkeys(specs)])


def render_batch(command, result, items):
    from .commands import text

    if command == "list":
        blocks = {}
        # Scope metadata is not a listing row: it may have been excluded by
        # --kinds or --max-depth. Render only the selected declarations.
        for row in result["results"]:
            blocks.setdefault(row["source"], []).append(text({"command": command, "results": [row]}))
        return "\n\n".join("\n".join(lines) for lines in blocks.values())
    if command == "diff":
        blocks, unchanged = [], {}
        for row in result["results"]:
            heads = [row[side] for side in ("before", "after") if row[side]]
            if row["status"] == "unchanged":
                # Broad selectors can match hundreds of unchanged definitions.
                # Keep their status per source pair; JSON retains every record.
                key = tuple(head["source"] for head in heads)
                group = unchanged.setdefault(key, {"heads": heads, "count": 0})
                group["count"] += 1
                continue
            blocks.append("\n".join([render(
                [fragment for side in ("before", "after") if row[side]
                 for fragment in row[side].get("fragments", [])] if row["status"] == "empty" else [],
                heads,
            ),
                       *(["DIFF " + row["status"]] if row["status"] != "empty" else []),
                       *([row["patch"]] if row["patch"] else [])]))
        for group in unchanged.values():
            count = group["count"]
            suffix = f" ({count} symbols)" if count > 1 else ""
            blocks.append(render([], group["heads"]) + "\nDIFF unchanged" + suffix)
        return "\n\n".join(blocks)
    if command == "history":
        return "\n\n".join(legacy.compact({"command": command, "results": [row]}) for row in result["results"])
    if command == "path":
        blocks = []
        for item in items:
            if "error" in item:
                continue
            if item.get("status") == "no_match":
                blocks.extend(head(record) for record in item["heads"])
                continue
            start, end = item["symbol"]["symbol"], item["destination"]["symbol"]
            if item.get("receiver"):
                start = item["receiver"] + "." + start.rsplit(".", 1)[-1]
            if item.get("destination_receiver"):
                end = item["destination_receiver"] + "." + end.rsplit(".", 1)[-1]
            label = f"{item.get('revision') or 'input'}:{start} -> {end}"
            blocks.append("\n".join([label, render([], item["heads"]), text({**item, "evidence": []})]))
        evidence = merge_source([row for item in items for row in item.get("evidence", [])])
        if evidence:
            blocks.append(render(evidence))
        return "\n\n".join(blocks)
    return "\n\n".join(dict.fromkeys(
        text(item) for item in items if "error" not in item and item.get("status") != "no_match"
    ))
