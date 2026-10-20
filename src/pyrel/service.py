"""Keep CLI source caches and ty processes in an automatically managed local worker.

Workers are isolated by repository, executable, environment and implementation.
Sequential commands reuse an idle worker; concurrent commands lease separate
workers, preserving Bash concurrency and query-local scopes. Idle exit releases
every snapshot and language server. The transport carries JSON only, never
executable objects.
"""

import base64
import gc
import hashlib
import importlib.util
import io
import json
import os
import secrets
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import chdir, redirect_stderr, redirect_stdout, suppress
from pathlib import Path

from .session import Session
from .platform_support import PIPE_EXIT, WINDOWS, lock_file, terminate_worker, windows_job


class SocketStream:
    """JSON lines with interruptible waits, including Ctrl+C in PowerShell."""

    def __init__(self, client):
        self.client = client
        self.pending = bytearray()

    def write(self, data):
        self.client.sendall(data)

    def flush(self):
        pass

    def readline(self):
        while b"\n" not in self.pending:
            # A blocking Winsock recv does not wake for Python signal handlers.
            if not select.select([self.client], [], [], 0.1)[0]:
                continue
            data = self.client.recv(65536)
            if not data:
                raise ConnectionError("lsp worker connection closed")
            self.pending.extend(data)
        line, _, remaining = self.pending.partition(b"\n")
        self.pending = bytearray(remaining)
        return bytes(line) + b"\n"


def send(stream, message):
    stream.write(json.dumps(message).encode() + b"\n")
    stream.flush()


def receive(stream):
    line = stream.readline()
    if not line:
        raise ConnectionError("lsp worker connection closed")
    return json.loads(line)


def endpoint(argv):
    from .commands import parser

    args = parser().parse_args(argv)
    root = Path(args.repo or Path.cwd()).resolve()
    found = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--absolute-git-dir"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    identity = found.stdout.strip() if found.returncode == 0 else str(root)
    environment = {k: v for k, v in os.environ.items() if k not in {"PWD", "OLDPWD", "SHLVL", "_"}}
    digest = hashlib.sha256(json.dumps([sys.executable, identity, environment], sort_keys=True).encode())
    native = "graphub_lsp" if __package__ == "pyrel" else __package__.removesuffix(".clis.pyrel") + ".lsp"
    native_root = Path(importlib.util.find_spec(native).origin).parent
    for path in sorted([*Path(__file__).parent.glob("*.py"), *native_root.glob("*.py")]):
        digest.update(str(path).encode())
        digest.update(path.read_bytes())
    default = Path(tempfile.gettempdir()) / ("lsp-cli" if WINDOWS else f"lsp-cli-{os.getuid()}")
    directory = Path(os.environ.get("LSP_RUNTIME_DIR", default))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    return directory / (digest.hexdigest()[:20] + ".sock")


def connect(path):
    client = socket.socket(socket.AF_INET if WINDOWS else socket.AF_UNIX)
    try:
        if WINDOWS:
            endpoint = json.loads(path.read_text(encoding="utf-8"))
            client.settimeout(30)
            client.connect(("127.0.0.1", endpoint["port"]))
            with client.makefile("rwb") as stream:
                send(stream, {"token": endpoint["token"]})
                if receive(stream) != {"ready": endpoint["token"]}:
                    raise ConnectionError("lsp worker authentication failed")
            client.settimeout(None)
        else:
            client.connect(str(path))
    except BaseException:
        client.close()
        raise
    return client


