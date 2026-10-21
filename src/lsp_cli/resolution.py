"""Explain named bindings using the existing occurrence-level binding engine.

Source history is explanatory evidence, not an alternative type inference
engine. Only Analysis values and member lookup can establish a target.
"""

from __future__ import annotations

import ast

from .model import UNKNOWN, Value, unknown


class Bindings:
    def __init__(self, analysis, definitions):
        self.analysis = analysis
        self.definitions = definitions
        self.sources = {source.name: source for source in analysis.sources}

    def value(self, name):
        a = self.analysis
        if name in a.modules:
            return Value("module", name)
        owner, _, short = name.rpartition(".")
        if owner in a.symbols:
            state = a.summary(a.symbols[owner]).state
            value = state.names.get(short, UNKNOWN)
            if owner in a.global_writes and short in a.global_writes[owner]:
                return unknown("global binding can be reassigned by another function", value)
            return value
        return UNKNOWN

    def evidence(self, name, limit=128):
        """Follow RHS/import binding history, retaining occurrence cutoffs.

        Earlier writes may have been overwritten. Each occurrence retains its
        value and control context; the separate final binding decides resolution.
        """
        found, visited = {}, set()
        complete = True

        def visit(binding, cutoff=None, depth=0):
            nonlocal complete
            key = (binding, cutoff)
            if key in visited:
                return
            visited.add(key)
            if depth > 24 or len(found) >= limit:
                complete = False
                return
            owner, _, _ = binding.rpartition(".")
            if owner not in self.analysis.symbols:
                return
            summary = self.analysis.summary(self.analysis.symbols[owner])
            for index, ref in enumerate(summary.refs):
                if cutoff is not None and index >= cutoff:
                    break
                if ref.get("binding") != binding or ref.get("role") not in {"write", "import", "definition"}:
                    continue
                if ref.get("unreachable"):
                    continue
                source = self.sources[ref["source"]]
                span = ref.get("statement_span", {k: ref[k] for k in
                                                  ("source", "line", "column", "end_line", "end_column")})
                declarations = []
                local = (self.definitions.in_source(source) if hasattr(self.definitions, "in_source")
                         else self.definitions)
                for symbol in local:
                    if symbol.name != binding or symbol.source is not source:
                        continue
                    own = source.span(symbol.node)
                    if ((own["line"], own["column"]) <= (span["line"], span["column"])
                            and (own["end_line"], own["end_column"]) >= (span["end_line"], span["end_column"])):
                        declarations.append(symbol)
                node = next((s.node for s in declarations), None)
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    span = source.span(node)
                text = ref.get("text", "")
                if not text:
                    text = source.segment(node) if isinstance(node, (ast.Import, ast.ImportFrom)) else ref["expression"]
                record = {**span, "name": binding, "role": ref["role"], "text": text,
                          "context": ref.get("context", []),
                          "value": {k: ref[k] for k in
                                    ("resolution", "target", "reason", "candidates", "import_target") if k in ref},
                          **source.provenance}
                location = tuple(record[k] for k in ("name", "source", "line", "column", "end_line", "end_column"))
                if len(found) >= limit and location not in found:
                    complete = False
                    return
                found[location] = record
                if isinstance(node, ast.ImportFrom):
                    module = self.analysis.import_module(source, node)
                    for alias in node.names:
                        if binding.endswith("." + (alias.asname or alias.name)):
                            visit(f"{module}.{alias.name}", depth=depth + 1)
                # RHS references already contain actual occurrence-time binding
                # names; their position stops later reassignments entering a snapshot.
                if ref["role"] == "write" and ref.get("statement_span"):
                    for position, read in enumerate(summary.refs[:index]):
                        if (read.get("role") == "read" and read.get("binding")
                                and read["source"] == span["source"]
                                and (read["line"], read["column"]) >= (span["line"], span["column"])
                                and (read["end_line"], read["end_column"]) <= (span["end_line"], span["end_column"])):
                            read_owner = read["binding"].rpartition(".")[0]
                            visit(read["binding"], position if read_owner == owner else None, depth + 1)

        visit(name)
        ordered = sorted(found.values(), key=lambda row: (row["source"], row["line"], row["column"], row["name"]))
        return ordered, complete

    def describe(self, name):
        value = self.value(name)
        evidence, complete = self.evidence(name)
        return {"name": name, **self.analysis.describe(value), "evidence": evidence,
                "evidence_complete": complete, "evidence_kind": "binding_history"}

    def member(self, value, attr, state=None):
        # A class-qualified method scope uses the same receiver convention as
        # Analysis.select, including cls/self dispatch and inherited methods.
        if value.kind == "class":
            value = Value("instance", value.target, identity=value.identity)
        return self.analysis.member(value, attr, state)

    def select_member(self, selector):
        from .discovery import SelectionError, matching

        parts = selector.split(".")
        for length in range(len(parts) - 1, 0, -1):
            matches = matching(self.definitions, ".".join(parts[:length]))
            names = {symbol.name for symbol in matches}
            if len(names) > 1:
                raise SelectionError(f"ambiguous receiver binding: {'.'.join(parts[:length])}", "ambiguous", matches)
            if not names:
                continue
            name = next(iter(names))
            owner = self.analysis.symbols.get(name.rpartition(".")[0])
            state = self.analysis.summary(owner).state if owner else None
            value = self.value(name)
            if all(symbol.kind == "module" for symbol in matches) and value.kind == "module":
                members = self.analysis.globals[value.target].names
                if (parts[length] not in members and "*" not in members
                        and f"{value.target}.{parts[length]}" not in self.analysis.modules):
                    return None  # Preserve declaration-not-found candidate feedback.
            report = self.describe(name)
            report["expression"] = selector
            report["steps"] = [{"expression": name, **self.analysis.describe(value)}]
            alternatives = []
            for offset, attr in enumerate(parts[length:], length):
                if value.kind == "unknown":
                    for target in value.candidates:
                        candidate = self.analysis.value(target)
                        for member in parts[offset:]:
                            candidate = self.member(candidate, member, state)
                        alternatives.append({"binding": target, **self.analysis.describe(candidate)})
                    break
                value = self.member(value, attr, state)
                report["steps"].append({"expression": ".".join(parts[:offset + 1]),
                                        **self.analysis.describe(value)})
            for key in ("definition", "inferred_type", "value_kind", "literal", "candidates",
                        "receiver", "dispatch", "import_target", "reason"):
                report.pop(key, None)
            report.update(self.analysis.describe(value))
            report["alternatives"] = alternatives
            if value.receiver:
                report["mro"] = self.analysis.mro(value.receiver)
            symbol = self.analysis.symbols.get(value.target)
            resolved = value.kind in {"function", "class", "module"} and symbol is not None
            if not resolved:
                report.update(resolution="unresolved", target=None)
                report.setdefault("reason", "member binding is not established by the supplied source")
            candidates = [self.analysis.symbols[item["target"]] for item in alternatives
                          if item.get("resolution") == "resolved" and item.get("target") in self.analysis.symbols]
            return ([(symbol, value.receiver)] if resolved else []), report, candidates
        return None
