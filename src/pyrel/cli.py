"""Stdin-first command-line interface and stable JSON/text output."""

from __future__ import annotations

import argparse
import ast
import json
import sys
import time
from copy import copy
from pathlib import Path

from . import __version__, git_queries
from .model import module_name
from .queries import query
from .session import Session

COMMANDS = ("outline", "defs", "show", "callee", "callers", "refs", "trace", "inherits", "path", "where",
            "diff", "history", "blame", "query")
EVENTS = ("CALL", "ASSIGN", "ATTR_WRITE", "SUBSCRIPT_WRITE", "RETURN", "RAISE")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def parser(prog="lsp-cli", *, legacy=False, cypher=True):
    result = Parser(
        prog=prog,
        usage=None if legacy else "%(prog)s [OPTIONS] [REV[:NAME] ...] 'CYPHER'",
        description=("Versioned Python symbols, source, bindings, calls and execution events." if legacy else
                     "Native Cypher queries over versioned Python symbols, bindings and source events."),
        epilog=(f"Files: {prog} trace Class.method --file path.py. "
        f"Pipe: git show TAG:path.py | {prog} trace Class.method --stdin-filename path.py. "
        "Columns are zero-based Unicode character offsets; end positions are exclusive. "
        f"Git: {prog} show TAG:pkg.task.Worker.run (uses the current directory's repository). "
        f"Path: {prog} path TAG:pkg.task.Worker.run TAG:pkg.storage.Store.write. "
        "A module is also a symbol: show TAG:pkg.task. "
        f"History: {prog} history Class.method; {prog} blame TAG:Class.method; "
        f"{prog} diff OLD..NEW:Class.method. "
        "Git mode discovers related files in one commit; source code is never executed. "
        "Repeat targets for independent queries in input order (except path/where). "
        f"Example: {prog} show v1:Class.method v2:Class.method. "
        "Default text output includes original line numbers; --json includes results[].code. "
        + (f"Cypher: {prog} query REV:SYMBOL 'MATCH (s:Symbol {{root:true}})-[:CALLS]->(t) RETURN t'. "
           f"Use {prog} query --schema for native Ladybug Cypher schema and scope rules." if cypher else ""))
        if legacy else
        f"Git: {prog} TAG:Class.method 'MATCH (s:Symbol {{root:true}}) RETURN s.source_code'. "
        f"Calls: {prog} TAG:Class.method 'MATCH (s:Symbol {{root:true}})-[c:CALLS]->(t) RETURN c,t'. "
        f"Pipe: git show TAG:path.py | {prog} 'MATCH (s:Symbol) RETURN s.name'. "
        f"{prog} --schema describes graph properties, relations, scope and JSON response fields. "
        "Default text renders source_code as labeled source blocks with original line numbers. "
        "List multiple REV:NAME inputs, or OLD..NEW:NAME, before one Cypher. "
        "Names include assignment declarations and Python file paths. "
        "RETURN diff(a.source_code,b.source_code) compares two original bodies. "
        "Source columns are zero-based Unicode character offsets; end positions are exclusive.",
    )
    result.add_argument("--version", action="version", version=f"{prog} {__version__}")
    if legacy:
        result.add_argument("command", choices=COMMANDS if cypher else COMMANDS[:-1])
    else:
        result.set_defaults(command="query")
    result.add_argument("symbols", nargs="*", metavar="[REV:SCOPE] CYPHER" if not legacy else "[REV:]SYMBOL")
    result.add_argument(
        "--file",
        "-f",
        "--source",
        dest="source",
        action="append",
        default=[],
        metavar="[MODULE=]PATH",
        help="input file (repeatable); '-' reads stdin; MODULE sets its import identity",
    )
    result.add_argument(
        "--stdin-filename",
        "--filename",
        dest="filename",
        default="<stdin>",
        help="label for piped stdin; does not read a file (use --file PATH)",
    )
    result.add_argument("--module", help="fully qualified module identity for stdin")
    result.add_argument("--root", type=Path, help="derive file module names relative to this directory")
    result.add_argument("--repo", type=Path,
                        help="override the current Git repository (default revision HEAD)")
    result.add_argument("--rev", help="Git revision; discovers files in the current directory's repository")
    result.add_argument("--import-root", action="append", default=[], metavar="PATH",
                        help="Python import root within the revision (repeatable; default flat/src/package config)")
    result.add_argument("--max-files", type=int, default=128,
                        help="maximum Python files parsed in Git mode (default 128)")
    result.add_argument("--stats", action="store_true", help="print input/analysis/query timing to stderr")
    if cypher:
        result.add_argument("--schema", action="store_true",
                            help="print graph schema and response contract without reading source")
        result.add_argument("--param", action="append", default=[], metavar="NAME=JSON",
                            help="query parameter (repeatable)")
        result.add_argument("--max-work", type=int, default=4096, help="maximum graph contexts expanded (default 4096)")
        result.add_argument("--timeout-ms", type=int, default=30000, help="native execution timeout (default 30000 ms)")
        result.add_argument("--max-rows", type=int, default=1000, help="output row budget (default 1000)")
    else:
        result.set_defaults(schema=False, param=[], max_work=4096, timeout_ms=30000, max_rows=1000)
    result.add_argument("--json", action="store_true", help="emit schema_version=1 JSON")
    result.add_argument("--receiver", help="analyze an inherited method using this class's MRO")
    if not legacy:
        result.set_defaults(line=None, column=None, max_depth=32, max_count=None, unified=None,
                            event=None, edge=None, role=None)
        return result
    result.add_argument("--line", type=int, help="one-based line for where")
    result.add_argument("--column", type=int, help="optional zero-based character column for where")
    result.add_argument("--max-depth", type=int, default=32, help="maximum call-path length (default 32)")
    result.add_argument("-n", "--max-count", type=int, help="history commit limit (default 20)")
    result.add_argument("-U", "--unified", type=int, help="diff context lines (default 3)")
    result.add_argument(
        "--event",
        action="append",
        help="trace event kinds, comma-separated; repeatable: " + ",".join(EVENTS),
    )
    result.add_argument(
        "--edge",
        choices=("all", "resolved", "unresolved"),
        help="filter CALL targets in trace/callee/callers; other trace events remain visible",
    )
    result.add_argument(
        "--role",
        choices=("read", "write", "definition", "parameter", "import"),
        help="refs role filter",
    )
    return result


