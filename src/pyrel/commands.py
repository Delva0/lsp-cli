"""Public lsp actions: discover symbols, read code, and follow relationships."""

from __future__ import annotations

import ast
import json
import sys
import textwrap
import time
from pathlib import Path

from . import __version__
from . import cli as legacy
from .completion import complete
from .evidence import body, code_record, fragments, render
from .relations import inheritance
from .semantics import REF_ROLES, ROLES, events, references
from .session import Session
from .symbols import KINDS

ACTIONS = ("list", "show", "refs", "inheritance", "path", "history", "diff", "where", "complete")


class ActionParser(legacy.Parser):
    def parse_args(self, args=None, namespace=None):
        args = list(sys.argv[1:] if args is None else args)
        if args and args[0] in getattr(self, "action_parsers", {}):
            indexed = [Argument(value, i) for i, value in enumerate(args[1:])]
            parsed = self.action_parsers[args[0]].parse_intermixed_args(indexed, namespace)
            parsed.command = args[0]
            ordered = [(s, "glob") for s in parsed.symbols] + [(s, "regex") for s in parsed.regexp]
            parsed.target_inputs = [(str(s), mode) for s, mode in sorted(ordered, key=lambda v: v[0].order)]
            parsed.symbols = list(map(str, parsed.symbols))
            parsed.regexp = list(map(str, parsed.regexp))
            return parsed
        return super().parse_args(args, namespace)


class Argument(str):
    """Preserve input order through argparse's intermixed/compact option parsing."""

    __slots__ = ("order",)

    def __new__(cls, value, order):
        instance = super().__new__(cls, value)
        instance.order = order
        return instance

    def __getitem__(self, item):
        return Argument(super().__getitem__(item), self.order)

    def split(self, *args, **kwargs):
        return [Argument(value, self.order) for value in super().split(*args, **kwargs)]

    def partition(self, *args):
        return tuple(Argument(value, self.order) for value in super().partition(*args))


