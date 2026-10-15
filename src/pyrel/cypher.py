"""Native Ladybug Cypher over an invocation-local, context-sensitive code graph.

There is deliberately no Cypher parser or query rewriter here. Ladybug owns
syntax, planning, path traversal and evaluation. We supply facts and preserve
their source evidence when native graph values are returned.
"""

from __future__ import annotations

import datetime
import decimal
import json
import warnings
from collections import defaultdict

from .code_graph import SourceCode, SourceDiff, Walk, evidence, identity, serialize
from .git_queries import unified
from .model import source_lines
from .source_view import show

ENGINE = {"name": "Ladybug", "version": "0.21.0", "dialect": "Ladybug Cypher",
          "documentation": "https://docs.ladybugdb.com/cypher/"}
SPAN_FIELDS = {"source": "STRING", "line": "INT64", "column": "INT64",
               "end_line": "INT64", "end_column": "INT64"}
CONTEXT_FIELDS = {"kind": "STRING", "label": "STRING", **SPAN_FIELDS}
SOURCE_FIELDS = {"kind": "STRING", "symbol": "STRING", "symbol_kind": "STRING", **SPAN_FIELDS, "code": "STRING",
                 "revision": "STRING", "git_commit": "STRING", "git_blob": "STRING", "git_path": "STRING"}
VALUE_FIELDS = {name: "STRING" for name in (
    "resolution", "target", "reason", "import_target", "receiver", "dispatch", "inferred_type", "literal", "value_kind",
)} | {"candidates": "STRING[]"}


def struct(fields):
    return "STRUCT(" + ", ".join(f"`{key}` {kind}" for key, kind in fields.items()) + ")"


DIFF_FIELDS = {"id": "STRING", "kind": "STRING", "status": "STRING", **SPAN_FIELDS,
               "before": struct(SOURCE_FIELDS), "after": struct(SOURCE_FIELDS),
               "patch": "STRING", "reason": "STRING"}
COMMON = {"id": "STRING", **SPAN_FIELDS, "revision": "STRING"}
OCCURRENCE = {
    "kind": "STRING", "scope": "STRING", "text": "STRING", "seq": "INT64",
    "context": struct(CONTEXT_FIELDS) + "[]", "unreachable": "BOOL", "resolution": "STRING",
    "target": "STRING", "callee": "STRING", "receiver": "STRING", "caller": "STRING",
    "reason": "STRING", "binding": "STRING", "expression": "STRING", "role": "STRING",
    "attribute": "STRING", "value": "STRING", "operation": "STRING",
    "value_binding": struct(VALUE_FIELDS), "target_span": struct(SPAN_FIELDS),
    "statement_span": struct(SPAN_FIELDS),
}
NODES = {
    "Symbol": COMMON | {"name": "STRING", "kind": "STRING", "parent": "STRING", "receiver": "STRING",
                        "root": "BOOL", "expanded": "BOOL", "code": "STRING", "mro": "STRING[]",
                        "source_code": struct(SOURCE_FIELDS),
                        "module": "STRING", "sha256": "STRING", "git_commit": "STRING",
                        "git_blob": "STRING", "git_path": "STRING"},
    "Event": COMMON | OCCURRENCE,
    "Binding": COMMON | {"name": "STRING", "scope": "STRING"},
}
REL_FIELDS = COMMON | OCCURRENCE | {"position": "INT64"}
RELATIONS = {
    "CALLS": [("Symbol", "Symbol")],
    "CONTAINS": [("Symbol", "Event")],
    "DEFINES": [("Symbol", "Symbol")],
    "INHERITS": [("Symbol", "Symbol")],
    "MRO": [("Symbol", "Symbol")],
    "READS": [("Symbol", "Binding")],
    "WRITES": [("Symbol", "Binding")],
    "REFERS_TO": [("Event", "Symbol"), ("Event", "Binding")],
}


