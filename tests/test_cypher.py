"""Retained native Cypher engine, semantic witnesses and internal query entry."""

import json
import subprocess
import sys

import pytest

from lsp_cli import __version__, cypher
from lsp_cli.analysis import Analysis
from lsp_cli.code_graph import CodeGraph
from lsp_cli.model import Source

from .test_cli import SPAN
from .test_git_sources import git
from .test_git_sources import repository as repository

CHAIN = "def leaf(): pass\ndef mid(): leaf()\ndef start(): mid()\n"
ENGINE = [sys.executable, "-c", "from lsp_cli.query_cli import main; raise SystemExit(main())"]


def invoke(*args, source=CHAIN, cwd=None):
    result = subprocess.run([*ENGINE, *args], input=source.encode("utf-8"), cwd=cwd,
                            capture_output=True, check=False, timeout=60)
    result.stdout = result.stdout.decode("utf-8").replace("\r\n", "\n")
    result.stderr = result.stderr.decode("utf-8").replace("\r\n", "\n")
    return result


def query(text, *args, source=CHAIN, cwd=None):
    result = invoke(*args, text, "--json", source=source, cwd=cwd)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not result.stderr
    return json.loads(result.stdout)


def values(body):
    return [row["values"] for row in body["results"]]


@pytest.mark.parametrize("name", ["show", "trace", "path", "refs", "callee", "callers", "diff", "history", "blame", "query"])
def test_internal_query_entry_treats_commands_as_ordinary_symbol_names(name):
    body = query('MATCH (s:Symbol {root:true}) RETURN s.name', name, "--module", "sample",
                 source=f"def {name}(): pass\n")
    assert values(body) == [[f"sample.{name}"]]


def test_internal_query_help_schema_and_version_need_no_source():
    help_result = invoke("--help", source="not Python")
    assert "[REV[:NAME] ...] 'CYPHER'" in help_result.stdout
    assert "{query}" not in help_result.stdout and "lsp-cli query" not in help_result.stdout
    assert "--event" not in help_result.stdout
    body = query("--schema", source="not Python")
    assert body["engine"]["name"] == "Ladybug"
    assert set(body["nodes"]) == {"Symbol", "Event", "Binding"}
    assert "CALLS" in body["relationships"] and "source_code" in body["nodes"]["Symbol"]
    assert invoke("--version").stdout.strip() == f"lsp-cli {__version__}"


def test_missing_query_and_schema_arguments_fail_before_reading_source():
    for args, message in ((["--json"], "Cypher text is required"),
                          (["--schema", "unwanted", "--json"], "--schema does not accept")):
        result = invoke(*args, source="not Python")
        body = json.loads(result.stdout)
        assert result.returncode == 2 and message in body["error"]["message"]
        assert body["command"] == "query" and body["sources"] == []


def test_native_path_order_parallel_calls_and_stable_ids():
    text = 'MATCH p=(s:Symbol {name:"__stdin__.start"})-[:CALLS*2..2]->(t) RETURN p ORDER BY t.name'
    body = query(text)
    assert body == query(text)
    path = values(body)[0][0]
    assert [n["name"] for n in path["nodes"]] == ["__stdin__.start", "__stdin__.mid", "__stdin__.leaf"]
    assert [e["line"] for e in path["relationships"]] == [3, 2]
    assert len(body["results"][0]["evidence"]) == 5
    assert all(SPAN <= row.keys() for row in path["nodes"] + path["relationships"])
    repeated = query('MATCH (s:Symbol)-[c:CALLS]->(t) RETURN c ORDER BY c.line',
                     source="def leaf(): pass\ndef run():\n leaf()\n leaf()\n")
    assert [row[0]["line"] for row in values(repeated)] == [3, 4]


def test_contexts_do_not_create_false_long_range_paths():
    source = ("def sink(): pass\ndef other(): pass\ndef relay(fn): fn()\n"
              "def start(): relay(other)\ndef elsewhere(): relay(sink)\n")
    body = query('MATCH p=(s:Symbol)-[:CALLS*2..2]->(t:Symbol {name:"__stdin__.sink"}) RETURN p', source=source)
    assert [row[0]["nodes"][0]["name"] for row in values(body)] == ["__stdin__.elsewhere"]
    contexts = query('MATCH (s:Symbol {name:"__stdin__.relay"}) RETURN count(s)', source=source)
    assert values(contexts) == [[3]]


