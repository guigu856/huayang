#Requires -Version 5.1
$ErrorActionPreference = 'Stop'

$RepoUrl = 'https://github.com/guigu856/huayang.git'
$RepoDir = Join-Path $env:LOCALAPPDATA 'huayang\repo'
$UvBin   = Join-Path $env:USERPROFILE '.local\bin'

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}

# 0. Fail fast when the CLI is in use: running processes lock the install directory.
$running = Get-Process huayang, huayang-mcp -ErrorAction SilentlyContinue
if ($running) {
    $running | Select-Object Id, ProcessName | Format-Table -AutoSize
    throw 'huayang / huayang-mcp is running and locks the install directory. Close the processes above (admin console, MCP server) and re-run.'
}

# 1. Locate project: use current directory when it is the huayang repo, otherwise clone.
$projectDir = $null
if ((Test-Path 'pyproject.toml') -and (Select-String -Path 'pyproject.toml' -Pattern '^name = "huayang"' -Quiet)) {
    $projectDir = (Get-Location).Path
    Step "Using current directory: $projectDir"
} else {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Step 'git not found; installing Git via winget...'
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            winget install --id Git.Git -e --silent --accept-package-agreements --accept-source-agreements
            Refresh-Path
        }
        if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
            throw 'git is required. Install it from https://git-scm.com/download/win and re-run.'
        }
    }
    if (Test-Path $RepoDir) {
        Step "Repo already at $RepoDir; pulling latest..."
        git -C $RepoDir pull --ff-only
        if ($LASTEXITCODE -ne 0) { Write-Host '    pull failed; using existing checkout.' }
    } else {
        Step "Cloning $RepoUrl -> $RepoDir"
        git clone $RepoUrl $RepoDir
    }
    $projectDir = $RepoDir
}

# 2. Ensure uv.
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Step 'uv not found; installing uv...'
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    Refresh-Path
    $env:Path = "$UvBin;$env:Path"
}
Step "uv $(uv --version)"

# 3. Ensure FFmpeg (required by download/render features).
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Step 'FFmpeg not found; installing via winget...'
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id Gyan.FFmpeg -e --silent --accept-package-agreements --accept-source-agreements
        Refresh-Path
    }
    if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
        Write-Host '    FFmpeg could not be installed automatically; download/render features need it: https://ffmpeg.org/download.html'
    }
}

# 4. Install dependencies, browser, and the huayang CLI.
Push-Location $projectDir
try {
    Step 'Syncing Python dependencies (uv sync)...'
    uv sync
    Step 'Installing Playwright Chromium...'
    uv run playwright install chromium
    Step 'Installing huayang CLI (uv tool)...'
    uv tool install --force --editable .
} finally {
    Pop-Location
}

# 5. Verify.
if (Test-Path (Join-Path $UvBin 'huayang-mcp.exe')) {
    Step "Done. CLI installed to $UvBin"
} else {
    Write-Host 'WARNING: huayang-mcp.exe not found; check the output above.'
}
if ($env:Path -notlike "*$UvBin*") {
    Write-Host "NOTE: add $UvBin to your user PATH so 'huayang' and 'huayang-mcp' work in new terminals."
}
Write-Host ''
Write-Host 'Next steps:'
Write-Host '  huayang        # start the admin console at http://127.0.0.1:8788'
Write-Host '  huayang-mcp    # start the MCP server'
