"""Source slicing retains Python byte offsets and physical-line coordinates."""

import ast

import pytest

from lsp_cli.model import Source

from .test_cypher import query, values


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("code", [
    'def café(x):\n    return combine(\n        "雪", x\n    )\n',
    'value = "a\u2028b\u0085c\fend"\n\fdef f():\n    return value\n',
])
def test_segments_match_stdlib_for_unicode_multiline_and_physical_newlines(newline, code):
    text = code.replace("\n", newline)
    source = Source("sample.py", "sample", text)
    nodes = [n for n in ast.walk(source.tree) if hasattr(n, "lineno")]
    for node in nodes:
        expected = ast.get_source_segment(text, node) or ast.unparse(node)
        assert source.segment(node) == expected
        assert source.segment(node) == expected
    assert source.segment(source.tree) == ast.unparse(source.tree)
    assert source.span(source.tree)["end_line"] == code.count("\n")


def test_source_projection_keeps_unicode_separators_on_their_original_line():
    text = 'def café():\n    value = "a\u2028b\fend"\n    return value\n'
    body = query('MATCH (s:Symbol {kind:"function"}) RETURN s.source_code', source=text)
    view = values(body)[0][0]
    assert view["code"] == text.rstrip("\n")
    assert (view["line"], view["end_line"], view["end_column"]) == (1, 3, 16)
    assert body["output"]["source_bodies"][0]["lines"] == 3


def test_symbol_token_locations_preserve_repeated_names_and_unicode_headers():
    from lsp_cli.symbols import Catalog

    source = Source("names.py", "names", (
        "class Café:\n"
        "    def café(\n"
        "        self, café: str = 'café',\n"
        "    ):\n"
        "        self.café.café = café\n"
    ))
    catalog = Catalog([source])
    method = catalog.select("Café.café")[0]
    parameter = catalog.select("Café.café.café")[0]
    assert (method.identifier["line"], method.identifier["column"]) == (2, 8)
    assert (parameter.identifier["line"], parameter.identifier["column"]) == (3, 14)
    attribute = source.tree.body[0].body[0].body[0].targets[0]
    span = catalog.selection(source, attribute, "café", last=True)
    assert (span["line"], span["column"], span["end_column"]) == (5, 18, 22)
    header = catalog.header(source, method.node)
    assert (header["end_line"], header["end_column"]) == (4, 6)