def addresses(args):
    """Separate revision-qualified symbols while retaining one immutable input revision."""
    revisions = [args.rev] if args.rev is not None else []

    def symbol(value):
        revision, sep, name = value.partition(":")
        if not sep:
            return value
        if not revision or not name:
            raise ValueError(f"expected REV:SYMBOL, got {value!r}")
        revisions.append(revision)
        return name

    args.symbols = [symbol(value) for value in args.symbols]
    if args.receiver:
        args.receiver = symbol(args.receiver)
    if revisions:
        args.rev = revisions[0]
    return list(dict.fromkeys(revisions))


def filters(args):
    """Validate filters before reading or analyzing source."""
    events = [kind.strip().upper() for group in args.event or [] for kind in group.split(",")]
    if args.command != "query" and (args.schema or args.param or args.max_work != 4096
                                    or args.timeout_ms != 30000 or args.max_rows != 1000):
        raise ValueError("--schema, --param, --max-work, --timeout-ms and --max-rows apply to query")
    if args.event is not None and (args.command != "trace" or not events or any(e not in EVENTS for e in events)):
        raise ValueError("--event applies to trace and accepts: " + ",".join(EVENTS))
    if args.edge is not None and args.command not in {"trace", "callee", "callers"}:
        raise ValueError("--edge applies to trace, callee and callers; it filters CALL records only")
    if args.role is not None and args.command != "refs":
        raise ValueError("--role applies to refs")
    if args.max_count is not None and (args.command != "history" or args.max_count < 1):
        raise ValueError("--max-count applies to history and must be positive")
    if args.unified is not None and (args.command != "diff" or args.unified < 0):
        raise ValueError("--unified applies to diff and must be nonnegative")
    return {
        key: value
        for key, value in {
            "event": sorted(set(events)) if args.event else None,
            "edge": args.edge,
            "role": args.role,
        }.items()
        if value is not None
    }


