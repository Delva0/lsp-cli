"""Async stdio JSON-RPC client for LSP (also used inside the sandbox worker)."""

import asyncio
import json
from contextlib import suppress


class RPCError(Exception):
    """Preserve the server's native error code, message and optional data."""

    def __init__(self, error):
        self.error = error
        super().__init__(error["message"])


async def read_message(reader):
    headers = {}
    while line := await reader.readline():
        if line in (b"\r\n", b"\n"):
            break
        name, value = line.decode("ascii").split(":", 1)
        headers[name.lower()] = value.strip()
    if not line:
        raise EOFError("JSON-RPC process closed stdout")
    length = int(headers["content-length"])
    if length < 0 or length > 128 * 1024 * 1024:
        raise ValueError("Invalid JSON-RPC Content-Length")
    return json.loads(await reader.readexactly(length))


def frame(message):
    body = json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    return f"Content-Length: {len(body)}\r\n\r\n".encode() + body


class Client:
    """Correlate concurrent requests; drain stderr and answer server requests."""

    def __init__(self, process, *, handler=None):
        self.process, self.handler = process, handler
        self.pending, self.sequence, self.stderr = {}, 0, ""
        self.reader = asyncio.create_task(self._read())
        self.errors = asyncio.create_task(self._stderr())
        self.failure = None

    @classmethod
    async def start(cls, argv, *, cwd=None, env=None, handler=None):
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=128 * 1024 * 1024,
        )
        return cls(process, handler=handler)

    async def send(self, message):
        if self.failure:
            raise RuntimeError(f"JSON-RPC process failed: {self.failure}; {self.stderr[-2000:]}")
        self.process.stdin.write(frame(message))
        await self.process.stdin.drain()

    async def notify(self, method, params=None):
        await self.send({"jsonrpc": "2.0", "method": method, "params": params})

    async def request(self, method, params=None, *, timeout=60):  # noqa: ASYNC109 -- RPC deadline sends cancellation.
        self.sequence += 1
        ident = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await self.send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
            async with asyncio.timeout(timeout):
                return await future
        except (TimeoutError, asyncio.CancelledError):
            with suppress(Exception):
                await self.notify("$/cancelRequest", {"id": ident})
            raise
        finally:
            self.pending.pop(ident, None)

    async def _read(self):
        try:
            while True:
                message = await read_message(self.process.stdout)
                if "method" in message:
                    answer = None
                    if self.handler:
                        answer = await self.handler(message["method"], message.get("params"))
                    if "id" in message:
                        await self.send({"jsonrpc": "2.0", "id": message["id"], "result": answer})
                elif future := self.pending.get(message.get("id")):
                    if not future.done():
                        if "error" in message:
                            future.set_exception(RPCError(message["error"]))
                        else:
                            future.set_result(message.get("result"))
        except (Exception, asyncio.CancelledError) as exc:
            self.failure = exc
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError(f"JSON-RPC connection lost: {exc}"))

    async def _stderr(self):
        while chunk := await self.process.stderr.read(8192):
            self.stderr = (self.stderr + chunk.decode(errors="replace"))[-16000:]

    async def close(self):
        if self.process.returncode is None:
            with suppress(Exception):
                await self.request("shutdown", timeout=3)
                await self.notify("exit")
            self.process.stdin.close()
            try:
                async with asyncio.timeout(3):
                    await self.process.wait()
            except TimeoutError:
                with suppress(ProcessLookupError):
                    self.process.kill()
                await self.process.wait()
        self.reader.cancel()
        self.errors.cancel()
        await asyncio.gather(self.reader, self.errors, return_exceptions=True)