@pytest.mark.parametrize(("text", "expected"), [
    ('MATCH (a:Symbol {name:"__stdin__.start"})-[:CALLS]->(b) WITH b '
     'MATCH (b)-[:CALLS]->(c) RETURN c.name', [["__stdin__.leaf"]]),
    pytest.param(
        'MATCH (a:Symbol {name:"__stdin__.leaf"}) OPTIONAL MATCH (a)-[:CALLS]->(b) RETURN b.name', [[None]],
        marks=pytest.mark.xfail(
            sys.platform == "win32", strict=True, raises=subprocess.TimeoutExpired,
            reason="Ladybug 0.21.0 Windows native preparation hangs for this internal OPTIONAL MATCH query",
        ),
    ),
    ('MATCH (a:Symbol)-[c:CALLS]->(b) RETURN count(c), collect(b.name) ORDER BY count(c)',
     [[2, ["__stdin__.leaf", "__stdin__.mid"]]]),
    ('UNWIND [1,2,2] AS n WITH DISTINCT n RETURN n * 2 ORDER BY n', [[2], [4]]),
    ('MATCH (s:Symbol) WHERE EXISTS { MATCH (s)-[:CALLS]->(:Symbol) } RETURN s.name ORDER BY s.name',
     [["__stdin__.mid"], ["__stdin__.start"]]),
    ('RETURN 1 AS n UNION RETURN 2 AS n', [[1], [2]]),
    ('RETURN "a; b" AS text, $item AS item', [["a; b", {"key": [1, 2]}]]),
    ('RETURN CAST(12 AS STRING), "line=" + CAST(12 AS STRING)', [["12", "line=12"]]),
])
def test_native_composition_without_a_custom_parser(text, expected):
    body = query(text, "--param", 'item={"key":[1,2]}')
    actual = values(body)
    # Cypher collect() without ORDER BY does not specify element order.
    if "collect(" in text:
        actual[0][1].sort()
    if " UNION " in text:
        actual.sort()  # UNION has no row-order guarantee without a final sort.
    assert actual == expected


def test_native_shortest_path_and_trail_semantics():
    source = "def leaf(): pass\ndef mid(): leaf()\ndef start():\n mid()\n leaf()\n"
    body = query('MATCH p=(s:Symbol {name:"__stdin__.start"})-[:CALLS* SHORTEST 1..8]->'
                 '(t:Symbol {name:"__stdin__.leaf"}) RETURN p', source=source)
    assert len(values(body)[0][0]["relationships"]) == 1
    recursive = "def loop(): loop()\n"
    body = query('MATCH p=(s:Symbol)-[:CALLS*2..2]->(s) RETURN length(p), is_trail(p)', source=recursive)
    assert values(body) == [[2, False]]


def test_source_projection_preserves_decorators_unicode_and_original_line_numbers():
    source = "def deco(f): return f\n@deco\ndef café():\n    return 'é'\n"
    body = query('MATCH (s:Symbol {name:"__stdin__.café"}) RETURN s.source_code', source=source)
    record = values(body)[0][0]
    assert record["code"] == "@deco\ndef café():\n    return 'é'"
    assert (record["line"], record["column"], record["end_line"], record["end_column"]) == (2, 0, 4, 14)
    assert body["results"][0]["evidence"][0]["line"] == 2
    text = invoke('MATCH (s:Symbol {name:"__stdin__.café"}) RETURN s.source_code', source=source).stdout
    assert "2 | @deco" in text and "4 |     return 'é'" in text


def test_ordered_events_writes_control_context_and_unknown_calls():
    source = ("def f(conn, flag):\n if flag:\n  conn.cert_reqs = 0\n else:\n  conn.cert_reqs = 1\n"
              " try:\n  conn.urlopen()\n except Exception:\n  raise\n return conn\n")
    body = query('MATCH (s:Symbol {name:"__stdin__.f"})-[:CONTAINS]->(e:Event) '
                 'WHERE e.kind <> "REF" RETURN e ORDER BY e.seq', source=source)
    events = [row[0] for row in values(body)]
    assert [r["kind"] for r in events] == ["ATTR_WRITE", "ATTR_WRITE", "CALL", "RAISE", "RETURN"]
    assert events[0]["attribute"] == "cert_reqs" and events[0]["context"][0]["kind"] == "if"
    assert events[1]["context"][0]["kind"] == "else"
    assert events[2]["resolution"] == "unresolved"
    assert body["unresolved_frontier"][0]["callee"] == "conn.urlopen"
    writes = query('MATCH (s:Symbol)-[w:WRITES]->(b:Binding) WHERE w.expression = "conn.cert_reqs" '
                   'RETURN w.value_binding.literal, w, b.name ORDER BY w.line', source=source)
    assert [r[0] for r in values(writes)] == ["0", "1"]
    assert all(SPAN <= r[1].keys() for r in values(writes))


