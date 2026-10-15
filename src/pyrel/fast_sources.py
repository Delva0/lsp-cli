"""Read exact lexical definitions without constructing a whole binding analysis."""

from .analysis import Analysis
from .model import Symbol
from .queries import query


class NeedsBindingsError(Exception):
    """Inherited or imported selectors need the semantic engine."""


class Definitions:
    def __init__(self, sources):
        self.sources = sources
        self.symbols, self.by_node, self.duplicates = {}, {}, set()
        self.facts, self.diagnostics = None, []
        for source in sources:
            symbol = Symbol(source.module, "module", source.tree, source, None)
            self.symbols[symbol.name] = symbol
            self.by_node[id(source.tree)] = symbol
            self._collect(source.tree, symbol)

    _collect = Analysis._collect

    def select(self, name, **_options):
        for source in self.sources:
            if name in (source.name, source.provenance.get("git_path")):
                name = source.module
                break
        found = ([self.symbols[name]] if name in self.symbols else
                 [s for key, s in self.symbols.items() if key.endswith("." + name)])
        if len(found) > 1 or any(s.name in self.duplicates for s in found):
            raise ValueError(f"ambiguous symbol: {name}; use a qualified, unique definition")
        if not found:
            raise NeedsBindingsError(name)
        return found[0], ""


def collect(args, loader):
    if args.receiver:
        raise NeedsBindingsError("explicit receiver context")
    if args.command in {"outline", "where"}:
        sources, metadata, diagnostics = loader.collect(args.command, args.symbols, args.source)
    else:
        seeds = loader.query_seeds(args.symbols, args.source)
        if not seeds:
            raise NeedsBindingsError("no direct lexical definition")
        sources, metadata, diagnostics = loader.collect("outline", [], sorted(seeds))
    if not metadata["complete"]:
        raise NeedsBindingsError("incomplete lexical discovery")
    metadata["scope"] = "lexical_definitions"
    return Definitions(sources), metadata, diagnostics


def source_query(args, loader):
    definitions, metadata, diagnostics = collect(args, loader)
    command = args.command if args.command in {"outline", "defs", "show", "where"} else "show"
    result = query(definitions, command, args.symbols, line=args.line, column=args.column)
    result["command"] = args.command
    result["git"] = metadata
    result["diagnostics"].extend(diagnostics)
    return result
