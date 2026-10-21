"""Discover lexical bindings, with name matching or local semantic ranking."""

import ast
import difflib
import os
import re
from pathlib import Path

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def context(ws, entry):
    parent = entry.scope.node
    doc = (
        ast.get_docstring(parent)
        if isinstance(parent, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        else ""
    )
    pieces = [entry.kind + " " + entry.name, doc or ""]
    if entry.kind in {"function", "method", "class"}:
        pieces.append(entry.source.segment(entry.node))
    else:
        pieces.extend(entry.source.segment(declaration) for declaration in entry.declarations)
        uses = [record for record in ws.catalog.occurrences if record["entry"] is entry]
        pieces.extend(entry.source.lines[record["node"].lineno - 1].strip() for record in uses[:8])
    return "\n".join(pieces)[:6000]


def semantic(texts, query):
    # Only this path imports ONNX/numpy. Model weights are installation assets;
    # repository embeddings are invocation-local and never written to disk.
    import numpy as np
    from fastembed import TextEmbedding

    cache = Path(os.environ.get("LSP_MODEL_CACHE", Path.home() / ".cache" / "lsp-cli" / "models"))
    try:
        embedder = TextEmbedding(
            MODEL,
            cache_dir=str(cache),
            local_files_only=True,
            threads=min(4, os.cpu_count() or 1),
        )
    except Exception as error:
        raise ValueError(
            "Semantic model weights are not prepared. During environment setup run "
            "python -m lsp_cli.search --prepare-model. "
            f"Cache: {cache}. {type(error).__name__}",
        ) from error
    vectors = np.asarray(list(embedder.embed([query, *texts], batch_size=32)))
    vectors /= np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
    return (vectors[1:] @ vectors[0]).tolist()


def name_score(name, query, mode):
    short = name.rsplit(".", 1)[-1]
    if mode == "exact":
        return 1.0 if name == query or name.endswith("." + query) else None
    needle, full, leaf = query.casefold(), name.casefold(), short.casefold()
    if needle in {full, leaf}:
        return 1.0
    if full.endswith("." + needle):
        return 0.98
    if needle in leaf:
        return 0.85 + 0.1 * len(needle) / max(1, len(leaf))
    if needle in full:
        return 0.75
    words = re.findall(r"\w+", needle)
    if words and all(word in full for word in words):
        return 0.7
    score = difflib.SequenceMatcher(None, leaf, needle).ratio()
    return score * 0.65 if score >= 0.6 else None


def search(ws, scope, query, kinds, mode, limit, all_results):
    candidates = ws.catalog.entries
    if scope:
        scopes = ws.catalog.select(scope)
        if not scopes:
            # A package/directory can contain modules without an __init__.py.
            scoped = [
                entry
                for entry in candidates
                if entry.name.startswith(scope + ".")
                or entry.source.provenance.get("git_path", entry.source.name).startswith(scope.rstrip("/") + "/")
            ]
            if not scoped:
                ws.select(scope)
            candidates = scoped
        else:
            if len(scopes) != 1:
                ws.select(scope)
            root = scopes[0]
            candidates = [
                entry
                for entry in candidates
                if entry.source is root.source and (entry.name == root.name or entry.name.startswith(root.name + "."))
            ]
            if root.kind == "module":
                candidates.extend(
                    entry
                    for entry in ws.catalog.entries
                    if entry.source is not root.source and entry.name.startswith(root.name + ".")
                )
    if kinds:
        candidates = [entry for entry in candidates if entry.kind in kinds]
    if mode == "semantic":
        scores = semantic([context(ws, entry) for entry in candidates], query) if candidates else []
    else:
        scores = [name_score(entry.name, query, mode) for entry in candidates]
    ranked = [(score, entry) for score, entry in zip(scores, candidates, strict=True) if score is not None]
    ranked.sort(
        key=lambda pair: (
            -pair[0],
            pair[1].name,
            pair[1].source.name,
            pair[1].identifier["line"],
            pair[1].identifier["column"],
        ),
    )
    total = len(ranked)
    if not all_results:
        ranked = ranked[:limit]
    revision = ws.loader.revision + ":" if ws.loader else ""
    rows = [
        {
            **entry.record(),
            "address": revision + entry.name,
            "score": round(score, 6),
            "declarations": [entry.source.span(node) for node in entry.declarations],
        }
        for score, entry in ranked
    ]
    result = ws.result("search", rows)
    result.update(
        query=query,
        scope=scope or None,
        match=mode,
        kinds=list(kinds),
        candidates=len(candidates),
        matched=total,
        returned=len(rows),
        truncated=len(rows) < total,
    )
    if mode == "semantic":
        result["backend"].update(name="fastembed-onnx+source", model=MODEL)
    return result


def prepare():
    from fastembed import TextEmbedding

    cache = Path(os.environ.get("LSP_MODEL_CACHE", Path.home() / ".cache" / "lsp-cli" / "models"))
    model = TextEmbedding(MODEL, cache_dir=str(cache), threads=min(4, os.cpu_count() or 1))
    list(model.embed(["model preparation"], batch_size=1))
    print(f"Prepared {MODEL} in {cache}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Prepare semantic search model weights during environment installation",
    )
    parser.add_argument("--prepare-model", action="store_true", required=True)
    parser.parse_args()
    prepare()
