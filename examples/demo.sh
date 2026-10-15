#!/usr/bin/env bash
# A disposable, local two-version repository; no external application is executed.
set -euo pipefail
lsp_project=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [ -n "${LSP_BIN:-}" ]; then
  lsp_cmd=("$LSP_BIN")
else
  lsp_cmd=(uv run --frozen --project "$lsp_project" lsp)
fi
lsp_demo=$(mktemp -d "${TMPDIR:-/tmp}/lsp-cli-demo.XXXXXXXX")
trap 'rm -rf -- "$lsp_demo"' EXIT
cd -- "$lsp_demo"
git init -q
git config user.name 'lsp-cli demo'
git config user.email 'demo@example.invalid'
cat > demo.py <<'PY'
def encode(value):
    """Prepare a request value."""
    return str(value)

class Base:
    def send(self, value):
        return encode(value)

class Client(Base):
    """A minimal client."""
    def send(self, value):
        self.last = value
        return super().send(value)

def start(value):
    client = Client()
    return client.send(value)
PY
git add demo.py
git commit -qm 'Add client and entry point'
git tag v1
cat > demo.py <<'PY'
def encode(value):
    """Prepare a request value."""
    return str(value)

class Base:
    def send(self, value):
        return encode(value)

class Client(Base):
    """A minimal client."""
    def send(self, value):
        if value is None:
            raise ValueError("missing value")
        self.last = value
        return super().send(value)

def start(value):
    client = Client()
    return client.send(value)
PY
git add demo.py
git commit -qm 'Validate values before sending'
git tag v2

run_lsp() {
  printf '\n$ lsp'; printf ' %q' "$@"; printf '\n'
  "${lsp_cmd[@]}" "$@" < /dev/null
}

run_lsp list v2:Client -r --kinds class method
run_lsp show '{v1,v2}:Client.send'
run_lsp show v2:Client --roles doc
run_lsp show v2:Client.send --roles call write return raise
run_lsp refs v2:encode --roles call
run_lsp inheritance v2:Client
run_lsp path v2:start encode
run_lsp history v2:Client.send -n 2
run_lsp diff v1..v2:Client.send
run_lsp show v2:Client.send --blame --roles write
run_lsp where v2:demo.py:14:8
run_lsp complete v2:demo.py:19:18
run_lsp list v2:Client --json
printf '\n$ git show v1:demo.py | lsp show Client.send\n'
git show v1:demo.py | "${lsp_cmd[@]}" show Client.send