def test_native_inheritance_mro_super_cls_and_self_dispatch():
    source = ("def sink(): pass\nclass A:\n def ping(self): sink()\n"
              " @classmethod\n def build(cls): return cls()\nclass B(A):\n"
              " def run(self):\n  self.ping()\n  super().ping()\n  self.build()\n")
    body = query('MATCH (b:Symbol {name:"__stdin__.B"})-[m:MRO]->(a) RETURN a.name,m.position,b.mro', source=source)
    assert values(body) == [["__stdin__.A", 1, ["__stdin__.B", "__stdin__.A", "builtins.object"]]]
    body = query('MATCH (s:Symbol {name:"__stdin__.B.run"})-[c:CALLS]->(t) '
                 'RETURN c.callee,t.name,t.receiver ORDER BY c.line', source=source)
    assert values(body) == [["self.ping", "__stdin__.A.ping", "__stdin__.B"],
                            ["super().ping", "__stdin__.A.ping", "__stdin__.B"],
                            ["self.build", "__stdin__.A.build", "__stdin__.B"]]


@pytest.mark.parametrize("rev", ["v1", "v2"])
def test_git_scope_import_aliases_immutable_revision_and_forward_paths(repository, rev):
    before = git(repository, "status", "--porcelain")
    body = query('MATCH p=(s:Symbol {root:true})-[:CALLS* SHORTEST 1..8]->'
                 '(t:Symbol {name:"pkg.base.Base.ping"}) RETURN p', f"{rev}:pkg.client.start", cwd=repository)
    assert [n["name"] for n in values(body)[0][0]["nodes"]] == [
        "pkg.client.start", "pkg.worker.Worker.run", "pkg.base.Base.ping",
    ]
    assert body["coverage"]["scope"] == "forward_from_symbols"
    assert "unrelated" not in [s["module"] for s in body["sources"]]
    assert all(s["git_commit"] == git(repository, "rev-parse", rev) for s in body["sources"])
    assert git(repository, "status", "--porcelain") == before


def test_module_and_whole_revision_scopes_for_reverse_queries(repository):
    text = 'MATCH (s:Symbol)-[c:CALLS]->(t:Symbol {name:"pkg.worker.Worker.run"}) RETURN c ORDER BY c.line'
    assert not values(query(text, "v1:Worker.run", cwd=repository))
    body = query(text, "v1", cwd=repository)
    assert values(body)[0][0]["caller"] == "pkg.client.start"
    assert body["coverage"]["scope"] == "all_input_symbols"
    module = query('MATCH (s:Symbol {root:true}) RETURN s.source_code', "v1:pkg.worker", cwd=repository)
    assert values(module)[0][0]["code"] == git(repository, "show", "v1:pkg/worker.py")


def test_enclosing_and_definitions_are_ordinary_predicates(repository):
    body = query('MATCH (s:Symbol) WHERE s.source = "v1:pkg/worker.py" '
                 'AND s.line <= 4 AND s.end_line >= 4 RETURN s.name ORDER BY s.end_line-s.line LIMIT 1',
                 "v1:pkg.worker", cwd=repository)
    assert values(body) == [["pkg.worker.Worker.run"]]
    body = query('MATCH (s:Symbol {root:true})-[:DEFINES]->(d) RETURN d.name ORDER BY d.name',
                 "v1:Worker", cwd=repository)
    assert values(body) == [["pkg.worker.Worker.run"]]


def test_file_inputs_and_scope_reuse_preserve_bindings(tmp_path):
    (tmp_path / "a.py").write_text("from b import leaf as fn\ndef start(): fn()\n")
    (tmp_path / "b.py").write_text("def leaf(): pass\n")
    body = query('MATCH (s:Symbol {root:true})-[c:CALLS]->(t) RETURN t.name,c', "start",
                 "-f", "a.py", "-f", "b.py", source="not Python", cwd=tmp_path)
    assert values(body)[0][0] == "b.leaf"
    assert values(body)[0][1]["source"] == "a.py"


@pytest.mark.parametrize(("text", "message"), [
    ("MATCH (n:Symbol) RETURN n.typo", "typo"),
    ("MATCH ()-[:CALSS]->() RETURN 1", "CALSS"),
    ("MATCH ( RETURN 1", "Parser"),
    ("RETURN 1; RETURN 2", "multiple statements"),
    ('CREATE (:Symbol {id:"invented"})', "read-only"),
    ('MATCH (s:Symbol) SET s.name="invented" RETURN s', "read-only"),
    ('MATCH (s:Symbol) DETACH DELETE s', "read-only"),
    ("RETURN $missing", "missing"),
    ("RETURN toString(12)", "TOSTRING"),
])
def test_native_errors_are_explicit_with_input_provenance(text, message):
    result = invoke(text, "--json")
    assert result.returncode == 2
    body = json.loads(result.stdout)
    assert body["error"]["kind"] == "CypherError" and message in body["error"]["message"]
    assert body["sources"][0]["source"] == "<stdin>" and body["query"] == text
    assert "Traceback" not in result.stdout + result.stderr


