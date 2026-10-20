"""Sandbox-only Git snapshots and native language-server sessions.

No symbol analysis lives here. The installed server owns every LSP result.
The operator starts this worker on first use; stdin EOF closes its sessions.
"""

import asyncio
import io
import json
import os
import re
import sys
import tempfile
import time
import tokenize
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

if __package__:
    from .lsp_client import Client, RPCError, frame, read_message
else:
    from graphub_lsp_client import Client, RPCError, frame, read_message


async def command(*args, cwd=None):
    process = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        output, error = await process.communicate()
    except BaseException:
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise ValueError(error.decode(errors="replace").strip()[-2000:])
    return output.decode().strip()


def uri_path(uri):
    parsed = urlsplit(uri)
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost") or parsed.query or parsed.fragment:
        raise ValueError("Use an absolute file:// URI in standard LSP parameters")
    path = Path.from_uri(uri)
    if not path.is_absolute():
        raise ValueError("LSP document URIs must be absolute")
    return path


def server_info_matches(actual, expected):
    """Match the pin, allowing ty's Windows release commit/date suffix."""
    for key, value in expected.items():
        found = actual.get(key)
        if found == value:
            continue
        if (
            key == "version" and expected.get("name") == "ty" and isinstance(found, str)
            and re.fullmatch(re.escape(value) + r" \([0-9a-f]+ \d{4}-\d{2}-\d{2}\)", found)
        ):
            continue
        return False
    return True


