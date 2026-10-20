"""Operator verification of the released artifact, without modifying its code."""

import asyncio
import importlib.metadata
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

from graphub_lsp.lsp_client import Client

report = {
    "platform": platform.platform(),
    "python": sys.version,
    "architecture": platform.machine(),
    "versions": {
        name: importlib.metadata.version(name)
        for name in ("lsp-cli", "ty", "ladybug", "fastembed")
    },
    "platform_features": {
        "os.getuid": hasattr(os, "getuid"),
        "socket.AF_UNIX": hasattr(socket, "AF_UNIX"),
    },
    "checks": [],
}


def check(name, function):
    try:
        result = function()
    except Exception:
        result = {"ok": False, "traceback": traceback.format_exc()}
    record = {"name": name, **result}
    report["checks"].append(record)
    print(json.dumps(record, ensure_ascii=True), flush=True)


def command(argv, *, cwd=None):
    result = subprocess.run(
        argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=45,
    )
    return {
        "ok": result.returncode == 0, "argv": argv,
        "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
    }


async def ty_probe(executable, root, source):
    async def handler(method, params):
        if method == "workspace/configuration":
            return [None for _ in params["items"]]
        if method == "workspace/workspaceFolders":
            return [{"uri": root.as_uri(), "name": "fixture"}]
        return None

    client = await Client.start([executable, "server"], cwd=root, handler=handler)
    observations = {}
    try:
        initialized = await client.request("initialize", {
            "processId": os.getpid(), "rootUri": root.as_uri(),
            "workspaceFolders": [{"uri": root.as_uri(), "name": "fixture"}],
            "capabilities": {
                "workspace": {"configuration": True},
                "general": {"positionEncodings": ["utf-16"]},
                "textDocument": {"documentSymbol": {"hierarchicalDocumentSymbolSupport": True}},
            },
        }, timeout=30)
        observations["serverInfo"] = initialized["serverInfo"]
        assert initialized["serverInfo"] == {"name": "ty", "version": "0.0.84"}
        await client.notify("initialized", {})
        await client.notify("textDocument/didOpen", {"textDocument": {
            "uri": source.as_uri(), "languageId": "python", "version": 1,
            "text": source.read_text(encoding="utf-8"),
        }})
        document = {"uri": source.as_uri()}
        symbols = await client.request("textDocument/documentSymbol", {"textDocument": document}, timeout=30)
        assert any(symbol["name"] == "greet" for symbol in symbols), symbols
        observations["documentSymbol"] = symbols
        definition = await client.request("textDocument/definition", {
            "textDocument": document, "position": {"line": 3, "character": 11},
        }, timeout=30)
        assert definition, definition
        observations["definition"] = definition
        references = await client.request("textDocument/references", {
            "textDocument": document, "position": {"line": 0, "character": 5},
            "context": {"includeDeclaration": False},
        }, timeout=30)
        assert any(row["range"]["start"]["line"] == 3 for row in references), references
        observations["references"] = references
        completion = await client.request("textDocument/completion", {
            "textDocument": document, "position": {"line": 3, "character": 10},
        }, timeout=30)
        items = completion["items"] if isinstance(completion, dict) else completion
        assert any(item["label"] == "greet" for item in items), items
        observations["completion_labels"] = [item["label"] for item in items]
    finally:
        await client.close()
    assert client.process.returncode == 0, client.stderr
    return {"ok": True, "observations": observations, "server_exit_code": client.process.returncode}


with tempfile.TemporaryDirectory(prefix="lsp windows space ") as directory:
    root = Path(directory)
    source = root / "demo.py"
    source.write_text(
        'def greet(name: str) -> str:\n    return name.upper()\n\nresult = greet("world")\n',
        encoding="utf-8",
    )
    for args in (
        ["init", "-q"], ["add", "demo.py"],
        ["-c", "user.name=Windows verification", "-c", "user.email=fixture@example.invalid",
         "commit", "-qm", "fixture"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True)
    ty = str(Path(sys.executable).with_name("ty.exe"))
    lsp = shutil.which("lsp")
    assert lsp, "uv did not expose the lsp console entry"
    check("ty --version", lambda: command([ty, "--version"]))
    check("ty stdio LSP with Windows file URIs", lambda: asyncio.run(ty_probe(ty, root, source)))
    for args in (
        ["--version"], ["show", "HEAD:greet", "--json"],
        ["refs", "HEAD:greet", "--json"], ["complete", "HEAD:demo.py:4:10", "--json"],
    ):
        check("lsp " + " ".join(args), lambda args=args: command([lsp, *args], cwd=root))

    def uri_roundtrip():
        from graphub_lsp.lsp_worker import uri_path

        actual = uri_path(source.as_uri())
        assert actual == source, (str(source), str(actual))
        return {"ok": True, "uri": source.as_uri(), "path": str(actual)}

    check("lsp-cli Windows file URI roundtrip", uri_roundtrip)

output = Path("windows-probe.json")
output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"Evidence: {output}", flush=True)
if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
    with Path(summary).open("a", encoding="utf-8") as stream:
        stream.write(f"Native {report['platform']}, Python {platform.python_version()}\n\n")
        stream.write("| Check | Result |\n| --- | --- |\n")
        for result in report["checks"]:
            stream.write(f"| {result['name']} | {'PASS' if result['ok'] else 'FAIL'} |\n")
raise SystemExit(0 if all(row["ok"] for row in report["checks"]) else 1)
