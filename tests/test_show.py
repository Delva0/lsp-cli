"""Exercise complete source reads, original line boundaries and inherited lookup."""

import json

import pytest

from .test_cli import invoke

SOURCE = '''class Parent:
    @staticmethod
    async def café(
        text="你好",
    ):
        """Keep this docstring."""
        # Keep this comment and the nested definition.
        def nested():
            return text
        return nested()  # Keep the final inline comment.

    def other(self): pass

class Child(Parent):
    pass
'''


def test_show_complete_decorated_async_function_and_character_spans():
    result = invoke("show", "Parent.café", "--json", source=SOURCE)
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    row = body["results"][0]
    assert (row["line"], row["column"], row["end_line"]) == (2, 0, 10)
    assert row["end_column"] == len(SOURCE.splitlines()[9])
    assert row["code"] == "\n".join(SOURCE.splitlines()[1:10])
    assert row["definition"]["line"] == 3 and row["definition"]["column"] == 4
    assert row["symbol"] == "__stdin__.Parent.café"
    assert result.stdout.index('"results"') < result.stdout.index('"sources"')


def test_show_text_is_numbered_and_keeps_indentation():
    result = invoke("show", "Parent.café", "--stdin-filename", "pkg/code.py", source=SOURCE)
    assert result.returncode == 0
    assert "pkg/code.py:2:0-10:" in result.stdout
    assert " 2 |     @staticmethod" in result.stdout
    assert ' 4 |         text="你好",' in result.stdout
    assert "10 |         return nested()  # Keep the final inline comment." in result.stdout
    assert "def other" not in result.stdout


@pytest.mark.parametrize(("symbol", "first", "last"), [("Parent", 1, 12), ("Parent.café.nested", 8, 9)])
def test_show_class_and_nested_function_keep_exact_boundaries(symbol, first, last):
    row = json.loads(invoke("show", symbol, "--json", source=SOURCE).stdout)["results"][0]
    assert row["code"] == "\n".join(SOURCE.splitlines()[first - 1 : last])
    assert (row["line"], row["end_line"]) == (first, last)


def test_show_inherited_method_follows_c3_definition():
    source = "class A:\n def run(self): return 'a'\nclass B(A): pass\nclass C(A):\n def run(self): return 'c'\n"
    source += "class D(B, C): pass\n"
    row = json.loads(invoke("show", "D.run", "--json", source=source).stdout)["results"][0]
    assert row["symbol"] == "__stdin__.C.run"
    assert row["receiver"] == "__stdin__.D"
    assert row["code"] == " def run(self): return 'c'"


def test_show_preserves_crlf_and_does_not_execute_code():
    source = "def run():\r\n    raise RuntimeError('source must never execute')\r\n"
    result = invoke("show", "run", "--json", source=source)
    assert result.returncode == 0
    assert json.loads(result.stdout)["results"][0]["code"] == source[:-2]


@pytest.mark.parametrize("symbols", [[], ["missing"], ["café"]])
def test_show_has_required_target_and_explicit_lookup_errors(symbols):
    source = SOURCE + "class Other:\n def café(self): pass\n"
    result = invoke("show", *symbols, "--json", source=source)
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["message"]


def test_show_missing_base_never_guesses_an_inherited_definition():
    source = "from external import Base\nclass Child(Base): pass\nclass Unrelated:\n def run(self): pass\n"
    result = invoke("show", "Child.run", "--json", source=source)
    assert result.returncode == 2 and json.loads(result.stdout)["results"] == []
