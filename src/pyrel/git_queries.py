"""Combine resolved Python definition spans with immutable Git diffs and line provenance.

Diff compares independently resolved definitions. History delegates line tracking
to Git and labels its location as the starting definition, rather than claiming
historical symbol identity. Blame retains both current and original line numbers.
"""

import ast
import difflib
import re
from copy import copy

from .model import source_lines
from .session import Session
from .source_view import show

SPAN = ("source", "line", "column", "end_line", "end_column")


def resolve(args, revision, session, *, missing_ok=False):
    if getattr(args, "actions", False):
        from .discovery import SelectionError
        from .workspace import Workspace

        selected = copy(args)
        selected.rev = revision
        workspace = Workspace(selected, session, args.symbols)
        try:
            entry = workspace.select(args.symbols[0])
        except SelectionError as error:
            if not missing_ok or error.status != "not_found" or not workspace.metadata()["complete"]:
                raise
            result = workspace.result(args.command)
            result["symbol"] = None
        else:
            if not workspace.metadata()["complete"]:
                raise ValueError("Cannot compare definitions with incomplete coverage; increase --max-files")
            result = view(workspace, entry)
        return workspace.loader, result
    loader = session.git(args, revision)
    from .fast_sources import NeedsBindingsError, source_query

    try:
        return loader, source_query(args, loader)
    except NeedsBindingsError:
        loader = session.git(args, revision)
    sources, metadata, diagnostics = loader.collect("show", args.symbols, args.source, missing_ok=missing_ok)
    analysis = session.analysis(sources) if sources else None
    symbol, receiver = analysis.select(args.symbols[0], missing_ok=missing_ok) if analysis else (None, "")
    if not metadata["complete"]:
        raise ValueError(
            "cannot establish a unique definition or its absence with incomplete source discovery; "
            "increase --max-files or use a qualified symbol and resolve the source diagnostics",
        )
    result = {
        "schema_version": 1,
        "command": args.command,
        "symbol": symbol.record() if symbol else None,
        "results": [show(symbol, receiver)] if symbol else [],
        "sources": [source.identity() for source in sources],
        "diagnostics": [*(analysis.diagnostics if analysis else []), *diagnostics],
        "git": metadata,
    }
    return loader, result


def view(ws, entry):
    """Build a diff/history view from an already resolved, shared workspace."""
    from .evidence import body, code_record

    rows = body(ws, entry)
    # Repeated writes are one lexical binding. Git tracks the complete
    # original interval spanning its declarations, with no concatenated
    # or invented source coordinates.
    view = code_record(
        entry.source,
        min(r["line"] for r in rows),
        max(r["end_line"] for r in rows),
        symbol=entry.name,
        kind=entry.kind,
        declarations=[r["selection"] for r in rows],
    )
    result = ws.result(ws.args.command, [view])
    result["symbol"] = entry.record()
    if getattr(ws.args, "roles", None):
        from .evidence import fragments
        from .semantics import events

        occurrences = events(ws, entry, ws.args.roles)
        result["selected"] = fragments(ws, occurrences, declarations=[entry])
        result["occurrences"] = occurrences
    return result


def unified(before, after, context):
    """Keep unified hunk coordinates relative to the original files."""
    rows = list(
        difflib.unified_diff(
            source_lines(before["code"]) if before else [],
            source_lines(after["code"]) if after else [],
            fromfile=before["source"] if before else "/dev/null",
            tofile=after["source"] if after else "/dev/null",
            n=context,
            lineterm="",
        ),
    )
    offsets = [(record["line"] - 1) if record else 0 for record in (before, after)]
    for index, row in enumerate(rows):
        match = re.fullmatch(r"@@ -(\d+)(,\d+)? \+(\d+)(,\d+)? @@(.*)", row)
        if match:
            a, ac, b, bc, tail = match.groups()
            rows[index] = f"@@ -{int(a) + offsets[0]}{ac or ''} +{int(b) + offsets[1]}{bc or ''} @@{tail}"
    return "\n".join(rows)


def diff(args, session):
    before_rev, sep, after_rev = (args.rev or "").partition("..")
    if not sep or not before_rev or not after_rev or after_rev.startswith(".") or ".." in after_rev:
        raise ValueError("diff requires OLD..NEW:SYMBOL, for example lsp diff v1..v2:Class.method")
    _, before = resolve(args, before_rev, session, missing_ok=True)
    _, after = resolve(args, after_rev, session, missing_ok=True)
    return compare(args, before, after)