def parser():
    result = ActionParser(
        prog="lsp",
        allow_abbrev=False,
        description="Read versioned Python source by symbols and semantic relationships.",
        epilog="Use lsp ACTION --help for action options. Addresses: REV:SYMBOL or REV:path.py. "
        "Quote '{v1,v2}:Worker.{run,close}' to expand sets inside lsp. "
        "Symbols use names/globs; -e selects regular expressions. File paths always use globs. "
        "Repository defaults to the current directory; unversioned input uses stdin/--files. "
        "Lines are one-based; columns are zero-based Unicode offsets; end positions are exclusive.",
    )
    result.add_argument("--version", action="version", version=f"lsp {__version__}")
    actions = result.add_subparsers(dest="command", required=True, parser_class=legacy.Parser)
    descriptions = {
        "list": "List symbols within input modules, a directory, a module or a lexical scope.",
        "show": "Read original source, optionally selected by semantic roles or annotated with Git blame.",
        "refs": "Read all references, including calls, inheritance, implementations and overrides.",
        "inheritance": "Inspect ancestors, descendants, overrides, implementations and C3 MRO.",
        "path": "Find one proven multi-hop path per matched source/destination pair, within each revision.",
        "history": "Find commits changing a symbol's source lines.",
        "diff": "Compare the union of symbols matching in OLD and NEW, including additions and deletions.",
        "where": "Find the enclosing symbol tree and binding at a source position.",
        "complete": "Complete names at a source position.",
    }
    targets = {
        "list": "[REVSET:]PATTERN; select directory/module/symbol scopes; omit for input modules",
        "history": "[REVSET:]PATTERN; revision defaults to HEAD; -n applies per symbol",
        "diff": "OLD..NEW:PATTERN; union matches by qualified name; exactly two revisions",
        "where": "[REVSET:]FILE_GLOB:LINE[:COLUMN]; file paths are repository-relative",
        "complete": "[REVSET:]FILE_GLOB:LINE:COLUMN; file paths are repository-relative",
    }
    usage = {
        "list": "[TARGET ...] [OPTIONS]",
        "path": "[REVSET:]FROM TO [OPTIONS]",
        "where": "[REVSET:]FILE_GLOB:LINE[:COLUMN] [TARGET ...] [OPTIONS]",
        "complete": "[REVSET:]FILE_GLOB:LINE:COLUMN [TARGET ...] [OPTIONS]",
    }
    examples = {
        "list": "lsp list '{v1,v2}:Worker' -r --kinds method",
        "show": "lsp show '{v1,v2}:Worker.{run,close}' --roles call return",
        "refs": "lsp refs 'v1:Worker.run' --roles call override",
        "inheritance": "lsp inheritance '{v1,v2}:Worker'",
        "path": "lsp path '{v1,v2}:start*' 'Client.send'",
        "history": "lsp history 'v1:Worker.run' -n 5",
        "diff": "lsp diff 'v1..v2:Worker.{run,close}' --roles call write",
        "where": "lsp where '{v1,v2}:pkg/**/*.py:42:8'",
        "complete": "lsp complete '{v1,v2}:pkg/**/client*.py:42:8'",
    }
    result.action_parsers = {}
    for action in ACTIONS:
        sub = actions.add_parser(
            action,
            allow_abbrev=False,
            help=descriptions[action],
            description=descriptions[action],
            usage="%(prog)s " + usage.get(action, "TARGET [TARGET ...] [OPTIONS]"),
            epilog="Example: " + examples[action]
            + "\nUnmatched targets are silent. Exit: 0 located, 1 no targets, 2 error.",
        )
        result.action_parsers[action] = sub
        sub.add_argument(
            "symbols",
            nargs="*",
            metavar="TARGET",
            help=targets.get(
                action,
                "[REVSET:]SYMBOL matches a name/qualified suffix or glob (*, **, ?, [], {A,B})",
            ),
        )
        if action in {"show", "refs", "diff"}:
            roles = REF_ROLES if action == "refs" else ROLES
            sub.add_argument(
                "--roles",
                "-R",
                nargs="+",
                choices=roles,
                metavar="ROLE",
                help="select a union with declaration and control context; roles: "
                + " ".join(roles),
            )
        if action == "show":
            sub.add_argument("--raw", action="store_true", help="one complete definition without locations")
            sub.add_argument(
                "-b", "--blame", action="store_true", help="attach Git commit attribution to displayed lines",
            )
        if action == "list":
            sub.add_argument("-r", "--recursive", action="store_true", help="include every descendant in the scope")
            sub.add_argument(
                "--kinds",
                "-k",
                nargs="+",
                choices=KINDS,
                metavar="KIND",
                help="filter output, not traversal; kinds: " + " ".join(KINDS),
            )
        if action in {"list", "path", "inheritance"}:
            sub.add_argument(
                "--max-depth",
                "-d",
                type=int,
                help="target is depth 0; default 1" if action == "list" else "traversal depth limit (default 32)",
            )
        if action == "history":
            sub.add_argument("-n", "--max-count", type=int, help="history commit limit")
        if action == "diff":
            sub.add_argument("-U", "--unified", type=int, help="diff context lines")
        matching = sub.add_argument_group("target matching")
        if action not in {"where", "complete"}:
            modes = matching.add_mutually_exclusive_group()
            modes.add_argument("-F", "--fixed-strings", action="store_true", help="literal symbol substring matching")
            modes.add_argument("-P", "--pcre2", action="store_true", help="use PCRE2 for -e expressions")
            matching.add_argument(
                "-e",
                "--regexp",
                action="append",
                default=[],
                metavar="[REVSET:]REGEX",
                help="symbol regex (rg); repeat or mix with ordinary targets; file globs are unchanged",
            )
        matching.add_argument(
            "-i", "--ignore-case", action="store_true", help="ignore case in symbol and file matching",
        )
        sub.add_argument("-j", "--json", action="store_true", help="structured evidence with exact original spans")
        sub.add_argument("--stats", action="store_true", help="write timing to stderr")
        inputs = sub.add_argument_group("source input")
        inputs.add_argument(
            "--files",
            "-f",
            dest="source",
            action="extend",
            nargs="+",
            default=[],
            metavar="GLOB",
            help="files/directories/globs (*, **, ?, [], {a,b}); targets go first; "
            "with REV, match its file tree; local MODULE=PATH assigns one file's identity",
        )
        inputs.add_argument("--stdin-filename", dest="filename", default="<stdin>", help="source label for piped input")
        inputs.add_argument("--module", help="module identity for stdin")
        inputs.add_argument("--root", type=Path, help="module root for explicit files")
        inputs.add_argument("--repo", type=Path, help="Git repository (default: current directory)")
        inputs.add_argument("--rev", help="explicit Git revision instead of REV: prefixes")
        inputs.add_argument(
            "--import-root",
            action="append",
            default=[],
            metavar="PATH",
            help="import root within a revision",
        )
        inputs.add_argument("--max-files", type=int, default=4096, help="source discovery limit (default 4096)")
        sub.set_defaults(
            roles=None,
            regexp=[],
            fixed_strings=False,
            pcre2=False,
            kinds=None,
            recursive=False,
            raw=False,
            blame=False,
            max_depth=None,
            max_count=None,
            unified=None,
            line=None,
            column=None,
            receiver=None,
            schema=False,
            param=[],
            max_work=4096,
            timeout_ms=30000,
            max_rows=1000,
            event=None,
            edge=None,
            role=None,
            actions=True,
        )
    return result


