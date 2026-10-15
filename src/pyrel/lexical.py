"""CPython lexical scopes for the flow engine, without a second call graph."""

from __future__ import annotations

import symtable


class LexicalFacts:
    """Binding values and C3 orders belong to Analysis; only locals need symtable.

    pyan also obtains these locals from CPython. Building its whole call graph
    adds no proof to Analysis: that engine independently resolves every use and
    validates every MRO before accepting it. Keep PyanFacts as a test oracle.
    """

    def __init__(self, sources):
        self.warnings = []
        self.mro = {}
        self.scopes = {}
        for source in sources:
            table = symtable.symtable(source.text, source.name, "exec")
            self.register(source.module, table)

    def register(self, name, table):
        self.scopes[name] = frozenset(
            symbol.get_name() for symbol in table.get_symbols()
            if symbol.is_assigned() and not symbol.is_imported()
            and not symbol.is_global() and not symbol.is_free()
        )
        self.children(name, table)

    def children(self, name, table):
        for child in table.get_children():
            kind = child.get_type()
            if getattr(kind, "value", kind) == "type parameters":
                # PEP 695 adds an implicit scope absent from the source AST.
                self.children(name, child)
            else:
                self.register(name + "." + child.get_name(), child)

    def local_names(self, scope):
        return set(self.scopes.get(scope, ()))

    def kind(self, name, default):
        return default

    def binding(self, source, node):
        return None
