#!/usr/bin/env bash
# Remove only the isolated lsp-cli installation and its command links.
set -euo pipefail

case "${1:-}" in
  -h|--help) printf '%s\n' 'Uninstall lsp-cli from uv; keep uv, Python, repositories and shared caches.'; exit 0 ;;
  '') ;;
  *) printf 'Unexpected argument: %s\n' "$1" >&2; exit 2 ;;
esac

if command -v uv >/dev/null 2>&1; then
  lsp_uv=$(command -v uv)
elif [ -x "${XDG_BIN_HOME:-$HOME/.local/bin}/uv" ]; then
  lsp_uv="${XDG_BIN_HOME:-$HOME/.local/bin}/uv"
else
  printf '%s\n' 'No uv installation found; nothing to uninstall.'
  exit 0
fi
lsp_tools=$("$lsp_uv" tool dir)
if [ ! -f "$lsp_tools/lsp-cli/uv-receipt.toml" ]; then
  printf '%s\n' 'lsp-cli is not installed in this uv tool directory.'
  exit 0
fi
"$lsp_uv" tool uninstall lsp-cli