def validate(args):
    if args.pcre2 and not args.regexp:
        raise ValueError("-P/--pcre2 requires a symbol regex supplied with -e")
    if args.max_files < 1:
        raise ValueError("--max-files must be positive")
    if args.max_depth is not None:
        minimum = 0 if args.command == "list" else 1
        if args.max_depth < minimum:
            raise ValueError(f"{args.command} --max-depth must be >= {minimum}")
    elif args.command == "list":
        args.max_depth = None if args.recursive else 1
    else:
        args.max_depth = 32
    if args.max_count is not None and args.max_count < 1:
        raise ValueError("--max-count must be positive")
    if args.unified is not None and args.unified < 0:
        raise ValueError("--unified must be nonnegative")
    if args.raw and (args.json or args.roles or args.blame):
        raise ValueError("--raw requires one complete show target without --roles, --blame or --json")
    if args.roles:
        args.roles = list(dict.fromkeys(args.roles))


def locate(ws, args, source=None):
    if source is None and len(ws.sources) != 1:
        raise ValueError(f"{args.command} needs a file position such as REV:path.py:42:8")
    source = source or ws.sources[0]
    if args.command == "complete":
        return complete(ws, source, args.line, args.column)
    if not 1 <= args.line <= max(1, len(source.lines)):
        raise ValueError("Position is outside the source")
    if args.column is not None and args.column > len(source.lines[args.line - 1].rstrip("\r\n")):
        raise ValueError("Column is outside the source line")
    scopes = ws.catalog.scope_at(source, args.line, args.column)
    rows = [
        {
            "symbol": scope.name,
            "kind": scope.kind,
            "parent": scope.parent.name if scope.parent else None,
            **source.span(scope.node),
        }
        for scope in scopes
    ]
    targets = ws.definition(source, {"line": args.line, "column": args.column}) if args.column is not None else []
    result = ws.result("where", rows)
    result["position"] = {
        "source": source.name, "line": args.line, "column": args.column or 0,
        "end_line": args.line, "end_column": args.column or 0,
    }
    result["context"] = [code_record(source, args.line, args.line)]
    if args.column is not None:
        result["targets"] = [entry.record() for entry in targets]
    return result


def selected(ws, entry, args):
    if args.command == "show":
        if args.roles:
            occurrences = events(ws, entry, args.roles)
            rows = fragments(ws, occurrences, declarations=[entry])
        else:
            occurrences, rows = [], body(ws, entry)
            if not args.raw and entry.kind in {"variable", "field", "import"}:
                # A binding can have declarations in different branches. Keep
                # those guards visible without expanding unrelated statements.
                rows += fragments(ws, [], declarations=[entry])
        if args.blame:
            if not ws.loader:
                raise ValueError("show --blame requires a Git revision: REV:SYMBOL or --rev REV")
            from .git_queries import blame

            for row in rows:
                row["blame"] = blame(ws.loader, {**row, "symbol": entry.name})
        result = ws.result("show", rows)
        result.update(symbol=entry.record(), roles=args.roles or [], occurrences=occurrences)
        if args.roles and not occurrences:
            result["status"] = "empty"
        return result
    if args.command == "refs":
        occurrences = references(ws, entry, args.roles or [])
        result = ws.result("refs", fragments(ws, occurrences, declarations=[entry]))
        result.update(symbol=entry.record(), roles=args.roles or [], occurrences=occurrences)
        if not occurrences:
            result["status"] = "empty"
        return result
    if args.command == "inheritance":
        return inheritance(ws, entry, args.max_depth)
    raise ValueError(f"Unsupported resolved action: {args.command}")


def location(row):
    return f"{row['source']}:{row['line']}:{row['column']}-{row['end_line']}:{row['end_column']}"