def compare(args, before, after):
    old = next(iter(before["results"]), None)
    new = next(iter(after["results"]), None)
    if old is None and new is None:
        raise ValueError(f"symbol not found in either revision: {args.symbols[0]}")
    anchor = new or old
    status = (
        "added"
        if old is None
        else "deleted"
        if new is None
        else ("modified" if old["code"] != new["code"] else "unchanged")
    )
    result = dict(after if new else before)
    result.update(
        sources=before["sources"] + after["sources"],
        diagnostics=before["diagnostics"] + after["diagnostics"],
        versions={"before": before["git"], "after": after["git"]},
        heads=[{key: side[key] for key in (*SPAN, "symbol", "kind")} for side in (old, new) if side],
        results=[
            {
                "kind": "DIFF",
                "symbol": anchor["symbol"],
                **{k: anchor[k] for k in SPAN},
                "status": status,
                "before": old,
                "after": new,
                "patch": unified(old, new, args.unified if args.unified is not None else 3),
            },
        ],
    )
    if getattr(args, "roles", None):
        from .role_diff import selected_diff

        old_rows, new_rows = before.get("selected", []), after.get("selected", [])
        result.pop("selected", None)
        result.pop("occurrences", None)
        result["roles"] = args.roles
        result["occurrences"] = {"before": before.get("occurrences", []), "after": after.get("occurrences", [])}
        has_occurrences = any(result["occurrences"].values())
        if has_occurrences:
            patch, hunks = selected_diff(old_rows, new_rows, args.unified if args.unified is not None else 3)
        else:
            patch, hunks = "", []
            result["status"] = "empty"
        row = result["results"][0]
        row.update(
            patch=patch,
            hunks=hunks,
            symbol_status=status,
            status=((status if status in {"added", "deleted"} else "modified") if patch else "unchanged")
            if has_occurrences else "empty",
        )
        # Sparse selections are explicit fragments, never a fabricated
        # contiguous code string with an incorrect original span.
        for side, view, fragments_ in (("before", old, old_rows), ("after", new, new_rows)):
            row[side] = (
                {**{k: v for k, v in view.items() if k != "code"}, "fragments": fragments_} if view else None
            )
    return result


def history(loader, view, max_count):
    path = view["source"].removeprefix(loader.revision + ":")
    raw = (
        loader.git(
            "log",
            "--no-color",
            "--no-show-signature",
            "--no-notes",
            "--no-patch",
            "--no-ext-diff",
            "--no-textconv",
            "--find-renames=50%",
            "--encoding=UTF-8",
            "--format=%H%x00%P%x00%an%x00%aI%x00%s%x00",
            f"--max-count={max_count + 1}",
            "-L",
            f"{view['line']},{view['end_line']}:{path}",
            loader.commit,
        )
        if loader.read(path)
        else b""
    )
    fields = raw.decode().split("\0")
    if fields[-1].strip() or (len(fields) - 1) % 5:
        raise ValueError("unexpected Git line-history record format")
    commits = []
    for start in range(0, len(fields) - 1, 5):
        commit, parents, author, date, subject = fields[start : start + 5]
        commits.append(
            {"commit": commit.strip(), "parents": parents.split(), "author": author, "date": date, "subject": subject},
        )
    return {
        "kind": "HISTORY",
        "symbol": view["symbol"],
        **{k: view[k] for k in SPAN},
        "scope": "git_line_history",
        "location_role": "starting_definition",
        "max_count": max_count,
        "truncated": len(commits) > max_count,
        "commits": commits[:max_count],
    }


def blame(loader, view):
    path = view["source"].removeprefix(loader.revision + ":")
    raw = (
        loader.git(
            "-c",
            "core.quotePath=false",
            "blame",
            "--line-porcelain",
            "--root",
            "--encoding=UTF-8",
            "-L",
            f"{view['line']},{view['end_line']}",
            loader.commit,
            "--",
            path,
        )
        if loader.read(path)
        else b""
    )
    source = loader.parsed[path]
    rows, metadata, header = [], {}, None
    for line in raw.split(b"\n"):
        if line.startswith(b"\t"):
            if header is None or not all(key in metadata for key in ("filename", "author", "author-time", "summary")):
                raise ValueError("incomplete Git blame record")
            commit, original, current = header
            if not view["line"] <= current <= view["end_line"]:
                raise ValueError("Git blame returned a line outside the selected definition")
            filename = metadata["filename"]
            if filename.startswith('"'):
                filename = ast.literal_eval(filename)
            text = source.lines[current - 1].rstrip("\r\n")
            rows.append(
                {
                    "kind": "BLAME",
                    "symbol": view["symbol"],
                    "source": view["source"],
                    "line": current,
                    "column": 0,
                    "end_line": current,
                    "end_column": len(text),
                    "text": text,
                    "commit": commit,
                    "author": metadata["author"],
                    "author_time": int(metadata["author-time"]),
                    "author_timezone": metadata.get("author-tz", ""),
                    "subject": metadata["summary"],
                    "origin": {"source": f"{commit}:{filename}", "line": original},
                },
            )
            header, metadata = None, {}
            continue
        match = re.fullmatch(rb"([0-9a-f]{40,64}) (\d+) (\d+)(?: \d+)?", line)
        if match:
            header = (match[1].decode(), int(match[2]), int(match[3]))
        elif header is not None:
            key, _, value = line.partition(b" ")
            metadata[key.decode()] = value.decode()
    expected = min(len(source.lines), view["end_line"]) - view["line"] + 1
    if len(rows) != expected:
        raise ValueError("Git blame did not return every line in the selected definition")
    return rows


def query(args, revisions, session=None):
    session = session or Session()
    if args.filename != "<stdin>" or args.module or args.root or args.receiver:
        raise ValueError("diff/history/blame read Git definitions; use REV:SYMBOL and --import-root when needed")
    if args.command == "diff":
        if len(revisions) > 1:
            raise ValueError("diff accepts one revision range: OLD..NEW:SYMBOL")
        return diff(args, session)
    loader, result = resolve(args, args.rev or "HEAD", session)
    for revision in revisions[1:]:
        if loader.resolve_revision(revision) != loader.commit:
            raise ValueError("targets refer to different Git commits")
    view = result["results"][0]
    if args.command == "history":
        result["results"] = [history(loader, view, args.max_count or 20)]
        if result["results"][0]["truncated"]:
            result["diagnostics"].append(
                {
                    "kind": "history_limit",
                    "message": "More line history is available; increase --max-count to include it.",
                },
            )
    else:
        result["results"] = blame(loader, view)
    return result