def filter_results(result, selected):
    """Keep source order, spans and context; filtering cannot create resolved edges."""
    if not selected:
        return
    result["filters"] = selected
    result["results"] = [
        row
        for row in result["results"]
        if (not selected.get("event") or row["kind"] in selected["event"])
        and (selected.get("edge", "all") == "all" or row["kind"] != "CALL" or row["resolution"] == selected["edge"])
        and (not selected.get("role") or row.get("role") == selected["role"])
    ]


def qualified_hints(body, module, symbols, prefix=""):
    hints = set()
    for node in body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            suffix = f"{module}.{prefix}{node.name}"
            hints.update(symbol[: -len(prefix + node.name) - 1] for symbol in symbols if symbol.endswith("." + suffix))
            hints.update(qualified_hints(node.body, module, symbols, prefix + node.name + "."))
    return hints


def inputs(args, stdin, session=None):
    session = session or Session()
    result = []
    paths = args.source or ["-"]
    used_stdin = False
    for specification in paths:
        explicit, sep, filename = specification.partition("=")
        if not sep:
            filename, explicit = explicit, ""
        if filename == "-":
            if used_stdin:
                raise ValueError("stdin may only be supplied once")
            used_stdin = True
            text = session.stdin(stdin)
            name = args.filename
            path = Path(name)
            module = explicit or args.module or ("__stdin__" if name == "<stdin>" else module_name(path))
            origin = "explicit" if explicit or args.module else "stdin" if name == "<stdin>" else "filename"
        else:
            path = Path(filename)
            text = session.file(path)
            name = filename
            if args.root:
                relative = path.resolve().relative_to(args.root.resolve())
            elif path.is_absolute():
                try:
                    relative = path.relative_to(Path.cwd())
                except ValueError:
                    relative = Path(path.name)
            else:
                relative = path
            module = explicit or module_name(relative)
            origin = "explicit" if explicit else "path"
        if not all(part.isidentifier() for part in module.split(".")):
            if origin == "explicit":
                raise ValueError(f"invalid module identity: {module}")
            origin = "file"
        source = session.source(name, module, text, path.name == "__init__.py", origin)
        # A fully qualified query explicitly supplies missing package identity
        # for exported bare files: requests.sessions.Session.send + sessions.py.
        # Require a unique exact definition suffix; never guess from imports.
        if origin == "path" and "." not in module:
            hints = qualified_hints(source.tree.body, module, args.symbols)
            if len(hints) == 1:
                source = session.source(name, hints.pop(), text, path.name == "__init__.py", "qualified_query")
            elif hints:
                raise ValueError(f"conflicting module identities for {filename}; use MODULE=PATH")
        result.append(source)
    names = [s.module for s in result]
    if len(set(names)) != len(names):
        flag = "--files" if getattr(args, "actions", False) else "--file"
        raise ValueError(f"duplicate module identity; assign distinct {flag} MODULE=PATH names")
    if len({s.name for s in result}) != len(result):
        raise ValueError("duplicate source label")
    return result


def location(record):
    return f"{record['source']}:{record['line']}:{record['column']}-{record['end_line']}:{record['end_column']}"