def connection(path):
    with path.with_suffix(".lock").open("ab") as lock:
        lock_file(lock)
        try:
            return connect(path)
        except (FileNotFoundError, ConnectionError, TimeoutError):
            path.unlink(missing_ok=True)
        directory = path.with_suffix(".work")
        if directory.exists():
            remove_work(directory)
        directory.mkdir(mode=0o700)
        with socket.socket(socket.AF_INET if WINDOWS else socket.AF_UNIX) as listener:
            token = None
            if WINDOWS:
                listener.bind(("127.0.0.1", 0))
                token = secrets.token_hex(32)
                path.write_text(json.dumps({"port": listener.getsockname()[1], "token": token}), encoding="utf-8")
            else:
                listener.bind(str(path))
            path.chmod(0o600)
            listener.listen()
            with path.with_suffix(".log").open("ab") as log:
                # Inherit the already listening socket so concurrent first calls
                # cannot launch competing workers or need startup polling.
                process = subprocess.Popen(
                    [sys.executable, "-I", "-m", __name__, str(listener.fileno()), str(path)],
                    stdin=subprocess.PIPE if WINDOWS else subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    cwd=Path(__file__).parent,
                    env={**os.environ, "TMPDIR": str(directory), "TMP": str(directory), "TEMP": str(directory)},
                    **(
                        {"creationflags": subprocess.CREATE_NO_WINDOW}
                        if WINDOWS else {"pass_fds": (listener.fileno(),), "start_new_session": True}
                    ),
                )
                if WINDOWS:
                    try:
                        # Winsock sockets are duplicated explicitly, not inherited
                        # via POSIX file descriptors. No pickle crosses the socket.
                        send(process.stdin, {"socket": base64.b64encode(listener.share(process.pid)).decode(),
                                             "token": token})
                    finally:
                        process.stdin.close()
        return connect(path)


def lease(path):
    slot = 0
    while True:
        candidate = path.with_name(f"{path.stem}-{slot}.sock")
        lock = candidate.with_suffix(".lease").open("ab")
        try:
            lock_file(lock, blocking=False)
        except BlockingIOError:
            lock.close()
            slot += 1
        else:
            return candidate, lock


def emit(result):
    """Write a CLI response, including buffered output, to its shell pipes."""
    try:
        sys.stdout.write(result["stdout"])
        sys.stderr.write(result["stderr"])
        sys.stdout.flush()
        sys.stderr.flush()
    except BrokenPipeError:
        # The consumer has finished. Prevent another flush error at interpreter
        # shutdown without changing SIGPIPE handling for the worker connection.
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())
            os.dup2(sink.fileno(), sys.stderr.fileno())
        return PIPE_EXIT
    return result["status"]


def invoke(argv):
    """Forward a CLI invocation without consuming stdin unless its query needs it.

    Args:
        argv: The original CLI arguments, interpreted in the caller's directory.
    """
    if WINDOWS:
        # pwsh pipes carry UTF-8, regardless of the Windows ANSI code page.
        for stream in (sys.stdout, sys.stderr):
            stream.reconfigure(encoding="utf-8")
        def interrupt(_signum, _frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGBREAK, interrupt)
    try:
        return dispatch(argv)
    except KeyboardInterrupt:
        return 130


def dispatch(argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    path = None
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            path = endpoint(argv)
        except ValueError:
            from .commands import main

            status = main(argv)
        except SystemExit as error:
            status = error.code or 0
    if path is None:
        return emit({"stdout": stdout.getvalue(), "stderr": stderr.getvalue(), "status": status})
    path, lock = lease(path)
    with lock, connection(path) as client:
        stream = SocketStream(client)
        send(stream, {"argv": argv, "cwd": str(Path.cwd()), "tty": sys.stdin.isatty()})
        pid = receive(stream)["pid"]
        finished = False
        previous = signal.getsignal(signal.SIGTERM)

        def terminate(signum, _frame):
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGTERM, terminate)
        try:
            while True:
                result = receive(stream)
                if result.get("stdin"):
                    send(stream, {"data": base64.b64encode(sys.stdin.buffer.read()).decode()})
                else:
                    finished = True
                    break
        finally:
            signal.signal(signal.SIGTERM, previous)
            if not finished:
                with suppress(ProcessLookupError):
                    terminate_worker(pid)
                if WINDOWS:
                    path.unlink(missing_ok=True)
                    remove_work(path.with_suffix(".work"))
        return emit(result)