def schema():
    return {
        "schema_version": 1, "command": "query", "engine": ENGINE,
        "nodes": NODES,
        "relationships": {name: {"endpoints": pairs, "properties": REL_FIELDS}
                          for name, pairs in RELATIONS.items()},
        "functions": {"diff(before.source_code, after.source_code)": DIFF_FIELDS},
        "response": {
            "schema_version": "Integer response-format version, currently 1.",
            "command": "query.",
            "engine": "Native engine name, version, dialect and documentation URL.",
            "query": "Original Cypher text.",
            "parameters": "Decoded --param NAME=JSON values.",
            "columns": "Array of {name, type}, in RETURN order; names include explicit AS aliases.",
            "results": "Array of {values, evidence}; values[i] corresponds to columns[i]. An empty array can mean zero matches or a query error; inspect error and inputs first.",
            "error": "Optional {kind, message, help} for invocation/query failure; absent on a successful query. Argument errors return a reduced response with error before any input/graph analysis.",
            "inputs": "Per-address resolution: address, revision, selector, status, symbols, candidates; failures retain message and available binding evidence. Successful inputs remain queryable when another input fails.",
            "status": "Combined input/graph state: ok, incomplete, partial or error. incomplete can accompany complete source bodies when static calls remain unresolved.",
            "output": "Returned rows: status (complete, truncated or error), rows, source_bodies and diffs. source_bodies inventories explicitly projected source_code values with spans, complete, lines and bytes.",
            "coverage": "Static graph extent: scope, complete, roots, root_symbols, nodes, relationships, expanded_contexts, max_work, unresolved_calls and per-version coverage.",
            "diagnostics": "Analysis/limit diagnostics, including unresolved_mro; native query failures are in error.kind/message. An unresolved MRO limits inherited dispatch, not the availability of an explicitly located source body.",
            "unresolved_frontier": "Unresolved static call sites with reasons and original source spans.",
            "sources": "Input source identities and hashes. Git queries also include versions and, for one revision, git metadata with peeled commits and blob identities.",
            "truncated": "True when native query output exceeds --max-rows; false when fully returned. Present after native query success.",
            "buffer_pool_bytes": "Native engine buffer budget for this invocation; present after native query success.",
        },
        "notes": [
            "Usage: pysym REV[:SYMBOL] 'CYPHER'; pipe Python source to pysym 'CYPHER'.",
            "Multiple REV:NAME inputs may span revisions; OLD..NEW:NAME expands to two inputs. Each revision is analyzed separately.",
            "Symbol.revision identifies the input revision. CALLS never links different revisions.",
            "Names include modules, classes, functions, assignment/import declarations and exact repository-relative Python file paths.",
            "Symbol.kind=binding/import describes a declaration site, not an inferred runtime class or callable.",
            "Declaration nodes keep expanded=false; their enclosing scope supplies execution events and binding references.",
            "A function scope follows resolved calls. A class/module also seeds its lexical descendants.",
            "A bare REV or file/stdin without a scope seeds every symbol in the selected input.",
            "Reverse matches cover this declared scope only; use a module or REV for callers outside a function.",
            "Symbol.name is fully qualified. Different argument/receiver/closure/attribute contexts have different ids.",
            "Symbol.root marks selected names, or input modules for a bare revision/file/stdin. expanded=false marks an unexpanded context.",
            "CALLS edges are resolved static call occurrences. Events retain unresolved calls and control context.",
            "Event.kind: CALL, ASSIGN, ATTR_WRITE, SUBSCRIPT_WRITE, RETURN, RAISE, REF. REF.role: read, write, import, definition.",
            "Event.text is original source; callee/target describe the call expression/resolved name. resolution is resolved or unresolved; reason explains uncertainty.",
            "Event.value_binding describes the assigned value; attribute names an ATTR_WRITE field; operation distinguishes assign/delete writes.",
            "Event.context contains control nesting (kind/label/span). target_span and statement_span identify a write's target and enclosing statement.",
            "CONTAINS joins a symbol context to source events/refs; seq is local to that context.",
            "RETURN s.source_code reads complete original code with spans and line numbers; s.code returns only the raw string.",
            "Default text renders source_code as labeled, numbered source blocks, and renders paths/diffs directly. Use --json for downstream data processing; source_code.code is a raw string in either format.",
            "RETURN diff(a.source_code,b.source_code) compares original bodies with original-file hunk coordinates and both source spans.",
            "diff with a NULL side returns unavailable; a missing query match is not proof of addition/deletion. Check inputs[].status.",
            "Per-input missing/ambiguous/error states retain successful results; candidate addresses are source-verified. Exit status is nonzero for failed inputs.",
            "Alias.method uses actual binding/member resolution; inputs[].binding retains steps, source history, control context and unresolved alternatives.",
            "Binding history includes prior writes, not an assertion that every recorded value remains active. Occurrence cutoffs preserve alias snapshots.",
            "output.status describes returned rows; output.source_bodies inventories projected complete bodies. coverage.complete describes graph coverage separately.",
            "Text source bodies end with END SOURCE. External shell truncation is not visible to the query producer.",
            "Text single-line values use tab-separated rows with a row/total counter. END RESULTS marks the end of returned rows; missing end markers can indicate downstream truncation.",
            "Symbol entity values omit source_code/code to keep paths compact; project either property explicitly to read source.",
            "Binding nodes represent scoped binding expressions, not runtime object identities across functions.",
            "MRO.position starts at 1 for bases; Symbol.mro includes the class itself and external builtins.object.",
            "All entities/relationships retain original source spans. Return entities or paths for automatic evidence.",
            "Lines are 1-based; columns are 0-based Unicode character offsets; end positions are exclusive. Quote the reserved column property with backticks.",
            "source is the input label (REV:path for Git). git_commit/git_blob/git_path and sha256 pin original source identity; revision is the requested ref.",
            "Symbol.kind includes module, class, function, method, classmethod, staticmethod, binding, import. parent/module identify lexical containment; receiver identifies dispatch context.",
            "Binding.name/scope identify a scoped expression, not a runtime object. Relationship.position applies to MRO base order.",
            "Scalar-only projections retain input provenance, not invented row-level source spans.",
            "Native Ladybug dialect: shortest paths use [:CALLS* SHORTEST 1..8]; paths default to walks.",
            "Native numeric-to-string conversion is CAST(value AS STRING). Return name, revision and line as separate columns for automatic text labels.",
            "For an empty/failed display, inspect the same response's error.kind/message, then inputs[].status. Preserve upstream failure with shell pipefail and surface error before processing results[].values.",
            "Use is_trail(p) or is_acyclic(p) to constrain repeated edges/nodes; bound long paths explicitly.",
            "One read-only statement per invocation. --param NAME=JSON supplies native query parameters.",
            "LIMIT controls rows returned by Cypher, not analysis scope. Choose a function scope to reduce graph construction.",
        ],
    }


