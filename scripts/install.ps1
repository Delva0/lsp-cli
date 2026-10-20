#requires -Version 7.0
# Run in pwsh, including: irm <this-script-url> | iex
& {
    $ErrorActionPreference = 'Stop'
    if (-not $IsWindows) { throw 'Use scripts/install.sh on Linux/macOS.' }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw 'Git is required. Install Git for Windows, reopen pwsh, then run this installer again.'
    }

    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    $uvDirectory = if ($env:UV_INSTALL_DIR) { $env:UV_INSTALL_DIR } else {
        Join-Path $env:USERPROFILE '.local\bin'
    }
    $uv = if ($uvCommand) { $uvCommand.Source } else { Join-Path $uvDirectory 'uv.exe' }
    if (-not (Test-Path -LiteralPath $uv)) {
        $oldInstallDir = $env:UV_INSTALL_DIR
        $oldNoModifyPath = $env:UV_NO_MODIFY_PATH
        try {
            $env:UV_INSTALL_DIR = $uvDirectory
            $env:UV_NO_MODIFY_PATH = '1'
            Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
        } finally {
            $env:UV_INSTALL_DIR = $oldInstallDir
            $env:UV_NO_MODIFY_PATH = $oldNoModifyPath
        }
        if (-not (Test-Path -LiteralPath $uv)) { throw 'uv installation failed.' }
    }

    $ref = if ($env:LSP_CLI_REF) { $env:LSP_CLI_REF } else { 'v0.18.19' }
    $source = if ($env:LSP_CLI_SOURCE) { $env:LSP_CLI_SOURCE } else {
        "git+https://github.com/Delva0/lsp-cli.git@$ref"
    }
    $python = if ($env:LSP_CLI_PYTHON) { $env:LSP_CLI_PYTHON } else { '3.13' }
    & $uv tool install --python $python --reinstall $source
    if ($LASTEXITCODE -ne 0) { throw 'lsp-cli installation failed.' }
    $toolBin = (& $uv tool dir --bin | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Cannot locate the uv tool bin directory.' }
    $lsp = Join-Path $toolBin 'lsp.exe'
    & $lsp --version
    if ($LASTEXITCODE -ne 0) { throw 'The installed lsp command failed its version check.' }

    # Activate this pwsh session and future terminals without editing profiles.
    $bins = @((Split-Path -Parent $uv), $toolBin) | Select-Object -Unique
    foreach ($bin in $bins) {
        if (($env:PATH -split ';') -notcontains $bin) { $env:PATH = "$bin;$env:PATH" }
    }
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $newUserPath = $userPath
    foreach ($bin in $bins) {
        if (($newUserPath -split ';') -notcontains $bin) {
            $newUserPath = (@($newUserPath, $bin) | Where-Object { $_ }) -join ';'
        }
    }
    if ($newUserPath -ne $userPath) {
        try {
            [Environment]::SetEnvironmentVariable('Path', $newUserPath, 'User')
        } catch {
            Write-Warning "Current session is ready. For future terminals, add to your user PATH: $($bins -join ';')"
        }
    }
    Write-Host "Installed: $lsp"
    Write-Host 'Ready: lsp --help'
}
