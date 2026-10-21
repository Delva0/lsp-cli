# lsp-cli

- This repository is the sole source for the `lsp` command, its Python analysis,
  native LSP transport, tests and packaging. Consumers install `lsp-cli`; do not
  copy code to Graphub or add imports, path fallbacks or source synchronization
  against another checkout.
- Develop on `main` with the repository's own environment: `uv sync --frozen`.
  Run Python through `uv run --frozen`.
- Keep the public command at `lsp_cli.commands:main` and implementation under
  `src/lsp_cli`. The native worker runs as `python -m lsp_cli.lsp.worker`.
- Verify runtime changes with `uv run --frozen pytest -q`, `uv build` and an
  installed-wheel smoke test outside this checkout. Preserve Windows behavior;
  CI checks Windows and Linux on Python 3.13 and 3.14.
- Commit related changes together. Push this repository to its own `origin`;
  Graphub integration changes belong in Graphub's separate repository.