def properties(value, fields):
    result = {key: value.get(key) for key in fields}
    if fields is VALUE_FIELDS:
        # An absent candidate set is empty. Supply its type even in batches
        # where every value is unknown; native nested-null inference otherwise
        # treats this list field as a string before the STRUCT cast.
        result["candidates"] = value.get("candidates") or []
    for key, nested in (("context", CONTEXT_FIELDS), ("value_binding", VALUE_FIELDS),
                        ("target_span", SPAN_FIELDS), ("statement_span", SPAN_FIELDS), ("source_code", SOURCE_FIELDS)):
        if key in result and result[key] is not None:
            result[key] = ([properties(item, nested) for item in result[key]] if key == "context"
                           else properties(result[key], nested))
    return result


def batches(rows, size=256):
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


def load_graph(connection, graph):
    """Parameterize facts; no intermediate CSV, pandas dependency or persistent DB."""
    groups = defaultdict(list)
    source_views = {}
    for node in graph.nodes.values():
        props = {"id": node.id, **node.props}
        if node.label == "Symbol":
            view = {**show(node.symbol, node.props["receiver"]),
                    **{key: props.get(key) for key in ("revision", "git_commit", "git_blob", "git_path")}}
            props["source_code"] = properties(view, SOURCE_FIELDS)
            props["code"] = props["source_code"]["code"]
            view = props["source_code"]
            source_views[source_key(view)] = SourceCode(
                identity(node.symbol.source.identity(), node.symbol.name,
                         tuple(view[field] for field in SPAN_FIELDS), "source_code"), view,
            )
        groups[node.label].append(properties(props, NODES[node.label]))
    for label, fields in NODES.items():
        ddl = ", ".join(f"`{key}` {kind}" for key, kind in fields.items())
        connection.execute(f"CREATE NODE TABLE {label}({ddl}, PRIMARY KEY(id))").close()
        assignments = ", ".join(f"`{key}`: CAST(r.`{key}` AS {kind})" for key, kind in fields.items())
        for rows in batches(groups[label]):
            connection.execute(f"UNWIND $rows AS r CREATE (:{label} {{{assignments}}})", {"rows": rows}).close()
    edges = defaultdict(list)
    for edge in graph.edges.values():
        edges[edge.type, edge.start.label, edge.end.label].append(
            properties({"id": edge.id, **edge.props}, REL_FIELDS) | {"start": edge.start.id, "end": edge.end.id},
        )
    for kind, endpoints in RELATIONS.items():
        pairs = ", ".join(f"FROM {start} TO {end}" for start, end in endpoints)
        ddl = ", ".join(f"`{key}` {typ}" for key, typ in REL_FIELDS.items())
        connection.execute(f"CREATE REL TABLE GROUP {kind}({pairs}, {ddl})").close()
        assignments = ", ".join(f"`{key}`: CAST(r.`{key}` AS {typ})" for key, typ in REL_FIELDS.items())
        for start, end in endpoints:
            for rows in batches(edges[kind, start, end]):
                connection.execute(
                    f"UNWIND $rows AS r MATCH (a:{start} {{id:r.`start`}}), (b:{end} {{id:r.`end`}}) "
                    f"CREATE (a)-[:{kind} {{{assignments}}}]->(b)", {"rows": rows},
                ).close()
    return source_views


