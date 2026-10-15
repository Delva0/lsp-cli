"""One compact source renderer for show and refs; metadata never edits code."""

import ast
import tokenize
from collections import defaultdict

from .cli import location
from .model import source_lines


def code_record(source, first, last, **extra):
    lines = source.lines[first - 1 : last]
    code = "".join(lines)
    code = code[:-2] if code.endswith("\r\n") else code[:-1] if code.endswith(("\n", "\r")) else code
    return {
        "source": source.name,
        "line": first,
        "column": 0,
        "end_line": last,
        "end_column": len(lines[-1].rstrip("\r\n")) if lines else 0,
        "code": code,
        **extra,
    }


def body(ws, entry):
    source = entry.source
    rows = []
    for node in entry.declarations:
        span = source.span(node)
        if entry.kind == "parameter":
            span = ws.catalog.header(source, entry.scope.node)
        first = min([span["line"], *(d.lineno for d in getattr(node, "decorator_list", []))])
        rows.append(
            code_record(
                source,
                first,
                span["end_line"],
                symbol=entry.name,
                kind=entry.kind,
                selection=entry.identifier,
            ),
        )
    return rows


def fragments(ws, occurrences, *, declarations=()):
    """Select real occurrences plus declaration context, without inventing role hits."""
    selected = defaultdict(set)
    context = defaultdict(set)
    full = defaultdict(set)
    ends = {}
    by_source = {source.name: source for source in ws.sources}
    catalog = ws.catalog

    def include_header(source, node, *, binding=None):
        header = catalog.header(source, node)
        if binding is not None and isinstance(node, (
            ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr, ast.Import, ast.ImportFrom,
        )):
            # Bindings have no def/class signature. Keep their declaration line,
            # including multiline targets/types, without expanding a multiline value.
            last = min(header["end_line"], max(
                header["line"], binding["end_line"],
                getattr(getattr(node, "value", None), "lineno", header["line"]),
            ))
            if last < header["end_line"]:
                header = {**header, "end_line": last,
                          "end_column": len(source.lines[last - 1].rstrip("\r\n"))}
        numbers = range(header["line"], header["end_line"] + 1)
        selected[source.name].update(numbers)
        context[source.name].update(numbers)
        key = source.name, header["end_line"]
        ends[key] = max(ends.get(key, 0), header["end_column"])
        for decorator in getattr(node, "decorator_list", []):
            numbers = range(decorator.lineno, decorator.end_lineno + 1)
            selected[source.name].update(numbers)
            context[source.name].update(numbers)
            full[source.name].update(numbers)

    def ancestors(source, node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            include_header(source, node)
        while id(node) in catalog.parents:
            parent = catalog.parents[id(node)]
            include_parent(source, node, parent)
            node = parent

    def include_parent(source, node, parent):
        if isinstance(parent, ast.match_case):
            first = parent.pattern.lineno
            depth, last = 0, first
            for token in catalog.tokens[source.name]:
                if token.start[0] < first:
                    continue
                if token.type == tokenize.OP:
                    if token.string in {"(", "[", "{"}:
                        depth += 1
                    elif token.string in {")", "]", "}"}:
                        depth -= 1
                    elif token.string == ":" and depth == 0:
                        last = token.end[0]
                        ends[source.name, last] = token.end[1]
                        break
            selected[source.name].update(range(first, last + 1))
            context[source.name].update(range(first, last + 1))
        if isinstance(parent, (
            ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.If, ast.For, ast.AsyncFor,
            ast.While, ast.With, ast.AsyncWith, ast.Try, ast.TryStar, ast.ExceptHandler, ast.Match,
        )):
            include_header(source, parent)
            # else/finally have no AST nodes; retain their original header.
            for field in ("orelse", "finalbody"):
                branch = getattr(parent, field, [])
                if node in branch and branch:
                    for number in range(branch[0].lineno - 1, parent.lineno - 1, -1):
                        line = source.lines[number - 1].lstrip()
                        if line.startswith(("else:", "finally:", "elif ")):
                            selected[source.name].add(number)
                            context[source.name].add(number)
                            break

    for entry in declarations:
        for node in entry.declarations:
            ancestors(entry.source, node)
            if entry.kind in {"variable", "field", "import"}:
                include_header(entry.source, node, binding=entry.identifier)
    for event in occurrences:
        source = by_source[event["source"]]
        span = event.get("statement", event)
        selected[source.name].update(range(span["line"], span["end_line"] + 1))
        full[source.name].update(range(span["line"], span["end_line"] + 1))
        point = (event["line"], event["column"])
        nodes = source.nodes_at(point)
        if nodes:
            node = min(nodes, key=lambda n: len(source.segment(n)))
            ancestors(source, node)
    rows = []
    for name, numbers in selected.items():
        source = by_source[name]
        limits = {
            number: ends.get((name, number), len(source.lines[number - 1].rstrip("\r\n")))
            if number not in full[name] else len(source.lines[number - 1].rstrip("\r\n"))
            for number in numbers
        }
        groups = []
        for number in sorted(numbers):
            if (groups and number == groups[-1][-1] + 1
                    and limits[number - 1] == len(source.lines[number - 2].rstrip("\r\n"))):
                groups[-1].append(number)
            else:
                groups.append([number])
        for group in groups:
            matches = [
                event for event in occurrences if event["source"] == name and group[0] <= event["line"] <= group[-1]
            ]
            row = code_record(
                source, group[0], group[-1], matches=matches,
                context_lines=sorted(context[name].intersection(group)),
            )
            # A one-line suite is not part of a declaration's signature.
            removed = row["end_column"] - limits[group[-1]]
            if removed:
                row["code"] = row["code"][:-removed]
                row["end_column"] = limits[group[-1]]
            rows.append(row)
    return rows


def head(record):
    """A located target, independent of whether the action returned any content."""
    label = record.get("symbol", "")
    if record.get("selected_as") and record["selected_as"] != label:
        label = record["selected_as"] + " -> " + label
    return (location(record) + " " + label).rstrip()


def render(rows, heads=()):
    files = {}
    for record in heads:
        files.setdefault(record["source"], {})
    annotations = defaultdict(set)
    provenance = {}
    for row in rows:
        name = row["source"]
        lines = files.setdefault(name, {})
        code = row.get("code", "")
        physical = source_lines(code + "\n")
        for number, text in enumerate(physical, row["line"]):
            lines[number] = text.rstrip("\r\n")
        for attribution in row.get("blame", []):
            provenance[name, attribution["line"]] = attribution
        for event in row.get("matches", []):
            if event.get("resolution") == "unresolved":
                annotations[name, event["line"]].add("unresolved: " + event.get("reason", "unknown target"))
            elif event.get("kind") == "CALL" and event.get("callee") and event.get("target"):
                annotations[name, event["line"]].add(event["callee"] + " -> " + event["target"])
    output = []
    for name, lines in files.items():
        if output:
            output.append("")
        output.append(name)
        width = len(str(max(lines, default=1)))
        for number, text in sorted(lines.items()):
            owner = provenance.get((name, number))
            label = f" {owner['commit'][:12]} ({owner['author']})" if owner else ""
            output.append(f"{number:>{width}}{label} | {text}")
            output.extend(f"{'':>{width}}   {annotation}" for annotation in sorted(annotations[name, number]))
    return "\n".join(output)
