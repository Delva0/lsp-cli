#requires -Version 7.0
& {
    $ErrorActionPreference = 'Stop'
    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    $uvDirectory = if ($env:UV_INSTALL_DIR) { $env:UV_INSTALL_DIR } else {
        Join-Path $env:USERPROFILE '.local\bin'
    }
    $uv = if ($uvCommand) { $uvCommand.Source } else { Join-Path $uvDirectory 'uv.exe' }
    if (-not (Test-Path -LiteralPath $uv)) {
        Write-Host 'No uv installation found; nothing to uninstall.'
        return
    }
    $toolRoot = (& $uv tool dir | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Cannot locate the uv tool directory.' }
    if (-not (Test-Path -LiteralPath (Join-Path $toolRoot 'lsp-cli\uv-receipt.toml'))) {
        Write-Host 'lsp-cli is not installed in this uv tool directory.'
        return
    }
    & $uv tool uninstall lsp-cli
    if ($LASTEXITCODE -ne 0) { throw 'lsp-cli uninstall failed.' }
    Write-Host 'Removed lsp-cli. Kept uv, Python, repositories and shared caches.'
}