def test_limits_empty_results_and_scalar_provenance_are_honest():
    limited = query('MATCH (s:Symbol) RETURN s ORDER BY s.name', "--max-work", "1", "--max-rows", "1")
    assert limited["status"] == "incomplete" and not limited["coverage"]["complete"]
    assert {d["kind"] for d in limited["diagnostics"]} >= {"query_limit", "result_limit"}
    scalar = query("RETURN 1 AS scalar")
    assert values(scalar) == [[1]] and scalar["results"][0]["evidence"] == []
    assert scalar["sources"][0]["sha256"]
    empty = query('MATCH (s:Symbol {name:"missing"}) RETURN s')
    assert values(empty) == []


def test_query_leaves_no_database_or_index_artifacts(tmp_path):
    before = list(tmp_path.iterdir())
    assert query('MATCH (s:Symbol) RETURN count(s)', cwd=tmp_path)["results"]
    assert list(tmp_path.iterdir()) == before


def test_two_native_invocations_do_not_share_graph_state():
    graph = CodeGraph(Analysis([Source("one.py", "one", "def first(): pass\n")]))
    graph.materialize()
    assert cypher.run(graph, "MATCH (s:Symbol) RETURN count(s)")["results"][0]["values"] == [2]
    other = CodeGraph(Analysis([Source("two.py", "two", "")]))
    other.materialize()
    assert cypher.run(other, 'MATCH (s:Symbol {name:"one.first"}) RETURN s')["results"] == []


def test_arbitrary_cypher_maps_cannot_fabricate_source_evidence():
    forged = {"kind": "SOURCE", "source": "invented.py", "symbol": "invented.run", "symbol_kind": "function",
              "line": 1, "column": 0, "end_line": 1, "end_column": 3, "code": "bad"}
    body = query("RETURN $row", "--param", "row=" + json.dumps(forged))
    assert values(body) == [[forged]]
    assert body["results"][0]["evidence"] == []
    body = query('MATCH (s:Symbol {name:"__stdin__.leaf"}) RETURN {nested:s.source_code}')
    assert body["results"][0]["evidence"][0]["kind"] == "SOURCE"


def test_unresolved_mro_diagnostic_is_not_overwritten_by_symbol_kind():
    body = query('MATCH (s:Symbol {kind:"class"}) RETURN s.mro', source="from absent import Base\nclass Child(Base): pass\n")
    assert any(d["kind"] == "unresolved_mro" and d["symbol"] == "__stdin__.Child" for d in body["diagnostics"])


def test_call_projection_fits_seventeen_rows_and_completion_marker_in_thirty_lines():
    source = ''.join(f'def target{i}(): pass\n' for i in range(17))
    source += 'def run():\n' + ''.join(f' target{i}()\n' for i in range(17))
    result = invoke('MATCH (s:Symbol {name:"__stdin__.run"})-[c:CALLS]->(t) '
                    'RETURN s.name,t.name ORDER BY c.line', source=source)
    assert result.returncode == 0
    first_thirty = result.stdout.splitlines()[:30]
    assert 'row\ts.name\tt.name' in first_thirty
    assert 'END RESULTS 17 rows (complete)' in first_thirty
    rows = [line for line in first_thirty if line.startswith('[')]
    assert len(rows) == 17
    assert all(json.loads(line.split('\t')[2]) == f'__stdin__.target{i}' for i, line in enumerate(rows))
    assert rows[-1].startswith('[17/17]\t')


def test_event_text_exposes_unresolved_binding_and_compact_control_span():
    source = 'def f(obj, flag):\n if flag:\n  obj.send()\n'
    result = invoke('MATCH (s:Symbol {kind:"function"})-[:CONTAINS]->(e:Event {kind:"CALL"}) '
                    'RETURN e,e.context', source=source)
    assert result.returncode == 0
    assert '[unresolved:' in result.stdout and 'obj.send()' in result.stdout
    assert 'if("flag") @ <stdin>:2:1-3:12' in result.stdout
    assert 'END RESULTS 1 rows (complete)' in result.stdout


def test_query_error_is_not_described_as_no_matches_and_limits_remain_visible():
    failed = invoke('MATCH (s:Symbol) RETURN s.no_such_property')
    assert failed.returncode == 2 and 'query error (CypherError)' in failed.stdout
    assert 'no matches' not in failed.stdout and 'END RESULTS 0 rows (error)' in failed.stdout
    limited = invoke('UNWIND [1,2,3] AS n RETURN n', '--max-rows', '1')
    assert limited.returncode == 0 and 'END RESULTS 1 rows (truncated)' in limited.stdout
