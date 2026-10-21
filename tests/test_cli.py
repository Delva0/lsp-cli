"""Verify the public symbol subcommands, source spans and failure feedback."""

import json
import os
import subprocess
import sys

import pytest

# Historical action contract remains directly replayable without exposing old
# aliases in the new public lsp command.
CLI = [sys.executable, "-c",
       "from lsp_cli.query_cli import subcommand_main; raise SystemExit(subcommand_main())"]
CODE = "def target(): pass\nclass Base: pass\ndef start():\n value = target()\n return value\n"
SPAN = {"source", "line", "column", "end_line", "end_column"}


def test_public_help_exposes_symbol_commands_without_cypher():
    result = invoke("--help", source="not Python")
    assert result.returncode == 0
    assert "show" in result.stdout and "trace" in result.stdout and "--event" in result.stdout
    assert "CYPHER" not in result.stdout and "--schema" not in result.stdout
    rejected = invoke("query", "RETURN 1", "--json", source="not Python")
    assert rejected.returncode == 2
    assert "invalid choice" in json.loads(rejected.stdout)["error"]["message"]


def invoke(*args, source=CODE, cwd=None):
    # Preserve fixture bytes, including deliberate CRLF; Windows text-mode
    # stdin would turn LF into CRLF and existing CRLF into CRCRLF.
    result = subprocess.run([*CLI, *args], input=source.encode("utf-8"),
                            capture_output=True, check=False, cwd=cwd)
    result.stdout = result.stdout.decode("utf-8").replace("\r\n", "\n")
    result.stderr = result.stderr.decode("utf-8").replace("\r\n", "\n")
    return result


@pytest.mark.parametrize(
    "args",
    [
        ["outline"],
        ["defs", "Base"],
        ["show", "start"],
        ["callee", "start"],
        ["callers", "target"],
        ["refs", "value"],
        ["trace", "start"],
        ["inherits", "Base"],
        ["path", "start", "target"],
        ["where", "--line", "4"],
    ],
)
def test_all_commands_stable_json_and_spans(args):
    first = invoke(*args, "--json")
    second = invoke("--json", *args)
    assert first.returncode == 0, first.stderr
    assert first.stdout == second.stdout
    assert first.stderr == ""
    payload = json.loads(first.stdout)
    assert payload["schema_version"] == 1
    assert payload["sources"][0]["source"] == "<stdin>"
    assert len(payload["sources"][0]["sha256"]) == 64
    assert payload["results"]
    assert all(row.keys() >= SPAN for row in payload["results"])