def scope_lines(result):
    """Render the same lexical records returned by JSON, grouped by source."""
    groups = {}
    for record in result["results"]:
        groups.setdefault(record["source"], []).append(record)
    lines = []
    position = result["position"]
    point = str(position["line"])
    if position["column"] is not None:
        point += f":{position['column']}"
    for source, records in groups.items():
        lines.append(f"{source}:{point}")
        children = {}
        for record in records:
            children.setdefault(record["parent"], []).append(record)

        def render(record, children, indent="", branch=""):
            span = f"{record['line']}:{record['column']}-{record['end_line']}:{record['end_column']}"
            lines.append(f"{indent}{branch}{record['kind']} {record['symbol']}  {span}")
            indent += "    " if branch == "└── " else "│   " if branch else ""
            nested = children.get(record["symbol"], [])
            for index, child in enumerate(nested):
                render(child, children, indent, "└── " if index == len(nested) - 1 else "├── ")

        names = {record["symbol"] for record in records}
        for record in records:
            if record["parent"] not in names:
                render(record, children)
    return lines or ["no matches"]


def compact(result):
    if "queries" in result:
        total = len(result["queries"])
        return "\n\n".join(f"[{index}/{total}] {result['command']} {item['input']}\n{compact(item)}"
                           for index, item in enumerate(result["queries"], 1))
    if result["command"] == "query" and "inputs" in result:
        from .cypher import compact as compact_query
        return compact_query(result)
    if "error" in result:
        return f"error ({result['error']['kind']}): {result['error']['message']}"
    if result["command"] == "query":
        from .cypher import compact as compact_query
        return compact_query(result)
    lines = []
    if "git" in result:
        git = result["git"]
        lines.append(f"git {git['revision']} @ {git['commit']} | {git['analyzed_files']} analyzed files "
                     f"/ {git['parsed_files']} parsed / {git['discovered_files']} discovered")
    if result.get("status"):
        lines.append(f"path: {result['status']}")
    if result["command"] == "where":
        lines.extend(scope_lines(result))
        lines.extend(f"{d['kind']}: {d['message']}" for d in result.get("diagnostics", []))
        return "\n".join(lines)
    for record in result["results"]:
        prefix = location(record)
        kind = record.get("kind", "")
        context = " / ".join(
            c["kind"] + (f"({c['label']})" if c.get("label") else "") for c in record.get("context", [])
        )
        suffix = f" [{context}]" if context else ""
        if record.get("unreachable"):
            suffix += " [unreachable]"
        if kind == "SOURCE":
            lines.append(f"{prefix} {record['symbol_kind']} {record['symbol']}")
            width = len(str(record["end_line"]))
            lines.extend(f"{number:>{width}} | {line}"
                         for number, line in enumerate(record["code"].splitlines(), record["line"]))
            continue
        if kind == "DIFF":
            lines.append(f"{prefix} DIFF {record['status']} {record['symbol']}")
            if record["patch"]:
                lines.append(record["patch"])
            continue
        if kind == "HISTORY":
            lines.append(f"{prefix} HISTORY {record['symbol']} (Git line history from this definition)")
            lines.extend(f"{commit['commit']} {commit['date']} {commit['author']} | {commit['subject']}"
                         for commit in record["commits"])
            continue
        if kind == "BLAME":
            lines.append(f"{prefix} {record['commit'][:12]} {record['author']} | {record['text']}")
            continue
        if kind == "CALL":
            target = record.get("target") or f"unresolved ({record.get('reason', '')})"
            expression = " ".join(record["text"].split()) if result["command"] == "trace" else record["callee"]
            detail = f"{expression} -> {target}"
        elif kind == "REF":
            target = record.get("target") or (record["binding"] if record["resolution"] == "resolved" else "unresolved")
            expression = " ".join(record.get("text", record["expression"]).split())
            detail = f"{record['role']} {expression} [{record['scope']}] -> {target}"
        elif kind in {"ASSIGN", "ATTR_WRITE", "SUBSCRIPT_WRITE", "RETURN", "RAISE"}:
            detail = " ".join(record["text"].split())
        else:
            detail = record.get("symbol", "")
            if "mro" in record:
                detail += " MRO: " + (
                    " -> ".join(record["mro"]) if record["mro"] else f"unresolved ({record['reason']})"
                )
        lines.append(f"{prefix} {kind} {detail}{suffix}")
    frontier = result.get("unresolved_frontier", [])
    lines.extend(
        f"{location(record)} unresolved {record['callee']}: {record.get('reason', '')}" for record in frontier[:20]
    )
    if len(frontier) > 20:
        lines.append(f"{len(frontier) - 20} more unresolved call sites; --json includes the complete frontier")
    lines.extend(f"{diagnostic['kind']}: {diagnostic['message']}" for diagnostic in result.get("diagnostics", []))
    return "\n".join(lines) or "no matches"


