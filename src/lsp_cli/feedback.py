"""Separate query execution, returned source bodies, and graph coverage."""

from __future__ import annotations

from .model import source_lines


def output(result):
    bodies, differences = {}, {}

    def visit(value):
        if isinstance(value, dict):
            if value.get("kind") == "SOURCE" and "id" in value and "code" in value:
                bodies.setdefault(value["id"], {key: value[key] for key in
                                  ("id", "symbol", "symbol_kind", "source", "line", "column", "end_line", "end_column")})
                bodies[value["id"]].update(complete=True, lines=len(source_lines(value["code"])),
                                           bytes=len(value["code"].encode("utf-8")))
            elif value.get("kind") == "DIFF" and "id" in value:
                # Text displays a diff's patch, not its embedded original bodies.
                # Those bodies count when projected explicitly beside the diff.
                differences.setdefault(value["id"], {"id": value["id"], "status": value["status"]})
            else:
                for item in value.values():
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for row in result["results"]:
        visit(row["values"])
    return {"status": "error" if "error" in result else "truncated" if result.get("truncated") else "complete",
            "rows": len(result["results"]), "source_bodies": list(bodies.values()),
            "diffs": list(differences.values())}
