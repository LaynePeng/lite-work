# lite-work Windows packaging script (PowerShell): frontend build -> PyInstaller backend -> NSIS installer
# Usage: .\scripts\build-windows.ps1 [-Insecure] [-Clean]
#   -Insecure  Skip TLS certificate validation (only for intranet/self-signed cert environments)
#   -Clean     Clear PyInstaller analysis cache (use for a clean release build)
#
# Prerequisites:
#   - Python 3.11+ (available as `python` on PATH)
#   - Node.js 18+
#   - First run needs network to download the Electron binary (npmmirror mirror is used by default in CN)
param([switch]$Insecure, [switch]$Clean)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

# Restricted network: default to CN mirrors (overridable via environment variables)
if (-not $env:ELECTRON_MIRROR) { $env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/" }
if (-not $env:ELECTRON_BUILDER_BINARIES_MIRROR) { $env:ELECTRON_BUILDER_BINARIES_MIRROR = "https://npmmirror.com/mirrors/electron-builder-binaries/" }

# 无签名证书：禁用签名发现，跳过 signtool 调用（未签名发布，行为同前）
if (-not $env:CSC_IDENTITY_AUTO_DISCOVERY) { $env:CSC_IDENTITY_AUTO_DISCOVERY = "false" }

if ($Insecure) {
    Write-Host "==> Non-strict TLS validation enabled (intranet/self-signed only)"
    $env:NODE_TLS_REJECT_UNAUTHORIZED = "0"
    $env:npm_config_strict_ssl = "false"
}

function Invoke-Step([string]$Name, [scriptblock]$Block) {
    Write-Host "==> $Name"
    $global:LASTEXITCODE = 0
    $timer = [Diagnostics.Stopwatch]::StartNew()
    # PS 5.1 下原生命令的 stderr 在 ErrorActionPreference=Stop 时会变成致命错误
    # （例如 pip 的升级提示），这里仅在步骤内临时降级，成功与否只看退出码
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Block
    } finally {
        $ErrorActionPreference = $prevEap
        $timer.Stop()
        Write-Host ("    elapsed: " + $timer.Elapsed.ToString("hh\:mm\:ss\.f"))
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "!! $Name failed (exit $LASTEXITCODE)" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

# Version single source of truth: sync npm-side version fields from litework/__init__.py
Invoke-Step "Sync version from litework/__init__.py" {
    node scripts/sync-version.mjs
}

# ------------------------------------------------------------ Python environment

$venvPython = Join-Path $PWD ".venv\Scripts\python.exe"
$venvPip = Join-Path $PWD ".venv\Scripts\pip.exe"

Invoke-Step "Prepare Python virtual environment" {
    if (-not (Test-Path $venvPython)) {
        python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw "Failed to create venv. Please install Python 3.11+" }
    }
}

# 加速策略：依赖清单未变化时复用现有 venv，避免每次重新解析/安装原生依赖。
# 依赖首次安装或发生变化时使用标准 build isolation；全新 runner 没有构建工具，
# 强制 --no-build-isolation 会导致原生包安装失败。
$pythonFingerprint = (Get-FileHash (Join-Path $PWD "pyproject.toml") -Algorithm SHA256).Hash
$pythonMarker = Join-Path $PWD ".venv\.lite-work-deps.sha256"
$pythonDepsChanged = $true
if (Test-Path $pythonMarker) {
    $pythonDepsChanged = ((Get-Content $pythonMarker -Raw).Trim() -ne $pythonFingerprint)
}
if ($pythonDepsChanged) {
    Invoke-Step "Install Python dependencies (.venv\Scripts\pip install -e .[dev,package])" {
        & $venvPip install -e ".[dev,package]"
        if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
        Set-Content -Path $pythonMarker -Value $pythonFingerprint -NoNewline
    }
} else {
    Write-Host "==> Python dependencies unchanged, skip pip install"
}

# ------------------------------------------------------------ Frontend dependencies

# 有 package-lock.json 时用 npm ci（严格按锁文件，比 npm install 快且可复现）
$npmArgs = "install"
if (Test-Path package-lock.json) { $npmArgs = "ci" }
if (-not (Test-Path node_modules)) {
    Invoke-Step "Install root dependencies (npm $npmArgs)" {
        npm $npmArgs
    }
}

$webNpmArgs = "install"
if (Test-Path web\package-lock.json) { $webNpmArgs = "ci" }
if (-not (Test-Path web\node_modules)) {
    Invoke-Step "Install frontend dependencies (npm --prefix web $webNpmArgs)" {
        npm --prefix web $webNpmArgs
    }
}

Invoke-Step "Build frontend (npm run build:web)" {
    npm run build:web
}

# ------------------------------------------------------------ Backend binary

Invoke-Step "Package Python backend (PyInstaller)" {
    if ($Clean) {
        node scripts/package-backend.mjs --clean
    } else {
        node scripts/package-backend.mjs
    }
}

# ------------------------------------------------------------ electron-builder NSIS

Invoke-Step "Package Windows installer (electron-builder --win nsis)" {
    npx electron-builder --win nsis --publish never
}

Write-Host "==> Done:"
Get-ChildItem release -Filter *.exe | ForEach-Object {
    Write-Host ("    " + $_.Name + " (" + [math]::Round($_.Length / 1MB, 1) + " MB)")
}
