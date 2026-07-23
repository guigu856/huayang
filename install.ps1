#Requires -Version 5.1
$ErrorActionPreference = 'Stop'

$RepoUrl = 'https://github.com/guigu856/huayang.git'
$RepoDir = Join-Path $env:LOCALAPPDATA 'huayang\repo'

function Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Fail([string]$Message) { throw $Message }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}
function Test-HuayangSource([string]$Root) {
    $checks = @(
        (Test-Path (Join-Path $Root 'pyproject.toml') -PathType Leaf),
        (Test-Path (Join-Path $Root '.mcp.json') -PathType Leaf),
        (Test-Path (Join-Path $Root '.codex-plugin\plugin.json') -PathType Leaf),
        (Test-Path (Join-Path $Root 'rules\main-agent.md') -PathType Leaf),
        (Test-Path (Join-Path $Root 'skills') -PathType Container),
        (Test-Path (Join-Path $Root 'schemas') -PathType Container)
    )
    if ($checks -contains $false) { return $false }
    return Select-String -Path (Join-Path $Root 'pyproject.toml') -Pattern '^name = "huayang"' -Quiet
}
function Test-ExpectedOrigin([string]$Origin) {
    return $Origin -in @(
        'https://github.com/guigu856/huayang',
        'https://github.com/guigu856/huayang.git',
        'git@github.com:guigu856/huayang',
        'git@github.com:guigu856/huayang.git',
        'ssh://git@github.com/guigu856/huayang',
        'ssh://git@github.com/guigu856/huayang.git'
    )
}
function Assert-ExpectedOrigin([string]$Root) {
    $origin = (& git -C $Root remote get-url origin 2>$null | Out-String).Trim()
    if (-not (Test-ExpectedOrigin $origin)) {
        Fail "Existing checkout has unexpected origin: $(if ($origin) { $origin } else { '<missing>' })"
    }
}
function Ensure-Git {
    if (Get-Command git -ErrorAction SilentlyContinue) { return }
    Step 'git not found; installing Git via winget...'
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id Git.Git -e --silent --accept-package-agreements --accept-source-agreements
        Refresh-Path
    }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Fail 'git is required. Install it from https://git-scm.com/download/win and re-run.'
    }
}

$running = Get-Process huayang, huayang-mcp -ErrorAction SilentlyContinue
if ($running) {
    $running | Select-Object Id, ProcessName | Format-Table -AutoSize
    Fail 'huayang / huayang-mcp is running. Close the processes above and re-run.'
}

$current = (Get-Location).Path
if (Test-HuayangSource $current) {
    $projectDir = $current
    if (Test-Path (Join-Path $projectDir '.git') -PathType Container) {
        Assert-ExpectedOrigin $projectDir
    }
    Step "Using current source directory: $projectDir"
} else {
    Ensure-Git
    if (Test-Path $RepoDir) {
        if (-not (Test-Path (Join-Path $RepoDir '.git') -PathType Container)) {
            Fail "$RepoDir exists but is not a Git checkout. Move it aside and re-run."
        }
        Assert-ExpectedOrigin $RepoDir
        $changes = (& git -C $RepoDir status --porcelain | Out-String).Trim()
        if ($changes) {
            Fail "$RepoDir has local changes. Commit or remove them before upgrading."
        }
        Step "Updating existing checkout at $RepoDir..."
        & git -C $RepoDir pull --ff-only
        if ($LASTEXITCODE -ne 0) { Fail 'git pull --ff-only failed.' }
    } else {
        Step "Cloning $RepoUrl -> $RepoDir"
        New-Item -ItemType Directory -Force -Path (Split-Path $RepoDir) | Out-Null
        & git clone $RepoUrl $RepoDir
        if ($LASTEXITCODE -ne 0) { Fail 'git clone failed.' }
    }
    if (-not (Test-HuayangSource $RepoDir)) {
        Fail 'Downloaded checkout is missing required Huayang files.'
    }
    $projectDir = $RepoDir
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Step 'uv not found; installing uv...'
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    Refresh-Path
    $env:Path = (Join-Path $env:USERPROFILE '.local\bin') + ';' + $env:Path
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Fail 'uv installation completed but uv is not on PATH.'
}
Step "uv $(uv --version)"
$UvBin = (& uv tool dir --bin | Out-String).Trim()
if (-not $UvBin) { Fail 'uv tool dir --bin returned an empty path.' }
New-Item -ItemType Directory -Force -Path $UvBin | Out-Null
$env:Path = "$UvBin;$env:Path"
try { uv tool update-shell | Out-Null } catch { }

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue) -or
    -not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
    Step 'FFmpeg not found; installing via winget...'
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id Gyan.FFmpeg -e --silent --accept-package-agreements --accept-source-agreements
        Refresh-Path
        $env:Path = "$UvBin;$env:Path"
    }
}
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { Fail 'ffmpeg is unavailable.' }
if (-not (Get-Command ffprobe -ErrorAction SilentlyContinue)) { Fail 'ffprobe is unavailable.' }

Push-Location $projectDir
try {
    Step 'Syncing locked Python dependencies...'
    & uv sync
    if ($LASTEXITCODE -ne 0) { Fail 'uv sync failed.' }
    Step 'Installing Playwright Chromium...'
    & uv run playwright install chromium
    if ($LASTEXITCODE -ne 0) { Fail 'Playwright Chromium installation failed.' }
    Step 'Installing Huayang commands...'
    & uv tool install --force --editable .
    if ($LASTEXITCODE -ne 0) { Fail 'uv tool install failed.' }
} finally {
    Pop-Location
}

$HuayangBin = Join-Path $UvBin 'huayang.exe'
$HuayangMcpBin = Join-Path $UvBin 'huayang-mcp.exe'
if (-not (Test-Path $HuayangBin -PathType Leaf)) { Fail "huayang was not installed to $UvBin." }
if (-not (Test-Path $HuayangMcpBin -PathType Leaf)) { Fail "huayang-mcp was not installed to $UvBin." }

Step 'Running installation doctor...'
& $HuayangBin doctor --json
if ($LASTEXITCODE -ne 0) { Fail 'Huayang installation doctor failed.' }

if (Get-Command codex -ErrorAction SilentlyContinue) {
    Step 'Codex detected; installing Huayang Plugin...'
    & $HuayangBin plugin install codex
    if ($LASTEXITCODE -ne 0) { Fail 'Huayang Codex Plugin installation failed.' }
} else {
    Write-Host 'NOTE: Codex was not found. Install it later with: huayang plugin install codex'
}

Write-Host ''
Write-Host 'Installation complete.'
Write-Host '  huayang                         # start admin console at http://127.0.0.1:8788'
Write-Host '  huayang-mcp                     # start MCP server'
Write-Host '  huayang doctor                  # verify runtime'
Write-Host '  huayang plugin install codex    # install/refresh Codex Plugin'