class Session:
    """One immutable commit, one lazily initialized native LSP process."""

    def __init__(self, repo, oid, config):
        self.repo, self.oid, self.config = repo, oid, config
        self.temp = tempfile.TemporaryDirectory(prefix="graphub-lsp-")
        self.root = Path(self.temp.name) / "source"
        self.client, self.initialize_result = None, None
        self.opened, self.document_lock = set(), asyncio.Lock()
        self.timings = {}

    async def start(self):
        tick = time.perf_counter()
        # Local object sharing does not copy the user's index/worktree, fetch,
        # execute checkout hooks, or rewrite the original repository's HEAD.
        await command("git", "-c", "core.hooksPath=/dev/null", "clone", "--shared", "--no-checkout",
                      "--", str(self.repo), str(self.root))
        await command("git", "-c", "core.hooksPath=/dev/null", "-c", "advice.detachedHead=false",
                      "-C", str(self.root), "checkout", "--detach", self.oid)
        self.timings["snapshot_seconds"] = time.perf_counter() - tick
        tick = time.perf_counter()
        self.client = await Client.start(self.config["command"], cwd=self.root, handler=self.server_request)
        self.initialize_result = await self.client.request("initialize", {
            "processId": os.getpid(), "rootUri": self.root.as_uri(),
            "workspaceFolders": [{"uri": self.root.as_uri(), "name": self.repo.name}],
            "capabilities": {
                "general": {"positionEncodings": ["utf-16"]},
                "workspace": {"configuration": True, "workspaceFolders": True, "applyEdit": False},
                "textDocument": {
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                    "hover": {"contentFormat": ["plaintext"]},
                    "diagnostic": {},
                },
            },
            "initializationOptions": self.config.get("initialization_options", {}),
        }, timeout=self.config.get("timeout", 60))
        if expected := self.config.get("server_info"):
            actual = self.initialize_result.get("serverInfo", {})
            if not server_info_matches(actual, expected):
                raise ValueError(f"Installed server does not match pinned server_info: {actual}")
        await self.client.notify("initialized", {})
        await self.client.notify("workspace/didChangeConfiguration", {"settings": self.config.get("settings", {})})
        encoding = self.initialize_result["capabilities"].get("positionEncoding", "utf-16")
        if encoding != "utf-16":
            raise ValueError(f"Server selected unadvertised position encoding: {encoding}")
        self.timings["initialize_seconds"] = time.perf_counter() - tick

    async def server_request(self, method, params):
        if method == "workspace/configuration":
            values = []
            for item in params["items"]:
                value = self.config.get("settings", {})
                section = item.get("section")
                for part in section.split(".") if section else []:
                    value = value.get(part, {})
                values.append(value)
            return values
        if method == "workspace/workspaceFolders":
            return [{"uri": self.root.as_uri(), "name": self.repo.name}]
        if method == "workspace/applyEdit":
            return {"applied": False, "failureReason": "This client observes an immutable Git commit"}
        if method in {"client/registerCapability", "client/unregisterCapability", "window/workDoneProgress/create"}:
            return None
        return None

    def parameters(self, value):
        """Translate repository file URIs to this commit's checkout; preserve all LSP fields."""
        if isinstance(value, list):
            return [self.parameters(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            if key in {"uri", "targetUri"} and isinstance(item, str) and item.startswith("file:"):
                path = uri_path(item)
                if path.is_relative_to(self.repo):
                    path = self.root / path.relative_to(self.repo)
                elif not path.is_relative_to(self.root) and any(
                        part.startswith("graphub-lsp-") for part in path.parts):
                    # Native dependency URIs returned by the server may live outside
                    # the checkout. A different commit's snapshot must never be mixed in.
                    raise ValueError("This URI belongs to a different commit/session; repeat its original query")
                result[key] = path.as_uri()
            elif key == "data":
                result[key] = item  # Opaque server tokens must round-trip unchanged.
            else:
                result[key] = self.parameters(item)
        return result

    async def open_document(self, uri):
        async with self.document_lock:
            if uri in self.opened:
                return
            path = uri_path(uri)
            # Git symlinks must not silently substitute a different checkout's code.
            if path.is_relative_to(self.root) and not path.resolve().is_relative_to(self.root):
                raise ValueError("Document symlink escapes the selected commit")
            raw = path.read_bytes()
            # Python's declared source encoding is authoritative; no parsing or inference.
            encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
            text = raw.decode(encoding)
            await self.client.notify("textDocument/didOpen", {"textDocument": {
                "uri": uri, "languageId": "python", "version": 1, "text": text}})
            self.opened.add(uri)

    async def query(self, api):
        method, params = api["method"], self.parameters(api.get("params", {}))
        if method == "initialize":
            # The adapter owns exactly one handshake; expose its unmodified response.
            if params:
                raise ValueError("Initialization is client-managed; use initialize with empty params to inspect it")
            return self.initialize_result
        if method in {"initialized", "shutdown", "exit", "workspace/executeCommand", "workspace/applyEdit"} or (
                method.startswith(("textDocument/did", "workspace/did", "$/", "notebookDocument/"))):
            raise ValueError("Lifecycle, document synchronization and edits are client-managed for immutable commits")
        document = params.get("textDocument", {})
        if document.get("uri"):
            await self.open_document(document["uri"])
        if (item := params.get("item")) and item.get("uri"):
            await self.open_document(item["uri"])
        return await self.client.request(method, params, timeout=self.config.get("timeout", 60))

    async def close(self):
        if self.client:
            await self.client.close()
        self.temp.cleanup()


class Engine:
    def __init__(self, config):
        self.config, self.sessions, self.locks = config, {}, {}
        self.limit = asyncio.Semaphore(config.get("concurrency", 8))

    async def query(self, commit, lsp_api, *, resolved=False):
        started = time.perf_counter()
        session, cold = None, False
        try:
            async with self.limit, asyncio.timeout(self.config.get("timeout", 60) + 30):
                if resolved:
                    # Internal callers already validated a canonical repository
                    # and immutable object ID. Public API calls still resolve
                    # mutable refs on every invocation below.
                    repo, oid = Path(commit["repo"]), commit["ref"]
                else:
                    repo = await asyncio.to_thread(Path(commit["repo"]).resolve, strict=True)
                    top = await command("git", "-C", str(repo), "rev-parse", "--show-toplevel")
                    if Path(top) != repo:
                        raise ValueError("commit.repo must be the repository root")
                    oid = await command("git", "-C", str(repo), "rev-parse", "--verify", "--end-of-options",
                                        commit["ref"] + "^{commit}")
                key = (str(repo), oid)
                async with self.locks.setdefault(key, asyncio.Lock()):
                    session = self.sessions.get(key)
                    if session and session.client.failure:
                        await session.close()
                        del self.sessions[key]
                        session = None
                    if session is None:
                        cold = True
                        session = Session(repo, oid, self.config)
                        try:
                            await session.start()
                        except BaseException:
                            await session.close()
                            raise
                        self.sessions[key] = session
                result = await session.query(lsp_api)
                response = {"result": result}
        except RPCError as exc:
            response = {"error": exc.error}
        except TimeoutError:
            response = {"error": {"code": -32001, "message": "LSP request timed out; no result was obtained"}}
        except Exception as exc:
            response = {"error": {"code": -32000, "message": str(exc), "data": {"type": type(exc).__name__}}}
        response["commit"] = {**commit, **({"oid": session.oid, "rootUri": session.root.as_uri()} if session else {})}
        response["timing"] = {"seconds": time.perf_counter() - started, "cold": cold,
                              **(session.timings if session and cold else {})}
        return response

    async def close(self):
        await asyncio.gather(*(session.close() for session in self.sessions.values()))
        self.sessions.clear()


async def serve(config):
    """Internal transport only; the model sees the tool's commit/lsp_api interface."""
    engine, tasks = Engine(config), {}
    reader = asyncio.StreamReader(limit=128 * 1024 * 1024)
    await asyncio.get_running_loop().connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)

    async def respond(message):
        try:
            if message["method"] == "query":
                result = await engine.query(**message["params"])
            elif message["method"] == "shutdown":
                for ident, task in list(tasks.items()):
                    if ident != message["id"]:
                        task.cancel()
                await asyncio.gather(*(task for ident, task in tasks.items() if ident != message["id"]),
                                     return_exceptions=True)
                await engine.close()
                result = None
            else:
                raise ValueError("Unknown internal method")
            reply = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        except asyncio.CancelledError:
            reply = {"jsonrpc": "2.0", "id": message["id"],
                     "error": {"code": -32800, "message": "Request cancelled"}}
        except Exception as exc:
            reply = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32603, "message": str(exc)}}
        sys.stdout.buffer.write(frame(reply))
        sys.stdout.buffer.flush()

    try:
        while True:
            message = await read_message(reader)
            if message.get("method") == "exit":
                break
            if message.get("method") == "$/cancelRequest":
                if task := tasks.get(message["params"]["id"]):
                    task.cancel()
            elif "id" in message:
                ident = message["id"]
                tasks[ident] = asyncio.create_task(respond(message))
                tasks[ident].add_done_callback(lambda _, ident=ident: tasks.pop(ident, None))
    except EOFError:
        pass
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        await engine.close()


if __name__ == "__main__":
    asyncio.run(serve(json.loads(sys.argv[1])))
