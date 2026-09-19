# CN mirror overrides for local packaging (sourced by build-macos-cn.sh)
# GitHub Actions 直接跑 build-macos.sh，不受此文件影响。
export npm_config_registry="${npm_config_registry:-https://registry.npmmirror.com}"
export PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
export ELECTRON_MIRROR="${ELECTRON_MIRROR:-https://npmmirror.com/mirrors/electron/}"
export ELECTRON_BUILDER_BINARIES_MIRROR="${ELECTRON_BUILDER_BINARIES_MIRROR:-https://npmmirror.com/mirrors/electron-builder-binaries/}"
