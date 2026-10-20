#requires -Version 7.0
# Exercise the installed command through real PowerShell quoting and pipelines.
$ErrorActionPreference = 'Stop'
$root = Join-Path ([IO.Path]::GetTempPath()) "lsp pwsh 中文 $([Guid]::NewGuid())"
$oldIdle = $env:LSP_IDLE_SECONDS
$oldRuntime = $env:LSP_RUNTIME_DIR
$env:LSP_IDLE_SECONDS = '2'
$env:LSP_RUNTIME_DIR = Join-Path $root 'runtime'
New-Item -ItemType Directory -Path $root | Out-Null

function Invoke-Git {
    & git @args
    if ($LASTEXITCODE -ne 0) { throw "git failed: $args" }
}
function Invoke-LspJson {
    $result = & lsp @args --json
    if ($LASTEXITCODE -ne 0) { throw "lsp failed: $args`n$result" }
    $parsed = ($result -join "`n") | ConvertFrom-Json
    if (-not $parsed.results) { throw "No results for: $args`n$result" }
    Write-Host "PASS: lsp $args"
    return $parsed
}

Push-Location $root
try {
    Invoke-Git init -q
    Invoke-Git config user.name fixture
    Invoke-Git config user.email fixture@example.invalid
    Invoke-Git config core.autocrlf false
    $source = @'
def encode(value):
    return str(value)

class Base:
    def send(self, value):
        return encode(value)

class Client(Base):
    def send(self, value):
        self.last = value
        return super().send(value)

def start(value):
    client = Client()
    return client.send(value)
'@
    Set-Content -LiteralPath demo.py -Value $source -Encoding utf8NoBOM
    Invoke-Git add .
    Invoke-Git commit -qm initial
    Invoke-Git tag v1
    Set-Content -LiteralPath demo.py -Value ($source.Replace('self.last = value', 'self.last = str(value)')) -Encoding utf8NoBOM
    Invoke-Git add .
    Invoke-Git commit -qm updated
    Invoke-Git tag v2

    $null = Invoke-LspJson list 'v2:Client' -r --kinds method
    $null = Invoke-LspJson show '{v1,v2}:Client.send'
    $null = Invoke-LspJson refs 'v2:encode' --roles call
    $null = Invoke-LspJson inheritance 'v2:Client'
    $null = Invoke-LspJson path 'v2:start' encode
    $null = Invoke-LspJson history 'v2:Client.send'
    $null = Invoke-LspJson diff 'v1..v2:Client.send'
    $null = Invoke-LspJson where 'v2:demo.py:10:8'
    $completion = Invoke-LspJson complete 'v2:demo.py:15:18'
    if ('send' -notin $completion.results.label) { throw 'ty completion did not return send.' }

    $pipeResult = git show v1:demo.py | lsp show Client.send --json
    if ($LASTEXITCODE -ne 0) { throw 'git -> lsp pipeline failed.' }
    $piped = ($pipeResult -join "`n") | ConvertFrom-Json
    if ($piped.results[0].code -notmatch 'self.last = value') { throw 'Incorrect pipeline result.' }
    Write-Host 'PASS: git show | lsp show'

    $unicodeResult = 'def 中文(): return "你好 café"' | lsp show 中文 --json
    if ($LASTEXITCODE -ne 0) { throw 'UTF-8 stdin failed.' }
    $unicode = ($unicodeResult -join "`n") | ConvertFrom-Json
    if ($unicode.results[0].code -notmatch '你好 café') { throw 'UTF-8 output was corrupted.' }
    Write-Host 'PASS: UTF-8 PowerShell stdin and stdout'

    # No ty server or snapshot should survive the configured idle timeout.
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    while ((Get-ChildItem $env:LSP_RUNTIME_DIR -Filter '*.sock') -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 100
    }
    if (Get-ChildItem $env:LSP_RUNTIME_DIR -Filter '*.sock') { throw 'Workers did not exit when idle.' }
    if (Get-ChildItem $env:LSP_RUNTIME_DIR -Filter '*.work') { throw 'Worker snapshots were not cleaned.' }
    Write-Host 'PASS: idle shutdown and cleanup'
} finally {
    Pop-Location
    $env:LSP_IDLE_SECONDS = $oldIdle
    $env:LSP_RUNTIME_DIR = $oldRuntime
    Remove-Item -LiteralPath $root -Recurse -Force
}
