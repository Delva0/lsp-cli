"""Exercise the installed native worker without a Graphub checkout or source injection."""

import asyncio
import json
import sys

import pytest

from lsp_cli.identity import source_identity
from lsp_cli.lsp.client import Client, RPCError
from lsp_cli.lsp.worker import uri_path
from lsp_cli.platform_support import executable

from .test_git_sources import git
from .test_git_sources import repository as repository  # noqa: PLC0414


@pytest.mark.skipif(sys.platform == "win32", reason="The stdio operator worker runs in POSIX containers")
def test_installed_worker_identity_queries_and_eof_cleanup(repository):
    async def run():
        config = {"command": [str(executable("ty")), "server"]}
        client = await Client.start(
            [sys.executable, "-I", "-m", "lsp_cli.lsp.worker", json.dumps(config)], cwd=repository,
        )
        before = git(repository, "status", "--porcelain")
        try:
            identity = await client.request("identity", {})
            assert identity == source_identity()
            assert {"lsp/client.py", "lsp/worker.py"} <= identity["files"].keys()
            result = await client.request("query", {
                "commit": {"repo": str(repository), "ref": "v1"},
                "lsp_api": {"method": "textDocument/documentSymbol", "params": {
                    "textDocument": {"uri": (repository / "pkg/base.py").as_uri()},
                }},
            })
            assert "error" not in result, result
            assert any(symbol["name"] == "Base" for symbol in result["result"])
            snapshot = uri_path(result["commit"]["rootUri"])
            assert snapshot.is_dir() and snapshot != repository
            with pytest.raises(RPCError):
                await client.request("unknown", {})
            assert await client.request("identity", {}) == identity
            client.process.stdin.close()
            await asyncio.wait_for(client.process.wait(), 5)
            assert not snapshot.exists()
            assert git(repository, "status", "--porcelain") == before
        finally:
            await client.close()

    asyncio.run(run())
