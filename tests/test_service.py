"""Verify reuse across actual CLI processes without mixing revisions or inputs."""

import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path

import pytest

from lsp_cli.commands import parser
from lsp_cli.session import Session
from lsp_cli.platform_support import PIPE_EXIT, WINDOWS, executable, terminate_worker
from lsp_cli.service import remove_work

from .test_git_sources import git, write
from .test_git_sources import repository as repository  # noqa: PLC0414


@pytest.fixture
def runtime(tmp_path_factory):
    directory = tmp_path_factory.mktemp("lsp-runtime")
    state = {"env": {**os.environ, "LSP_RUNTIME_DIR": str(directory)}, "pids": set()}
    yield state
    for pid in state["pids"]:
        with suppress(ProcessLookupError):
            terminate_worker(pid)
    if WINDOWS:
        for endpoint in directory.glob("*.sock"):
            endpoint.unlink()
            remove_work(endpoint.with_suffix(".work"))
    deadline = time.monotonic() + 5
    while list(directory.glob("*.sock")) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not list(directory.glob("*.sock"))


def command(runtime, cwd, *args, source="", status=0):
    result = subprocess.run(
        [str(executable("lsp")), *args, "--json", "--stats"],
        input=source,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=cwd,
        env=runtime["env"],
        timeout=30,
        check=False,
    )
    assert result.returncode == status, result.stdout + result.stderr
    stats = next(
        (
            json.loads(line.removeprefix("lsp runtime: "))
            for line in result.stderr.splitlines()
            if line.startswith("lsp runtime: ")
        ),
        {},
    )
    if stats:
        runtime["pids"].add(stats["worker_pid"])
    return json.loads(result.stdout), stats


def test_separate_actions_reuse_ty_and_source_without_touching_worktree(repository, runtime):
    before = git(repository, "status", "--porcelain")
    _, first = command(runtime, repository, "show", "v1:Worker.run", "--roles", "call")
    result, second = command(runtime, repository, "refs", "v1:Base.ping")
    assert result["results"]
    assert first["worker_pid"] == second["worker_pid"]
    assert first["ty_pids"] == second["ty_pids"] and len(first["ty_pids"]) == 1
    assert first["snapshots"] == second["snapshots"]
    assert second["source_files"] >= first["source_files"] > 0
    assert git(repository, "status", "--porcelain") == before


def test_ref_changes_and_revision_labels_stay_fresh(repository, runtime):
    git(repository, "branch", "moving", "v1")
    first, _ = command(runtime, repository, "show", "moving:Base.ping")
    assert "return 1" in first["results"][0]["code"]
    git(repository, "branch", "-f", "moving", "v2")
    second, _ = command(runtime, repository, "show", "moving:Base.ping")
    assert "return 2" in second["results"][0]["code"]
    assert first["git"]["commit"] != second["git"]["commit"]
    assert second["results"][0]["source"].startswith("moving:")
    tagged, _ = command(runtime, repository, "show", "v2:Base.ping")
    assert tagged["results"][0]["source"].startswith("v2:")
    missing, _ = command(runtime, repository, "show", "nonexistent:Base.ping", status=2)
    assert "Cannot resolve Git revision" in missing["error"]["message"]


def test_local_content_and_stdin_are_fresh_but_identical_inputs_reuse_ty(repository, runtime):
    source = "def leaf(): return 1\ndef caller(): return leaf()\n"
    file = repository / "local.py"
    file.write_text(source)
    first, a = command(runtime, repository, "refs", "leaf", "--files", "local.py")
    second, b = command(runtime, repository, "show", "caller", "--files", "local.py", "--roles", "call")
    assert first["results"] and second["results"]
    assert a["ty_pids"] == b["ty_pids"] and len(a["ty_pids"]) == 1
    file.write_text(source.replace("return 1", "return 22"))
    changed, _ = command(runtime, repository, "show", "leaf", "--files", "local.py")
    assert "return 22" in changed["results"][0]["code"]
    for value in (3, 4):
        result, _ = command(runtime, repository, "show", "leaf", source=f"def leaf(): return {value}\n")
        assert f"return {value}" in result["results"][0]["code"]