class Input:
    def __init__(self, stream, tty):
        self.stream, self.tty, self.data = stream, tty, None

    def isatty(self):
        return self.tty

    @property
    def buffer(self):
        if self.data is None:
            send(self.stream, {"stdin": True})
            self.data = io.BytesIO(base64.b64decode(receive(self.stream)["data"]))
        return self.data


def execute(request, stream, shared):
    from .commands import main

    stdout, stderr, stdin = io.StringIO(), io.StringIO(), sys.stdin
    try:
        sys.stdin = Input(stream, request["tty"])
        with chdir(request["cwd"]), redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                status = main(request["argv"], shared=shared)
            except SystemExit as error:
                status = error.code
            except Exception:
                # Match the standalone CLI traceback while keeping a failed
                # query from discarding unrelated immutable revision caches.
                traceback.print_exc()
                status = 1
            if "--stats" in request["argv"]:
                native = [
                    s for owner in shared.owners() if owner.lsp_engine for s in owner.lsp_engine.sessions.values()
                ]
                print(
                    "lsp runtime: "
                    + json.dumps(
                        {
                            "worker_pid": os.getpid(),
                            "ty_pids": [s.client.process.pid for s in native],
                            "snapshots": [str(s.root) for s in native],
                            "source_files": sum(
                                len(loader._source_cache)
                                for owner in shared.owners()
                                for loader in owner.revisions.values()
                            ),
                        },
                    ),
                    file=sys.stderr,
                )
    finally:
        sys.stdin = stdin
    return {"stdout": stdout.getvalue(), "stderr": stderr.getvalue(), "status": status}


def remove_work(directory):
    def writable(function, path, error):
        if not WINDOWS or not isinstance(error, PermissionError):
            raise error
        os.chmod(path, os.stat(path).st_mode | stat.S_IWRITE)
        function(path)

    # Windows may briefly retain mapped DLLs/files after job termination.
    deadline = time.monotonic() + 5
    while True:
        try:
            shutil.rmtree(directory, onexc=writable)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if not WINDOWS or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def serve(listener, path, token=None):
    inode = path.stat().st_ino
    # The worker owns this process. AST-heavy queries keep most objects alive;
    # the default 700-object nursery repeatedly scans that retained graph.
    # Keep cyclic collection enabled, with a bounded, larger nursery.
    gc.set_threshold(50_000, *gc.get_threshold()[1:])

    def terminate(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    try:
        with listener, Session() as shared:
            listener.settimeout(float(os.environ.get("LSP_IDLE_SECONDS", "300")))
            while True:
                try:
                    client, _ = listener.accept()
                except TimeoutError:
                    return
                with client, client.makefile("rwb") as stream:
                    try:
                        if token is not None:
                            client.settimeout(5)
                            auth = stream.readline(4096)
                            credentials = json.loads(auth)
                            supplied = credentials.get("token") if isinstance(credentials, dict) else None
                            if not isinstance(supplied, str) or not secrets.compare_digest(
                                supplied.encode(), token.encode(),
                            ):
                                continue
                            send(stream, {"ready": token})
                            client.settimeout(None)
                        request = receive(stream)
                        send(stream, {"pid": os.getpid()})
                        send(stream, execute(request, stream, shared))
                    except (ConnectionError, BrokenPipeError, TimeoutError, ValueError):
                        # A closed frontend must not prevent later invocations.
                        continue
    except KeyboardInterrupt:
        pass
    finally:
        # A replacement worker may already own the same endpoint after a crash.
        if path.exists() and path.stat().st_ino == inode:
            path.unlink()
            remove_work(path.with_suffix(".work"))


if __name__ == "__main__":
    if WINDOWS:
        job = windows_job()
        startup = receive(sys.stdin.buffer)
        serve(socket.fromshare(base64.b64decode(startup["socket"])), Path(sys.argv[2]), startup["token"])
    else:
        serve(socket.socket(fileno=int(sys.argv[1])), Path(sys.argv[2]))