def emit(result, args, prog, started, phases=None):
    if "error" in result and not args.json and not (result.get("command") == "query" and "inputs" in result):
        print(f"{prog}: {result['error']['message']}", file=sys.stderr)
        if result["error"].get("help"):
            print(result["error"]["help"], file=sys.stderr)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else compact(result))
    if args.stats and "error" not in result:
        elapsed = time.perf_counter()
        timing = f"total={elapsed - started:.3f}s"
        if phases:
            inputs_done, analysis_done = phases
            timing = (f"inputs={inputs_done - started:.3f}s analysis={analysis_done - inputs_done:.3f}s "
                      f"query={elapsed - analysis_done:.3f}s")
        print(f"{prog} timing: {timing}", file=sys.stderr)
    return 2 if ("error" in result or any("error" in row for row in result.get("queries", []))
                 or any(row["status"] != "resolved" for row in result.get("inputs", []))) else 0


def failure(error, command, *, empty_stdin=False):
    record = {"message": str(error), "kind": type(error).__name__}
    if empty_stdin:
        record["message"] += (
            "; stdin was empty. --filename/--stdin-filename label stdin, not a file to read; "
            "use --file PATH or pipe Python source."
        )
    if isinstance(error, SyntaxError):
        record.update(
            source=error.filename,
            line=error.lineno,
            column=max(0, (error.offset or 1) - 1),
            end_line=error.end_lineno or error.lineno,
            end_column=max(0, (error.end_offset or error.offset or 1) - 1),
        )
    return {"schema_version": 1, "command": command, "sources": [], "results": [],
            "diagnostics": [], "error": record}