def test_cached_discovery_does_not_hide_budget_or_scope(repository, runtime):
    complete, _ = command(runtime, repository, "refs", "v1:Base.ping")
    assert complete["coverage"]["complete"]
    limited, _ = command(
        runtime,
        repository,
        "show",
        "v1:pkg.worker.Worker.run",
        "--roles",
        "call",
        "--max-files",
        "1",
    )
    assert not limited["coverage"]["complete"]
    assert any(row["resolution"] == "unresolved" for row in limited["occurrences"])


def test_completion_reuses_source_content_without_reusing_labels(repository, runtime):
    source = "class Worker:\n    def execute(self): pass\nw = Worker()\nw.\n"
    snapshots, pids = [], []
    for label in ("first.py", "second.py"):
        result, stats = command(
            runtime,
            repository,
            "complete",
            label + ":4:2",
            "--module",
            "example",
            "--stdin-filename",
            label,
            source=source,
        )
        assert "execute" in {row["label"] for row in result["results"]}
        assert all(row["source"] == label for row in result["results"])
        snapshots.append(stats["snapshots"])
        pids.append(stats["ty_pids"])
    assert snapshots[0] == snapshots[1] and len(snapshots[0]) == 1
    assert pids[0] == pids[1] and len(pids[0]) == 1


def test_worker_crash_restarts_and_idle_exit_cleans_native_resources(repository, runtime):
    _, first = command(runtime, repository, "refs", "v1:Base.ping")
    if WINDOWS:
        terminate_worker(first["worker_pid"])
    else:
        os.kill(first["worker_pid"], signal.SIGKILL)
    time.sleep(0.05)
    _, restarted = command(runtime, repository, "refs", "v1:Base.ping")
    assert first["worker_pid"] != restarted["worker_pid"]
    assert all(not Path(root).exists() for root in first["snapshots"])
    runtime["env"]["LSP_IDLE_SECONDS"] = "0.2"
    _, idle = command(runtime, repository, "refs", "v1:Base.ping")
    deadline = time.monotonic() + 5
    while any(Path(root).exists() for root in idle["snapshots"]) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert all(not Path(root).exists() for root in idle["snapshots"])


def test_concurrent_calls_keep_outputs_separate(repository, runtime):
    queries = [("show", "v1:Base.ping"), ("show", "v2:Base.ping"), ("list", "v1:Worker"), ("refs", "v1:Base.ping")]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda args: command(runtime, repository, *args), queries))
    assert "return 1" in results[0][0]["results"][0]["code"]
    assert "return 2" in results[1][0]["results"][0]["code"]
    assert all(result["command"] == args[0] for (result, _), args in zip(results, queries, strict=True))


@pytest.mark.parametrize("large", [False, True])
def test_closed_output_pipe_is_quiet_and_worker_remains_usable(repository, runtime, large):
    # Close the reader before starting, so both buffered and direct writes see
    # EPIPE deterministically, independently of the shell's scheduling.
    path = repository / "pipe_sample.py"
    path.write_text("def sample():\n    return " + repr("x" * (65536 if large else 1)) + "\n")
    _, before = command(runtime, repository, "show", "sample", "--files", str(path))
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    try:
        result = subprocess.run(
            [str(executable("lsp")), "show", "sample", "--files", str(path)],
            stdin=subprocess.DEVNULL, stdout=write_fd, stderr=subprocess.PIPE,
            cwd=repository, env=runtime["env"], timeout=30, check=False,
        )
    finally:
        os.close(write_fd)
    assert result.returncode == PIPE_EXIT, result.stderr
    assert result.stderr == b""
    # The output pipe belongs to this client, not to the reusable analyzer.
    data, stats = command(runtime, repository, "show", "sample", "--files", str(path))
    assert data["results"] and stats["worker_pid"] == before["worker_pid"]