def test_exported_bare_files_get_identity_from_qualified_query(tmp_path):
    (tmp_path / "sessions.py").write_text(
        "from .adapters import HTTPAdapter\nclass Session:\n def send(self):\n  HTTPAdapter().send()\n",
    )
    (tmp_path / "adapters.py").write_text("class HTTPAdapter:\n def send(self): pass\n")
    result = invoke(
        "path",
        "requests.sessions.Session.send",
        "requests.adapters.HTTPAdapter.send",
        "--source",
        "sessions.py",
        "--source",
        "adapters.py",
        "--json",
        source="",
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "found"
    assert [s["module"] for s in payload["sources"]] == ["requests.sessions", "requests.adapters"]
    assert all(s["module_origin"] == "qualified_query" for s in payload["sources"])


def test_stdin_can_join_explicit_source_and_has_original_label(tmp_path):
    dep = tmp_path / "dep.py"
    dep.write_text("def target(): pass\n")
    result = invoke(
        "callee",
        "start",
        "--source",
        "pkg.dep=" + str(dep),
        "--source",
        "-",
        "--module",
        "pkg.app",
        "--filename",
        "git:v1:pkg/app.py",
        "--json",
        source="from .dep import target as run\ndef start(): run()\n",
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 0
    assert payload["results"][0]["target"] == "pkg.dep.target"
    assert payload["results"][0]["source"] == "git:v1:pkg/app.py"


@pytest.mark.parametrize(
    ("args", "source"),
    [
        (["trace", "missing"], CODE),
        (["callee"], CODE),
        (["where", "--line", "0"], CODE),
        (["outline"], "def broken(:\n"),
        (["callee", "f"], "class A:\n def f(self): pass\nclass B:\n def f(self): pass\n"),
        (["outline", "--source", "missing.py"], ""),
        (["trace", "start", "--unknown-option"], CODE),
    ],
)
def test_json_errors_have_nonzero_exit_and_no_traceback(args, source):
    result = invoke(*args, "--json", source=source)
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["message"]
    assert "Traceback" not in result.stdout + result.stderr


def test_non_utf8_stdin_cookie_and_unicode_spans():
    source = "# coding: latin-1\ndef café(): pass\ndef start(): café()\n".encode("latin-1")
    result = subprocess.run([*CLI, "callee", "start", "--json"], input=source, capture_output=True, check=False)
    payload = json.loads(result.stdout)
    assert payload["results"][0]["target"] == "__stdin__.café"
    assert payload["results"][0]["end_column"] == 19


def test_text_trace_exposes_arguments_and_ref_write_value():
    code = "def f(conn, verify):\n conn.cert_reqs = 'CERT_NONE'\n conn.urlopen(verify=verify)\n"
    result = invoke("trace", "f", source=code)
    assert "conn.urlopen(verify=verify)" in result.stdout
    refs = invoke("refs", "conn.cert_reqs", source=code)
    assert "conn.cert_reqs = 'CERT_NONE'" in refs.stdout


def test_no_index_database_or_daemon_artifacts(tmp_path):
    source = tmp_path / "input.py"
    source.write_text(CODE)
    before = sorted(os.listdir(tmp_path))
    result = invoke("outline", "--source", "input.py", cwd=tmp_path, source="")
    assert result.returncode == 0
    assert sorted(os.listdir(tmp_path)) == before


@pytest.mark.parametrize("flag", ["--file", "-f", "--source"])
def test_explicit_file_aliases_read_file_not_stdin(tmp_path, flag):
    (tmp_path / "input.py").write_text(CODE)
    result = invoke("trace", "start", flag, "input.py", "--json", source="not Python!", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    assert body["results"][0]["target"] == "input.target"
    assert body["results"][0]["source"] == "input.py"


@pytest.mark.parametrize("flag", ["--stdin-filename", "--filename"])
def test_stdin_label_does_not_read_same_named_file(tmp_path, flag):
    (tmp_path / "input.py").write_text("not Python!")
    result = invoke("trace", "start", flag, "input.py", "--json", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["results"][0]["target"] == "input.target"


def test_empty_stdin_explains_missing_input_without_rejecting_empty_files(tmp_path):
    missing = invoke("trace", "Thing.run", "--filename", "input.py", "--json", source="")
    assert missing.returncode == 2
    message = json.loads(missing.stdout)["error"]["message"]
    assert "stdin was empty" in message and "--file PATH" in message
    empty = invoke("outline", "--json", source="")
    assert empty.returncode == 0
    assert json.loads(empty.stdout)["diagnostics"][0]["kind"] == "empty_stdin"
    (tmp_path / "input.py").write_text("")
    explicit = invoke("outline", "--file", "input.py", "--json", cwd=tmp_path, source="")
    assert explicit.returncode == 0
    assert not json.loads(explicit.stdout)["diagnostics"]


@pytest.mark.parametrize(("edge", "target"), [("resolved", "__stdin__.known"), ("unresolved", None)])
def test_trace_filters_preserve_order_writes_control_context_and_unknowns(edge, target):
    code = "def known(): pass\ndef run(obj, enabled):\n if enabled:\n  known()\n  obj.limit = 10\n  obj.send()\n"
    result = invoke("trace", "run", "--event", "CALL,ATTR_WRITE", "--edge", edge, "--json", source=code)
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    rows = body["results"]
    assert len(rows) == 2
    call = next(row for row in rows if row["kind"] == "CALL")
    write = next(row for row in rows if row["kind"] == "ATTR_WRITE")
    assert call["target"] == target and call["resolution"] == edge
    assert write["text"] == "obj.limit = 10" and write["line"] == 5
    assert all(row["context"][0]["label"] == "enabled" for row in rows)
    assert [row["order"] for row in rows] == ([1, 2] if edge == "resolved" else [2, 3])
    assert all(row.keys() >= SPAN for row in rows)
    assert body["filters"]["edge"] == edge


def test_refs_role_filters_keep_assignment_value_and_location():
    code = "def run(obj):\n obj.limit = 10\n return obj.limit\n"
    result = invoke("refs", "obj.limit", "--role", "write", "--json", source=code)
    rows = json.loads(result.stdout)["results"]
    assert result.returncode == 0 and len(rows) == 1
    assert rows[0]["text"] == "obj.limit = 10" and rows[0]["line"] == 2


@pytest.mark.parametrize(
    "args",
    [
        ["trace", "start", "--event", "RETUN"],
        ["trace", "start", "--event", ""],
        ["path", "start", "target", "--edge", "resolved"],
        ["callee", "start", "--role", "write"],
    ],
)
def test_invalid_filters_report_errors_instead_of_silent_empty_results(args):
    result = invoke(*args, "--json")
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["message"]