def execute(args, session, selected_filters, prog):
    """Run one query using invocation-local caches and an independent failure boundary."""
    sources = []
    try:
        if args.command == "query":
            from .cypher_cli import execute as execute_query
            return execute_query(args, session)
        revisions = addresses(args)
        expected = 2 if args.command == "path" else 0 if args.command in {"outline", "where"} else 1
        if expected == 0 and len(args.symbols) == 1:
            if args.repo is None and args.rev is None:
                raise ValueError(f"{args.command} accepts a module target in Git mode: "
                                 f"{prog} {args.command} REV:MODULE")
        elif len(args.symbols) != expected:
            raise ValueError(f"{args.command} requires {expected} symbol argument(s)")
        if args.command in {"diff", "history", "blame"}:
            return git_queries.query(args, revisions, session), None
        git_metadata, input_diagnostics = None, []
        if args.repo is not None or args.rev is not None:
            if args.module or args.filename != "<stdin>" or args.root:
                raise ValueError("Git mode reads revision files; use --import-root for module identity")
            loader = session.git(args, args.rev or "HEAD")
            for revision in revisions[1:]:
                if loader.resolve_revision(revision) != loader.commit:
                    raise ValueError("targets refer to different Git commits; use one revision for this query. "
                                     "Cross-version relationship comparison is not supported.")
            from .fast_sources import NeedsBindingsError, source_query

            fallback = []
            try:
                if args.command in {"show", "defs", "outline", "where"}:
                    result = source_query(args, loader)
                    return result, (time.perf_counter(), time.perf_counter())
                if args.command in {"callee", "callers", "refs"}:
                    from .lsp_queries import relationship

                    result = relationship(args, loader, session)
                    filter_results(result, selected_filters)
                    return result, (time.perf_counter(), time.perf_counter())
            except NeedsBindingsError as error:
                if str(error).startswith("LSP "):
                    fallback.append({"kind": "lsp_fallback", "message": str(error) + "; using the binding engine"})
                loader = session.git(args, args.rev or "HEAD")
            sources, git_metadata, input_diagnostics = loader.collect(
                args.command, args.symbols, args.source, args.receiver or "",
            )
            input_diagnostics = [*fallback, *input_diagnostics]
        else:
            if args.import_root or args.max_files != 128:
                raise ValueError("--import-root and --max-files require --rev or --repo")
            sources = inputs(args, sys.stdin, session)
        inputs_done = time.perf_counter()
        analysis = session.analysis(sources)
        analysis_done = time.perf_counter()
        result = query(
            analysis,
            args.command,
            args.symbols,
            line=args.line,
            column=args.column,
            receiver=args.receiver or "",
            max_depth=args.max_depth,
        )
        filter_results(result, selected_filters)
        if git_metadata is not None:
            result["git"] = git_metadata
            result["diagnostics"].extend(input_diagnostics)
            if not git_metadata["complete"] and result.get("status") == "no_resolved_path":
                result["status"] = "incomplete"
        if git_metadata is None and all(not source.text for source in sources) and not args.source:
            result["diagnostics"].append(
                {
                    "kind": "empty_stdin",
                    "message": "stdin was empty; --filename labels stdin, not a file to read. "
                    "Use --file PATH or pipe Python source.",
                },
            )
    except (ValueError, OSError, SyntaxError, UnicodeError) as error:
        empty = (sources and all(not source.text for source in sources) and not args.source
                 and args.repo is None and args.rev is None)
        return failure(error, args.command, empty_stdin=empty), None
    else:
        return result, (inputs_done, analysis_done)


def main(argv=None, *, prog="lsp-cli", legacy=False, cypher=True):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = None
    started = time.perf_counter()
    try:
        args = parser(prog, legacy=legacy, cypher=cypher).parse_intermixed_args(argv)
        if args.command != "where" and (args.line is not None or args.column is not None):
            raise ValueError("--line and --column apply to where")
        selected_filters = filters(args)
    except (ValueError, OSError, SyntaxError, UnicodeError) as error:
        command = args.command if args else (
            next((arg for arg in argv if arg in COMMANDS), None) if legacy else "query"
        )
        result = failure(error, command)
        if "--json" in argv:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"{prog}: {result['error']['message']}", file=sys.stderr)
        return 2
    with Session() as session:
        if len(args.symbols) > 1 and args.command not in {"path", "where", "query"}:
            groups = []
            for address in args.symbols:
                item = copy(args)
                item.symbols = [address]
                result, _ = execute(item, session, selected_filters, prog)
                groups.append({"input": address, **result})
            errors = sum("error" in row for row in groups)
            result = {"schema_version": 1, "command": args.command,
                      "status": "error" if errors == len(groups) else "partial" if errors else "ok", "queries": groups}
            return emit(result, args, prog, started)
        result, phases = execute(args, session, selected_filters, prog)
        return emit(result, args, prog, started, phases)


def rgit_main():
    """Expose the shared semantic engine through the Git-style CLI name."""
    return main(prog="rgit")


def legacy_main(argv=None, *, prog="pyrel"):
    """Retain the combined command and Cypher entry for internal replays."""
    return main(argv, prog=prog, legacy=True)


def subcommand_main(argv=None):
    """Expose the symbol subcommands through the public lsp-cli executable."""
    return main(argv, prog="lsp-cli", legacy=True, cypher=False)