def source_key(value):
    if (set(value) == set(SOURCE_FIELDS) and value.get("kind") == "SOURCE"
            and all(item is None or isinstance(item, (str, int)) for item in value.values())):
        return tuple(value[key] for key in SOURCE_FIELDS)
    return None


def hydrate(value, graph, source_views, diff_views=None):
    """Replace engine-internal offsets with stable source-backed graph identities."""
    if isinstance(value, dict):
        difference = (diff_views or {}).get(value.get("id"))
        if difference is not None and value == difference.props:
            return difference
        # User-created Cypher maps must not acquire invented source evidence.
        # Only a complete projection matching an original source is trusted.
        view = source_views.get(source_key(value))
        if view is not None:
            return view
        if "_NODES" in value and "_RELS" in value:
            return Walk([hydrate(n, graph, source_views, diff_views) for n in value["_NODES"]],
                        [hydrate(e, graph, source_views, diff_views) for e in value["_RELS"]])
        if "_ID" in value and "_LABEL" in value:
            records = graph.edges if "_SRC" in value else graph.nodes
            if value.get("id") in records:
                return records[value["id"]]
        return {key: hydrate(item, graph, source_views, diff_views) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [hydrate(item, graph, source_views, diff_views) for item in value]
    if isinstance(value, (datetime.date, datetime.time, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return {"days": value.days, "seconds": value.seconds, "microseconds": value.microseconds}
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    return value


class CypherError(ValueError):
    """An engine diagnostic that the CLI can report without a Python traceback."""


def install_diff(connection, source_views):
    """A native scalar UDF backed by the existing exact-span diff implementation."""
    differences = {}

    def compare(before, after):
        views = []
        for value in (before, after):
            view = source_views.get(source_key(value)) if value is not None else None
            if value is not None and view is None:
                raise ValueError("diff requires original source_code values from this query's inputs")
            views.append(view)
        old, new = views
        anchor = new or old
        status = "unavailable" if old is None or new is None else (
            "unchanged" if old.props["code"] == new.props["code"] else "modified")
        key = identity("diff", old.id if old else None, new.id if new else None)
        props = {"id": key, "kind": "DIFF", "status": status,
                 **{field: anchor.props[field] if anchor else None for field in SPAN_FIELDS},
                 "before": old.props if old else None, "after": new.props if new else None,
                 "patch": unified(old.props, new.props, 3) if old and new else "",
                 "reason": "missing source_code; inspect per-input status and candidates" if status == "unavailable" else ""}
        differences[key] = SourceDiff(key, props, old, new)
        return props

    # The Python UDF type parser retains quoted field names literally. These
    # fixed schema identifiers are safe unquoted, including nested structs.
    source_type = struct(SOURCE_FIELDS).replace("`", "")
    connection.create_function("diff", compare, [source_type, source_type],
                               struct(DIFF_FIELDS).replace("`", ""), default_null_handling=False)
    return differences


def run(graph, text, parameters=None, *, timeout_ms=30000, max_rows=1000):
    import ladybug

    if timeout_ms < 1 or max_rows < 1:
        raise ValueError("--timeout-ms and --max-rows must be positive")
    # Bound memory independently of host RAM. Wide property tables need more
    # than 64 MiB for real test scopes even when the final projection is small.
    # Scale with the graph actually supplied, retaining a strict 512 MiB cap.
    block = 32 * 1024 * 1024
    required = block + (len(graph.nodes) + len(graph.edges)) * 4096
    buffer_pool_size = min(512 * 1024 * 1024, max(64 * 1024 * 1024, ((required + block - 1) // block) * block))
    try:
        with ladybug.Database(buffer_pool_size=buffer_pool_size, max_num_threads=2) as database:
            with ladybug.Connection(database) as connection:
                source_views = load_graph(connection, graph)
                diff_views = install_diff(connection, source_views)
                connection.set_query_timeout(timeout_ms)
                connection.execute("BEGIN TRANSACTION READ ONLY").close()
                # Native prepare rejects multiple statements. Suppress only its
                # deprecation notice; parsing, binding and execution stay native.
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", message="The use of separate prepare \\+ execute",
                                            category=DeprecationWarning)
                    prepared = connection.prepare(text, parameters or {})
                if not prepared.is_success():
                    raise CypherError(prepared.get_error_message())
                result = connection.execute(prepared, parameters or {})
                try:
                    columns = [{"name": name, "type": kind} for name, kind in zip(
                        result.get_column_names(), result.get_column_data_types(), strict=True,
                    )]
                    rows = []
                    while result.has_next() and len(rows) < max_rows:
                        values = [hydrate(item, graph, source_views, diff_views) for item in result.get_next()]
                        rows.append({"values": [serialize(item) for item in values], "evidence": evidence(values)})
                    return {"columns": columns, "results": rows, "truncated": result.has_next(),
                            "buffer_pool_bytes": buffer_pool_size}
                finally:
                    result.close()
    except RuntimeError as error:
        raise CypherError(str(error)) from error


def compact(result):
    if "nodes" in result:
        lines = [f"{ENGINE['name']} {ENGINE['version']} — native {ENGINE['dialect']}"]
        for label, fields in NODES.items():
            lines.append(f"({label}) " + ", ".join(f"{key}: {value}" for key, value in fields.items()))
        for kind, pairs in RELATIONS.items():
            lines.extend(f"({start})-[:{kind}]->({end})" for start, end in pairs)
        lines.append("Relationships: " + ", ".join(f"{key}: {value}" for key, value in REL_FIELDS.items()))
        lines.append("diff(before.source_code, after.source_code): " + struct(DIFF_FIELDS))
        lines.append("JSON response contract (--json):")
        lines.extend(f"  {name}: {description}" for name, description in result["response"].items())
        return "\n".join([*lines, *result["notes"], ENGINE["documentation"]])

    def location(value):
        return (f"{value['source']}:{value['line']}:{value['column']}-"
                f"{value['end_line']}:{value['end_column']}")

    shown_sources = set()

    def indent(value):
        return "\n".join("  " + line for line in source_lines(value))

    def render(value):
        if isinstance(value, dict) and value.get("kind") == "SOURCE" and "code" in value and "id" in value:
            header = f"{location(value)} {value['symbol_kind']} {value['symbol']}"
            if value["id"] in shown_sources:
                return header + " (source already displayed above)"
            shown_sources.add(value["id"])
            width = len(str(value["end_line"]))
            code_lines = source_lines(value["code"])
            return header + f" (complete body, {len(code_lines)} lines)\n" + "\n".join(
                f"{index:>{width}} | {line}" for index, line in enumerate(code_lines, value["line"])
            ) + f"\nEND SOURCE {value['symbol']} @ {location(value)}"
        if isinstance(value, dict) and value.get("kind") == "DIFF" and "id" in value:
            before, after = value["before"], value["after"]
            lines = [f"DIFF {value['status']}: " + (location(before) if before else "missing")
                     + " -> " + (location(after) if after else "missing")]
            if value["patch"]:
                lines.append(value["patch"])
            if value["reason"]:
                lines.append(value["reason"])
            return "\n".join(lines)
        if isinstance(value, dict) and "nodes" in value and "relationships" in value:
            nodes, edges = value["nodes"], value["relationships"]
            if not nodes:
                return "[]"
            lines = [render(nodes[0])]
            for edge, node in zip(edges, nodes[1:], strict=True):
                lines.append(f"  -[{edge['type']} {location(edge)} {' '.join(edge.get('text', '').split())}]-> "
                             + render(node))
            return "\n".join(lines)
        if isinstance(value, dict) and "id" in value and "source" in value:
            detail = value.get("name") or " ".join(value.get("text", value.get("expression", "")).split())
            contexts = " / ".join(c["kind"] + (f"({c['label']})" if c.get("label") else "")
                                  for c in value.get("context", []))
            resolution = ""
            if value.get("kind") == "CALL" or value.get("type") == "CALLS":
                if value.get("resolution") == "unresolved":
                    resolution = " [unresolved: " + " ".join(value.get("reason", "").split()) + "]"
                elif value.get("target"):
                    resolution = f" -> {value['target']}"
            return (f"{location(value)} {value.get('type', value.get('kind', 'Binding'))} {detail}{resolution}"
                    + (f" [{contexts}]" if contexts else ""))
        if isinstance(value, list) and value and all(
            isinstance(item, dict) and set(item) == set(CONTEXT_FIELDS) for item in value
        ):
            return " / ".join(f"{item['kind']}({json.dumps(item['label'], ensure_ascii=False)}) @ {location(item)}"
                              for item in value)
        if isinstance(value, list) and any(isinstance(item, (dict, list)) for item in value):
            return "\n" + "\n".join(f"  [{index}]\n{indent(indent(render(item)))}" for index, item in enumerate(value, 1))
        if isinstance(value, dict):
            return "\n" + "\n".join(f"  {key} = {indent(render(item)).lstrip()}" for key, item in value.items())
        return json.dumps(value, ensure_ascii=False)

    from .feedback import output

    coverage = result["coverage"]
    displayed = result.get("output") or output(result)
    inputs = result.get("inputs", [])
    resolved = sum(item["status"] == "resolved" for item in inputs)
    invocation = "error" if "error" in result or (inputs and not resolved) else "partial" if resolved < len(inputs) else "ok"
    lines = [f"query: {invocation} | results: {displayed['status']}, {len(result['results'])} rows | "
             f"inputs: {resolved}/{len(inputs)} resolved",
             f"returned source: {len(displayed['source_bodies'])} complete bodies; {len(displayed['diffs'])} diffs"]
    if "error" in result:
        lines.append(f"query error ({result['error']['kind']}): {result['error']['message']}")
        if result["error"].get("help"):
            lines.append(result["error"]["help"])
    if "git" in result:
        lines.append(f"git {result['git']['revision']} @ {result['git']['commit']}")
    else:
        lines.extend(f"git {rev} @ {commit}" for rev, commit in dict.fromkeys(
            (item["revision"], item["commit"]) for item in result.get("versions", [])))
    for item in inputs:
        lines.append(f"input[{item['input']}] {item['address']}: {item['status']}"
                     + (" — " + item["message"] if item.get("message") else ""))
        for symbol in item.get("symbols", []):
            lines.append(f"  {symbol['kind']} {symbol['name']} @ {location(symbol)}")
        binding = item.get("binding", {})
        if binding and (binding.get("steps") or binding.get("target") or binding.get("candidates")):
            target = binding.get("target") or f"unresolved ({binding.get('reason', '')})"
            lines.append(f"  binding: {binding['name']} -> {target}")
            if binding.get("mro"):
                lines.append("  MRO: " + " -> ".join(binding["mro"]))
            lines.append("  binding history (writes retain their source conditions):")
            for fact in binding.get("evidence", []):
                context = " / ".join(c["kind"] + (f"({c['label']})" if c.get("label") else "")
                                     for c in fact.get("context", []))
                lines.append(f"    {location(fact)} {fact['role']} {' '.join(fact['text'].split())}"
                             + (f" [{context}]" if context else ""))
            for alternative in binding.get("alternatives", []):
                lines.append(f"  possible receiver {alternative['binding']}: " +
                             (alternative.get("target") or "unresolved — " + alternative.get("reason", "")))
            if not binding.get("evidence_complete", True):
                lines.append("  binding history reached its evidence budget")
        lines.extend(f"  candidate: {c['address']} ({c['kind']}) {location(c)}" for c in item["candidates"][:20])
        if len(item["candidates"]) > 20:
            lines.append(f"  {len(item['candidates']) - 20} more candidates in --json")
    rendered = [[render(value) for value in row["values"]] for row in result["results"]]
    columns = [column["name"] for column in result["columns"]]
    if rendered and all("\n" not in cell and "\r" not in cell and "\t" not in cell
                        for row in [columns, *rendered] for cell in row):
        lines.append("row\t" + "\t".join(columns))
        lines.extend(f"[{index}/{len(rendered)}]\t" + "\t".join(row) for index, row in enumerate(rendered, 1))
    else:
        for index, row in enumerate(rendered, 1):
            lines.extend(f"[{index}] {column} = {value}" for column, value in zip(columns, row, strict=True))
    if not result["results"] and "error" not in result and (not inputs or resolved):
        lines.append("no matches within the declared scope; unresolved calls are not proof of absence")
    lines.append(f"END RESULTS {len(rendered)} rows ({displayed['status']})")
    lines.append(f"graph: {'complete' if coverage['complete'] else 'incomplete'} | {coverage['scope']} | "
                 f"{len(result['unresolved_frontier'])} unresolved call sites")
    diagnostics = defaultdict(list)
    for diagnostic in result["diagnostics"]:
        diagnostics[(diagnostic["kind"], diagnostic["message"])].append(diagnostic)
    for (kind, message), facts in diagnostics.items():
        lines.append(f"graph {kind}: {message}" + (f" ({len(facts)} occurrences)" if len(facts) > 1 else ""))
        sites = list({(fact.get("symbol", ""), location(fact)) for fact in facts if "source" in fact})
        lines.extend(f"  {name} @ {span}" for name, span in sorted(sites)[:3])
        if len(sites) > 3:
            lines.append(f"  {len(sites) - 3} more affected symbols in --json diagnostics")
    return "\n".join(lines)
