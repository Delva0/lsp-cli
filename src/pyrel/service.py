"""Keep CLI source caches and ty processes in an automatically managed local worker.

Workers are isolated by repository, executable, environment and implementation.
Sequential commands reuse an idle worker; concurrent commands lease separate
workers, preserving Bash concurrency and query-local scopes. Idle exit releases
every snapshot and language server. The transport carries JSON only, never
executable objects.
"""

import base64
import fcntl
import gc
import hashlib
import importlib.util
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import traceback
from contextlib import chdir, redirect_stderr, redirect_stdout, suppress
from pathlib import Path

from .session import Session


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
    directory = Path(os.environ.get("LSP_RUNTIME_DIR", Path(tempfile.gettempdir()) / f"lsp-cli-{os.getuid()}"))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    return directory / (digest.hexdigest()[:20] + ".sock")


def connect(path):
    client = socket.socket(socket.AF_UNIX)
    try:
        client.connect(str(path))
    except OSError:
        client.close()
        raise
    return client


def connection(path):
    with path.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return connect(path)
        except (FileNotFoundError, ConnectionRefusedError):
            path.unlink(missing_ok=True)
        directory = path.with_suffix(".work")
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(mode=0o700)
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(path))
            path.chmod(0o600)
            listener.listen()
            with path.with_suffix(".log").open("ab") as log:
                # Inherit the already listening socket so concurrent first calls
                # cannot launch competing workers or need startup polling.
                subprocess.Popen(
                    [sys.executable, "-I", "-m", __name__, str(listener.fileno()), str(path)],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    cwd=Path(__file__).parent,
                    pass_fds=(listener.fileno(),),
                    start_new_session=True,
                    env={**os.environ, "TMPDIR": str(directory)},
                )
        return connect(path)


def lease(path):
    slot = 0
    while True:
        candidate = path.with_name(f"{path.stem}-{slot}.sock")
        lock = candidate.with_suffix(".lease").open("w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
        return 128 + signal.SIGPIPE
    return result["status"]


def invoke(argv):
    """Forward a CLI invocation without consuming stdin unless its query needs it.

    Args:
        argv: The original CLI arguments, interpreted in the caller's directory.
    """
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
    with lock, connection(path) as client, client.makefile("rwb") as stream:
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
                    os.kill(pid, signal.SIGINT)
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


def serve(fd, path):
    inode = path.stat().st_ino
    # The worker owns this process. AST-heavy queries keep most objects alive;
    # the default 700-object nursery repeatedly scans that retained graph.
    # Keep cyclic collection enabled, with a bounded, larger nursery.
    gc.set_threshold(50_000, *gc.get_threshold()[1:])

    def terminate(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    try:
        with socket.socket(fileno=fd) as listener, Session() as shared:
            listener.settimeout(float(os.environ.get("LSP_IDLE_SECONDS", "300")))
            while True:
                try:
                    client, _ = listener.accept()
                except TimeoutError:
                    return
                with client, client.makefile("rwb") as stream:
                    try:
                        request = receive(stream)
                        send(stream, {"pid": os.getpid()})
                        send(stream, execute(request, stream, shared))
                    except (ConnectionError, BrokenPipeError):
                        # A closed frontend must not prevent later invocations.
                        continue
    except KeyboardInterrupt:
        pass
    finally:
        # A replacement worker may already own the same endpoint after a crash.
        if path.exists() and path.stat().st_ino == inode:
            path.unlink()
            shutil.rmtree(path.with_suffix(".work"))


if __name__ == "__main__":
    serve(int(sys.argv[1]), Path(sys.argv[2]))
