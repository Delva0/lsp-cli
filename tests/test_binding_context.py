"""Binding source must not make mutually exclusive declarations look sequential."""

import ast

from .test_action_roles import commit_source
from .test_actions import invoke, query
from .test_git_sources import repository as repository  # noqa: PLC0414


def test_binding_show_retains_try_except_without_expanding_initializer_uses():
    source = """try:
    import ssl
    context = make_context()
    context.load_certificates("roots.pem")
except ImportError:
    context = None
"""
    result = query("show", "context", source=source)
    assert not result["occurrences"]
    assert [row["line"] for row in result["results"]] == [1, 3, 5]
    assert [row["code"] for row in result["results"]] == [
        "try:", "    context = make_context()", "except ImportError:\n    context = None",
    ]
    for row in result["results"]:
        assert row["code"] == "\n".join(source.splitlines()[row["line"] - 1:row["end_line"]])
    status, out, err = invoke("show", "context", source=source)
    assert status == 0 and not err
    assert "1 | try:" in out and "5 | except ImportError:" in out
    assert "load_certificates" not in out and "import ssl" not in out
    # Uses remain a separate relation query, including post-assignment setup.
    refs = query("refs", "context", source=source)
    assert any('context.load_certificates("roots.pem")' in row["code"] for row in refs["results"])


def test_local_binding_keeps_all_guards_and_complete_multiline_values():
    source = """def configure(mode):
    match mode:
        case "network":
            with lock:
                if ready:
                    value = (
                        "one",
                        "two",
                    )
                else:
                    value = None
        case _:
            value = False
    unrelated()
"""
    result = query("show", "configure.value", source=source)
    text = "\n".join(row["code"] for row in result["results"])
    for header in ["def configure(mode):", "match mode:", 'case "network":',
                   "with lock:", "if ready:", "else:", "case _:"]:
        assert header in text
    assert '"one",\n                        "two",\n                    )' in text
    assert "unrelated()" not in text
    assert not result["occurrences"]


def test_import_guards_and_raw_syntax_unit_are_distinct():
    source = "if enabled:\n    import missing_dependency as codec\n    unused()\n"
    status, out, err = invoke("show", "codec", source=source)
    assert status == 0 and "showing its import" in err
    diagnostic = err
    assert "1 | if enabled:" in out and "2 |     import missing_dependency as codec" in out
    assert "unused" not in out
    status, raw, err = invoke("show", "codec", "--raw", source=source)
    assert (status, raw, err) == (0, "import missing_dependency as codec\n", diagnostic)
    assert isinstance(ast.parse(raw).body[0], ast.Import)


def test_binding_context_blame_uses_original_commit_and_line(repository):
    source = "try:\n    context = create()\nexcept ImportError:\n    context = None\n"
    commit = commit_source(repository, source, "binding-context")
    result = query("show", "binding-context:roles.context", "--blame", "--repo", str(repository))
    rows = [attribution for row in result["results"] for attribution in row["blame"]]
    assert [row["line"] for row in rows] == [1, 2, 3, 4]
    assert all(row["commit"] == commit and row["origin"]["line"] == row["line"] for row in rows)
