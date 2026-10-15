# lsp-cli

Read Python code by symbol and Git revision. The command is **`lsp`**.

```bash
lsp show 'v1:Client.send'
lsp show '{v1,v2}:Client.{send,close}'
lsp diff 'v1..v2:Client.send'
lsp refs 'v2:Client.send' --roles call
```

Source is returned with its original file and line numbers. Versioned queries read Git objects without changing the checkout. No repository pre-indexing or database setup is required. The CLI combines ty with Python source and binding analysis.

## Install / uninstall

Linux/macOS, Git and network access are required. The installer uses an isolated [uv tool environment](https://docs.astral.sh/uv/guides/tools/), installs uv if needed, and lets uv obtain Python 3.13. It does not modify shell startup files or require sudo.

```bash
curl -fsSL https://raw.githubusercontent.com/Delva0/lsp-cli/v0.18.17/scripts/install.sh | bash
```

If `lsp` is not on your PATH, use the path printed by the installer (normally `~/.local/bin/lsp`), or add that directory to PATH. Installation downloads the pinned analysis dependencies; it is not a single static executable. No embedding model is downloaded by the public actions.

Uninstall the isolated tool, keeping uv, Python, your repositories and shared download caches:

```bash
curl -fsSL https://raw.githubusercontent.com/Delva0/lsp-cli/v0.18.17/scripts/uninstall.sh | bash
```

With uv already installed:

```bash
uv tool install --python 3.13 'git+https://github.com/Delva0/lsp-cli.git@v0.18.17'
uv tool uninstall lsp-cli
```

## Try it on a real repository

The repository must already contain the requested revisions. Tags, branch names, short/full commit IDs and parent expressions such as `HEAD~1` are accepted.

```bash
git clone https://github.com/psf/requests.git
cd requests

# Compare several complete definitions at two versions in one command.
lsp show '{v2.31.0,v2.32.3}:HTTPAdapter.{send,cert_verify}'

# Locate members, then inspect just documentation/signatures or execution roles.
lsp list 'v2.32.3:HTTPAdapter' --kinds method
lsp show 'v2.32.3:HTTPAdapter' --roles doc
lsp show 'v2.32.3:HTTPAdapter.send' --roles call write return raise

# Read the actual change and its history.
lsp diff 'v2.31.0..v2.32.3:HTTPAdapter.send'
lsp history 'v2.32.3:HTTPAdapter.send'
lsp show 'v2.32.3:HTTPAdapter.send' --blame --roles call

# Reference and inheritance relations within the analyzed input.
lsp refs 'v2.32.3:HTTPAdapter.cert_verify' --roles call
lsp inheritance 'v2.32.3:HTTPAdapter'
```

For a small, self-contained demonstration of all nine actions (including a multi-hop path), clone this repository and run `bash examples/demo.sh`. It creates a temporary two-version Python repository and prints actual results; no model/API credentials are needed.

## Actions

| Action | Purpose | Example |
|---|---|---|
| `list` | Enumerate a scope; optionally recurse/filter symbol kinds | `lsp list 'v1:Client' -r --kinds method` |
| `show` | Read definitions or selected semantic roles | `lsp show 'v1:Client.send' --roles doc call` |
| `refs` | Read uses, including calls and inheritance/override relations | `lsp refs 'v1:Client.send' --roles call` |
| `inheritance` | Ancestors, descendants, overrides/implementations and MRO | `lsp inheritance 'v1:Client'` |
| `path` | Multi-hop call paths between two sets in each selected revision | `lsp path '{v1,v2}:start' 'Client.send'` |
| `history` | Changes to a symbol along Git history | `lsp history 'v2:Client.send'` |
| `diff` | Compare symbols across two revisions, including additions/deletions | `lsp diff 'v1..v2:Client.*' --roles call write` |
| `where` | Position → enclosing symbol hierarchy | `lsp where 'v1:pkg/client.py:20:8'` |
| `complete` | Completion candidates at a source position | `lsp complete 'v1:pkg/client.py:20:8'` |

Use `lsp ACTION --help` for that action's options. Positions use **1-based lines and 0-based columns**; source spans have exclusive end positions. JSON carries complete source spans and source identity.

## Selection and output

- Ordinary names match a symbol or qualified suffix: `Client.send`. Quote sets/globs so the CLI expands them: `'{v1,v2}:Client.{send,close}'`. `*` matches one name/path component and `**` spans components.
- Use `-e` for explicit regex, `-F` for literal matching, `-i` for case-insensitive matching and `-P` for PCRE2. For example: `lsp list -e 'v1:^Client\.(send|close)$'`. Explicit regex/literal matching requires the system `rg` executable; `-P` also needs its PCRE2 support. Ordinary names and globs work without rg.
- `--files` limits the input with path globs, including exclusions; it does not change symbol matching. Working-tree example: `lsp show Client.send --files 'src/**/*.py'`.
- `show`, `refs` and `diff` accept `--roles` (union, deduplicated). Declaration and control context remain visible; a `show` role with no matching body still returns the declaration. See action help for the accepted roles.
- Text groups source by revision/file, merges overlapping fragments and keeps original line numbers. `--json` returns schema version 3 with source hashes, Git identities, query statuses and resolution/coverage diagnostics. No match is distinct from an invalid query or incomplete analysis.
- `show --raw` emits one complete definition without metadata for piping to syntax tools. `--stats` enables optional timing diagnostics; normal output has no timing lines.

Single-file stdin also works. `--stdin-filename` is optional source/module identity, not another file to read:

```bash
git show v2.31.0:requests/adapters.py | lsp show HTTPAdapter.send
git show v2.31.0:requests/adapters.py | lsp refs conn.cert_reqs
```

## Analysis boundaries

Direct source reads avoid starting semantic engines. Relationships use ty where available, supplemented by local lexical/control-flow analysis and validated C3 MRO. The default query path avoids building a redundant pyan3 graph; pyan3 remains a semantic regression oracle. Unresolved dynamic dispatch and imports outside the analysis input stay unresolved; an empty or partial result is not proof that no runtime relationship exists. Inspect the JSON coverage/diagnostics for automated consumers. CPU/IO and query size still matter; this is static evidence, not runtime execution.

A short-lived local worker reuses parsed source and ty sessions between invocations and exits after 300 idle seconds by default (`LSP_IDLE_SECONDS` configures this). It is started automatically, uses a local Unix socket, and needs no service deployment. Temporary snapshots do not modify your checkout. See `src/pyrel/service.py` for the runtime contract.

## Development / container deployment

```bash
git clone https://github.com/Delva0/lsp-cli.git
cd lsp-cli
uv sync --frozen
uv run --frozen pytest -q
uv run --frozen lsp --help
bash examples/demo.sh

# Or install this checkout into the isolated tool environment:
LSP_CLI_SOURCE="$PWD" bash scripts/install.sh
```

`uv sync` installs the project editable for development. In a prepared Python 3.13 container, install the tagged source with pip and run from the mounted repository:

```bash
python -m pip install 'git+https://github.com/Delva0/lsp-cli.git@v0.18.17'
cd /workspace/your-repository
lsp list HEAD: --kinds class function
```

Git must be installed in that container. Source queries do not require installing the target application's dependencies; missing external types may limit relationship resolution.

This release carries the 0.18.17 analysis implementation. `upstream-source.json` records original source hashes; tests are carried over with package import paths adapted. Licensed under MIT; dependency licenses remain their own.