@pytest.mark.parametrize("args,stream", [
    (["--help"], "stdout"),
    (["show", "--unknown-argument"], "stderr"),
    (["show", "--unknown-argument", "--json"], "stdout"),
])
def test_early_cli_output_handles_closed_pipe_without_traceback(tmp_path, args, stream):
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    try:
        result = subprocess.run(
            [str(executable("lsp")), *args],
            stdin=subprocess.DEVNULL,
            stdout=write_fd if stream == "stdout" else subprocess.PIPE,
            stderr=write_fd if stream == "stderr" else subprocess.PIPE,
            cwd=tmp_path, timeout=10, check=False,
        )
    finally:
        os.close(write_fd)
    assert result.returncode == PIPE_EXIT, result.stderr
    assert not (result.stderr if stream == "stdout" else result.stdout)


def test_blocked_stdin_query_does_not_serialize_an_independent_call(repository, runtime):
    with subprocess.Popen(
        [str(executable("lsp")), "show", "leaf", "--json", "--stats"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=repository,
        env=runtime["env"],
    ) as blocked:
        directory = Path(runtime["env"]["LSP_RUNTIME_DIR"])
        deadline = time.monotonic() + 5
        while not list(directory.glob("*.sock")) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert list(directory.glob("*.sock"))
        result, independent = command(runtime, repository, "show", "v1:Base.ping")
        assert result["results"]
        stdout, stderr = blocked.communicate("def leaf(): return 9\n", timeout=10)
    assert blocked.returncode == 0
    assert "return 9" in json.loads(stdout)["results"][0]["code"]
    stats = json.loads(
        next(line.removeprefix("lsp runtime: ") for line in stderr.splitlines() if line.startswith("lsp runtime: ")),
    )
    runtime["pids"].add(stats["worker_pid"])
    assert stats["worker_pid"] != independent["worker_pid"]


def test_git_query_does_not_wait_for_unused_stdin(repository, runtime):
    with subprocess.Popen(
        [str(executable("lsp")), "show", "v1:Base.ping", "--json", "--stats"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=repository,
        env=runtime["env"],
    ) as process:
        # Windows anonymous pipes are small. Drain output concurrently while
        # deliberately keeping stdin open, or wait() tests pipe capacity.
        with ThreadPoolExecutor(max_workers=2) as pool:
            out = pool.submit(process.stdout.read)
            err = pool.submit(process.stderr.read)
            try:
                assert process.wait(timeout=10) == 0
            finally:
                if process.poll() is None:
                    process.kill()
            stdout, stderr = out.result(), err.result()
    assert json.loads(stdout)["results"]
    stats = json.loads(
        next(line.removeprefix("lsp runtime: ") for line in stderr.splitlines() if line.startswith("lsp runtime: ")),
    )
    runtime["pids"].add(stats["worker_pid"])


def test_separate_invocations_reuse_parsed_source_objects(repository):
    args = parser().parse_args(["show", "Worker.run", "--repo", str(repository)])
    with Session() as shared:
        with Session(shared) as first:
            old = first.git(args, "v1").source("pkg/worker.py")
        with Session(shared) as second:
            cached = second.git(args, "v1").source("pkg/worker.py")
        assert cached is old
        write(repository, "local.py", "value = 1\n")
        with Session(shared) as first:
            assert first.file(repository / "local.py") == "value = 1\n"
        write(repository, "local.py", "value = 2\n")
        with Session(shared) as second:
            assert second.file(repository / "local.py") == "value = 2\n"


def test_raw_stdout_preserves_utf8_and_crlf(repository, runtime):
    source = 'def café():\r\n    return "你好"\r\n'.encode("utf-8")
    result = subprocess.run(
        [str(executable("lsp")), "show", "café", "--raw", "--stats"],
        input=source, capture_output=True, cwd=repository, env=runtime["env"], timeout=15,
    )
    assert result.returncode == 0, result.stderr
    stats = json.loads(next(line.removeprefix("lsp runtime: ") for line in result.stderr.decode().splitlines()
                            if line.startswith("lsp runtime: ")))
    runtime["pids"].add(stats["worker_pid"])
    assert result.stdout == source[:-2] + b"\n"
