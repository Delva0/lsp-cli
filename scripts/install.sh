#!/usr/bin/env bash
# Install into uv's isolated tool environment; no sudo or project changes.
set -euo pipefail

case "${1:-}" in
  -h|--help)
    printf '%s\n' 'Install lsp-cli with uv (Linux/macOS).' \
      'Optional: LSP_CLI_REF=v0.18.17, LSP_CLI_SOURCE=/path/to/checkout' \
      'UV_TOOL_DIR and UV_TOOL_BIN_DIR are honored by uv.'
    exit 0 ;;
  '') ;;
  *) printf 'Unexpected argument: %s\n' "$1" >&2; exit 2 ;;
esac

if command -v uv >/dev/null 2>&1; then
  lsp_uv=$(command -v uv)
elif [ -x "${XDG_BIN_HOME:-$HOME/.local/bin}/uv" ]; then
  lsp_uv="${XDG_BIN_HOME:-$HOME/.local/bin}/uv"
else
  command -v curl >/dev/null 2>&1 || { printf '%s\n' 'curl is required to install uv.' >&2; exit 1; }
  curl --fail --silent --show-error --location https://astral.sh/uv/install.sh |
    env UV_NO_MODIFY_PATH=1 UV_INSTALL_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}" sh
  lsp_uv="${XDG_BIN_HOME:-$HOME/.local/bin}/uv"
fi

lsp_ref=${LSP_CLI_REF:-v0.18.17}
lsp_source=${LSP_CLI_SOURCE:-git+https://github.com/Delva0/lsp-cli.git@$lsp_ref}
if [[ "$lsp_source" == git+* ]]; then
  command -v git >/dev/null 2>&1 || { printf '%s\n' 'git is required; install Git first.' >&2; exit 1; }
fi
"$lsp_uv" tool install --python 3.13 --reinstall "$lsp_source"
lsp_bin=$("$lsp_uv" tool dir --bin)
"$lsp_bin/lsp" --version
printf 'Installed: %s/lsp\n' "$lsp_bin"
case ":$PATH:" in
  *":$lsp_bin:"*) ;;
  *) printf 'Add to your shell: export PATH="%s:$PATH"\n' "$lsp_bin" ;;
esac
