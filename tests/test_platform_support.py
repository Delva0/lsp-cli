"""Native platform contracts, also exercised by the Windows CI jobs."""

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from graphub_lsp.lsp_worker import server_info_matches, uri_path
from pyrel.platform_support import WINDOWS, lock_file
from .test_git_sources import git
from .test_service import command, runtime  # noqa: F401
from .test_git_sources import repository  # noqa: F401


def test_uri_roundtrip_with_spaces_unicode_and_escaped_characters(tmp_path):
    source = tmp_path / "中文 space #100% + café.py"
    source.write_text("def café(): return 1\n", encoding="utf-8")
    assert uri_path(source.as_uri()) == source
    assert uri_path(source.as_uri().replace("file:///", "file://localhost/")) == source


@pytest.mark.parametrize("uri", [
    "https://example.com/a.py", "file:relative.py", "file:///tmp/a.py?query",
    "file:///tmp/a.py#fragment", "file://remote-server/tmp/a.py",
])
def test_invalid_document_uris_still_rejected(uri):
    with pytest.raises(ValueError):
        uri_path(uri)


@pytest.mark.parametrize("version,accepted", [
    ("0.0.84", True), ("0.0.84 (8dd9a7f7f 2026-09-24)", True),
    ("0.0.85 (8dd9a7f7f 2026-09-24)", False), ("0.0.840", False),
    ("0.0.84-dev", False), ("0.0.84 unknown", False), (None, False),
])
def test_ty_pin_allows_only_release_build_metadata(version, accepted):
    expected = {"name": "ty", "version": "0.0.84"}
    assert server_info_matches({"name": "ty", "version": version}, expected) is accepted
    assert not server_info_matches({"name": "other", "version": version}, expected)


def test_lock_is_exclusive_and_released_on_close(tmp_path):
    path = tmp_path / "lease"
    code = (
        "from pyrel.platform_support import lock_file; import sys\n"
        "with open(sys.argv[1], 'ab') as stream:\n"
        " try: lock_file(stream, blocking=False)\n"
        " except BlockingIOError: raise SystemExit(23)\n"
    )
    def probe():
        return subprocess.run([sys.executable, "-c", code, str(path)], timeout=10, check=False).returncode

    with path.open("ab") as stream:
        lock_file(stream)
        assert probe() == 23
    assert probe() == 0


def test_native_queries_in_unicode_space_directory(tmp_path, runtime):
    root = tmp_path / "中文 workspace space"
    root.mkdir()
    git(root, "init", "-q")
    source = root / "café.py"
    source.write_text("def café(): return 1\ndef caller(): return café()\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "source")
    shown, _ = command(runtime, root, "show", "HEAD:café")
    assert "café" in shown["results"][0]["code"]
    refs, first = command(runtime, root, "refs", "HEAD:café")
    assert refs["results"] and first["ty_pids"]
    complete, second = command(runtime, root, "complete", "HEAD:café.py:2:21")
    assert any(row["label"] == "café" for row in complete["results"])
    assert first["worker_pid"] == second["worker_pid"]
    assert first["ty_pids"] == second["ty_pids"]
    local, _ = command(runtime, root, "show", "café", "--files", str(source))
    assert "café" in local["results"][0]["code"]
    local_complete, _ = command(runtime, root, "complete", str(source) + ":2:21", "--files", str(source))
    assert any(row["label"] == "café" for row in local_complete["results"])


@pytest.mark.skipif(not WINDOWS, reason="Windows job lifecycle and Winsock authentication")
def test_cancel_terminates_ty_and_cleans_snapshots(repository, runtime):
    _, before = command(runtime, repository, "refs", "v1:Base.ping")
    # Raise the same Python interrupt as Ctrl+C after the worker acknowledges a
    # request. The real dispatch finally block must stop the real worker and ty.
    code = """
import os, sys
from pyrel import service
original = service.SocketStream.readline
calls = 0
def interrupted(self):
    global calls
    calls += 1
    if calls == 2:
        raise KeyboardInterrupt
    return original(self)
service.SocketStream.readline = interrupted
raise SystemExit(service.invoke(["show", "leaf", "--json"]))
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=repository, env=runtime["env"],
                            capture_output=True, timeout=15)
    assert result.returncode == 130, result.stderr
    assert not result.stderr
    assert all(not Path(root).exists() for root in before["snapshots"])
    assert_windows_processes_exited(before["ty_pids"])
    _, after = command(runtime, repository, "refs", "v1:Base.ping")
    assert after["worker_pid"] != before["worker_pid"]


def assert_windows_processes_exited(pids):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    for pid in pids:
        handle = kernel.OpenProcess(0x100000, False, pid)
        if handle:
            try:
                assert kernel.WaitForSingleObject(handle, 5000) == 0
            finally:
                kernel.CloseHandle(handle)
        else:
            assert ctypes.get_last_error() == 87


@pytest.mark.skipif(not WINDOWS, reason="Windows authenticated loopback transport")
def test_windows_worker_rejects_unauthenticated_requests(repository, runtime):
    _, before = command(runtime, repository, "refs", "v1:Base.ping")
    path, = Path(runtime["env"]["LSP_RUNTIME_DIR"]).glob("*.sock")
    endpoint = json.loads(path.read_text(encoding="utf-8"))
    for credentials in ({"token": "wrong"}, {"token": "中文"}, [], {"token": None}):
        with socket.create_connection(("127.0.0.1", endpoint["port"]), timeout=5) as client:
            client.sendall(json.dumps(credentials).encode() + b"\n")
            assert client.recv(1024) == b""
    _, after = command(runtime, repository, "refs", "v1:Base.ping")
    assert after["worker_pid"] == before["worker_pid"]
