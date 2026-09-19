# CN mirror overrides for local packaging (sourced by build-windows-cn.ps1)
# GitHub Actions 直接跑 build-windows.ps1，不受此文件影响。
if (-not $env:npm_config_registry) { $env:npm_config_registry = "https://registry.npmmirror.com" }
if (-not $env:PIP_INDEX_URL) { $env:PIP_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple" }
if (-not $env:ELECTRON_MIRROR) { $env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/" }
if (-not $env:ELECTRON_BUILDER_BINARIES_MIRROR) { $env:ELECTRON_BUILDER_BINARIES_MIRROR = "https://npmmirror.com/mirrors/electron-builder-binaries/" }
