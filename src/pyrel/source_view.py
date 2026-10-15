"""Return complete original source lines for a resolved definition."""

import ast


def show(symbol, receiver=""):
    """Include decorators, indentation and inline comments without rewriting code.

    The outer span covers the displayed full lines. The definition span retains
    the AST's narrower boundaries, excluding decorators and trailing comments.
    """
    source, node = symbol.source, symbol.node
    definition = symbol.record()
    first = min([definition["line"], *(d.lineno for d in getattr(node, "decorator_list", []))])
    last = definition["end_line"]
    lines = source.lines[first - 1 : last]
    code = "".join(lines)
    # A source span ends before the final line terminator.
    if code.endswith("\r\n"):
        code = code[:-2]
    elif code.endswith(("\r", "\n")):
        code = code[:-1]
    result = {
        "kind": "SOURCE", "symbol": symbol.name, "symbol_kind": symbol.kind,
        "source": source.name, "line": first, "column": 0,
        "end_line": last, "end_column": len(lines[-1].rstrip("\r\n")) if lines else 0,
        "code": code, "definition": definition,
    }
    if receiver:
        result["receiver"] = receiver
    if isinstance(node, ast.Lambda):
        # A lambda can share its line with other expressions. Return only its
        # exact expression, not unrelated statements on that line.
        result.update(source.span(node), code=source.segment(node))
    return result