def text(result):
    command, rows = result["command"], result["results"]
    if not rows and command == "diff":
        return ""
    if command in {"show", "refs"}:
        return render(rows)
    if command == "list":
        return "\n".join(f"{row['symbol']}\t{row['kind']}\t{location(row)}" for row in rows)
    if command == "where":
        chain = "\n".join(f"{row['symbol']}\t{location(row)}" for row in rows)
        targets = "\n".join(f"-> {row['symbol']}\t{location(row)}" for row in result.get("targets", []))
        return "\n".join(
            part for part in (location(result["position"]), chain, render(result["context"]), targets) if part
        )
    if command == "complete":
        return "\n".join([
            location(result["position"]),
            *(f"{row['label']}\t{' '.join(row['detail'].split())}\t{location(row)}" for row in rows),
        ])
    if command == "inheritance":
        mro = " -> ".join(result["mro"]) if result["mro"] else "unresolved: " + result["reason"]
        return "\n".join(
            [f"{location(result['symbol'])} {result['symbol']['symbol']}", "MRO: " + mro,
             *(f"{location(row)} {row['symbol']} {row['relation']} {row['target']}" for row in rows)],
        )
    if command == "path":
        chain = "\n".join(f"{row['caller']} -> {row['target']}\t{location(row)}" for row in rows)
        return "\n".join(part for part in (result["status"], chain, render(result.get("evidence", []))) if part)
    return legacy.compact(result)


def main(argv=None, *, shared=None):
    """Run explicit arguments locally, or dispatch the CLI to its reusable worker.

    Args:
        argv: Explicit arguments for embedding; omission selects the CLI transport.
        shared: Worker-owned resources reused across locally executed invocations.
    """
    if argv is None:
        from .service import invoke

        return invoke(sys.argv[1:])
    argv = list(argv)
    started = time.perf_counter()
    try:
        args = parser().parse_args(argv)
        validate(args)
    except ValueError as error:
        failure = legacy.failure(error, None)
        # A failed request must remain distinguishable from an empty search,
        # including when a consumer previews only the beginning of the JSON.
        result = {
            "schema_version": 3,
            "status": "error",
            "error": failure.pop("error"),
            **{key: value for key, value in failure.items() if key != "schema_version"},
            "queries": [],
            "heads": [],
        }
        if any(flag in argv for flag in ("--json", "-j")):
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"lsp: {error}", file=sys.stderr)
        return 2
    from .batch import execute

    with Session(shared=shared) as session:
        result, items = execute(args, session)
        errors = [item for item in items if "error" in item]
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.raw and not errors:
            if len(result["results"]) > 1:
                print(
                    "lsp: --raw needs one contiguous syntax unit; use --json for multiple declarations",
                    file=sys.stderr,
                )
                return 2
            if result["results"]:
                code = textwrap.dedent(result["results"][0]["code"])
                try:
                    ast.parse(code)
                except SyntaxError:
                    print(
                        "lsp: --raw needs a complete syntax unit; select the enclosing function/class/module",
                        file=sys.stderr,
                    )
                    return 2
                print(code)
        elif args.command in {"show", "refs"}:
            rendered = render(result["results"], result["heads"])
            if rendered:
                print(rendered)
        else:
            from .batch import render_batch

            if rendered := render_batch(args.command, result, items):
                print(rendered)
        if not args.json:
            seen = set()
            seen_errors = set()
            for item in items:
                if "error" in item:
                    error = item["error"]
                    # A workspace load failure affects every target, but is one
                    # input error. Keep per-query failures attributable to inputs.
                    label = "" if error.get("scope") == "input" else str(item.get("input", ""))
                    key = label, json.dumps(error, sort_keys=True)
                    if key not in seen_errors:
                        prefix = label + ": " if label else ""
                        print("lsp: " + prefix + error["message"], file=sys.stderr)
                        for candidate in error.get("candidates", []):
                            print(f"  {candidate['symbol']} {location(candidate)}", file=sys.stderr)
                        seen_errors.add(key)
                for diagnostic in item.get("diagnostics", []):
                    message = diagnostic["kind"] + ": " + diagnostic["message"]
                    if message not in seen:
                        print("lsp: " + message, file=sys.stderr)
                        seen.add(message)
    if args.stats:
        print(f"lsp timing: total={time.perf_counter() - started:.3f}s", file=sys.stderr)
    return 2 if errors else 1 if not result["heads"] and all(item.get("status") == "no_match" for item in items) else 0
