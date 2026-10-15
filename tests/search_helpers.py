"""Exercise retained search internals without re-exposing a public CLI action."""

import io
from unittest.mock import patch

from pyrel.commands import parser
from pyrel.search import search
from pyrel.session import Session
from pyrel.workspace import Workspace


def internal_search(
    query,
    *,
    source="",
    scope="",
    revision=None,
    repo=None,
    kinds=(),
    mode="fuzzy",
    limit=20,
    all_results=False,
    files=(),
):
    argv = ["list", *files]
    if repo:
        argv.extend(["--repo", str(repo)])
    args = parser().parse_args(argv)
    args.command, args.rev = "search", revision or args.rev
    with patch("sys.stdin", io.StringIO(source)), Session() as session:
        ws = Workspace(args, session, [scope] if scope else [], all_files=True)
        return search(ws, scope, query, list(kinds), mode, limit, all_results)
