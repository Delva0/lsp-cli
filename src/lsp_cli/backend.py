"""Pinned pyan3 integration; retain facts before its graph loses call sites."""

from __future__ import annotations

import ast
import logging

from pyan.analyzer import CallGraphVisitor
from pyan.node import Node


class SiteVisitor(CallGraphVisitor):
    """Keep pyan's resolved bindings per occurrence, including duplicate calls."""

    def _init_common(self, *args, **kwargs):
        super()._init_common(*args, **kwargs)
        self.bindings = {}

    def visit(self, node):
        result = super().visit(node)
        if isinstance(node, (ast.Name, ast.Attribute, ast.Call)):
            self.bindings[(self.filename, node.lineno, node.col_offset)] = (
                result.get_name() if isinstance(result, Node) and result.namespace is not None else None
            )
        return result


class PyanFacts:
    """Reuse upstream symbols, lexical locals, bindings and valid C3 orders.

    Uses edges deliberately do not become calls: they also include references,
    annotations, decorators and wildcard expansion. Per-site facts are used
    only after the flow layer establishes the binding at that occurrence.
    """

    def __init__(self, sources):
        self.warnings = []
        # Concurrent revision analyses must not capture one another's warnings.
        logger = logging.Logger("lsp_cli.pyan")  # noqa: LOG001 -- per-analysis capture, never a shared global logger
        logger.setLevel(logging.WARNING)
        logger.propagate = False
        warnings = self.warnings

        class Capture(logging.Handler):
            def emit(self, record):
                message = record.getMessage()
                if record.levelno >= logging.WARNING and message not in warnings:
                    warnings.append(message)

        handler = Capture()
        logger.addHandler(handler)
        try:
            self.visitor = SiteVisitor.from_sources(
                [(s.text, s.module + (".__init__" if s.is_package else "")) for s in sources],
                logger=logger,
                cull_subsumed_edges=False,
                use_parameter_annotations=False,
            )
        finally:
            logger.removeHandler(handler)
        self.nodes = {
            n.get_name(): n
            for group in self.visitor.nodes.values()
            for n in group
            if n.namespace is not None and n.defined
        }
        self.mro = {n.get_name(): [b.get_name() for b in bases] for n, bases in self.visitor.mro.items()}

    def local_names(self, scope):
        result = self.visitor.scopes.get(scope)
        return set(result.locals) if result is not None else set()

    def binding(self, source, node):
        module = source.module + (".__init__" if source.is_package else "")
        return self.visitor.bindings.get((module, node.lineno, node.col_offset))

    def kind(self, name, default):
        node = self.nodes.get(name)
        return node.flavor.value if node is not None else default
