"""Position completion, including unfinished Python, through the native LSP."""

import re

from .fast_sources import NeedsBindingsError


def complete(ws, source, line, column):
    lines = [*source.lines, ""]
    if not 1 <= line <= len(lines) or not 0 <= column <= len(lines[line - 1].rstrip("\r\n")):
        raise ValueError("Completion position is outside the source")
    span = {"source": source.name, "line": line, "column": column, "end_line": line, "end_column": column}
    context = {"triggerKind": 1}
    if column and lines[line - 1][column - 1] == ".":
        context = {"triggerKind": 2, "triggerCharacter": "."}
    try:
        response = ws.request("textDocument/completion", {**ws.params(source, span), "context": context})
        items = response.get("items", []) if isinstance(response, dict) else response or []
        incomplete = response.get("isIncomplete", False) if isinstance(response, dict) else False
    except NeedsBindingsError as error:
        ws.diagnostics.append({"kind": "lsp_fallback", "message": str(error)})
        items, incomplete = [], True
    if not items and not source.parse_error:
        before = lines[line - 1][:column]
        prefix = re.search(r"\w*$", before).group()
        if "." not in before[-len(prefix) - 1 :]:
            scopes = ws.catalog.scope_at(source, line, column)
            if scopes:
                scope = scopes[-1]
                seen = set()
                while scope:
                    for name, entry in scope.bindings.items():
                        if name.startswith(prefix) and name not in seen:
                            items.append({"label": name, "detail": entry.kind + " " + entry.name})
                            seen.add(name)
                    scope = scope.parent
                incomplete = True
    rows = [
        {
            **span,
            "label": item["label"],
            "detail": item.get("detail", ""),
            "insert_text": item.get("insertText", item.get("textEdit", {}).get("newText", item["label"])),
            "lsp": item,
        }
        for item in items
    ]
    result = ws.result("complete", rows)
    result.update(position=span, incomplete=incomplete, status="ok" if rows else "no_candidates")
    return result
