#!/usr/bin/env bash
# lite-work macOS 一键打包脚本：依赖检查 → 前端构建 → PyInstaller 后端 → electron-builder .app → hdiutil DMG
#
# 用法:
#   ./scripts/build-macos.sh            # 默认构建（当前机器架构，增量）
#   ./scripts/build-macos.sh --clean    # 干净发布构建（清 PyInstaller 缓存）
#   ./scripts/build-macos.sh --skip-web # 跳过前端构建（dist 已是最新时）
#   ./scripts/build-macos.sh --skip-deps # 跳过依赖安装检查
#
# 产物: release/lite-work-<版本>-<arch>.dmg
set -euo pipefail

cd "$(dirname "$0")/.."

# ------------------------------------------------------------ 参数
CLEAN=""
SKIP_WEB=0
SKIP_DEPS=0
for arg in "$@"; do
  case "$arg" in
    --clean)      CLEAN="--clean" ;;
    --skip-web)   SKIP_WEB=1 ;;
    --skip-deps)  SKIP_DEPS=1 ;;
    *) echo "未知参数: $arg（支持 --clean / --skip-web / --skip-deps）"; exit 1 ;;
  esac
done

# 彩色步骤输出
step() { printf "\033[1;34m==> %s\033[0m\n" "$1"; }
ok()   { printf "\033[1;32m✔ %s\033[0m\n" "$1"; }

# ------------------------------------------------------------ 依赖检查
if [ "$SKIP_DEPS" -eq 0 ]; then
  step "检查依赖"

  if [ ! -x ".venv/bin/python" ]; then
    echo "缺少 .venv（PyInstaller 打包后端需要）。先执行："
    echo "  python3 -m venv .venv && .venv/bin/pip install -e \".[dev,package]\""
    exit 1
  fi
  ok ".venv"

  if [ ! -d "node_modules" ]; then
    step "安装根目录 npm 依赖"
    npm install
  fi
  ok "node_modules"

  if [ ! -d "web/node_modules" ]; then
    step "安装 web 前端依赖"
    npm --prefix web install
  fi
  ok "web/node_modules"

  # 图标生成依赖 sharp（package.mjs 内部也会跑图标脚本，这里提前暴露缺依赖）
  if ! node -e "require.resolve('sharp')" >/dev/null 2>&1; then
    step "安装图标生成依赖 (sharp)"
    npm install
  fi
  ok "sharp"
fi

# ------------------------------------------------------------ 前端构建
if [ "$SKIP_WEB" -eq 0 ]; then
  step "构建前端 (vite)"
  npm run build:web
  ok "web/dist"
else
  step "跳过前端构建（--skip-web）"
  if [ ! -f "web/dist/index.html" ]; then
    echo "web/dist 不存在，无法跳过前端构建。去掉 --skip-web 重跑。"
    exit 1
  fi
fi

# ------------------------------------------------------------ 完整打包
# package.mjs 内部流程：sync-version → 图标 → PyInstaller 后端 →
# electron-builder --dir（.app）→ hdiutil 手动封装 DMG（规避 electron-builder
# 直接出 DMG 被 macOS 判损坏的问题）；网络受限自动走 npmmirror 镜像
step "打包（node scripts/package.mjs ${CLEAN}）"
node scripts/package.mjs $CLEAN

# node-pty spawn-helper 需要可执行权限（asar 打包可能丢失）；
# 主进程也会运行时修复，此处构建时双保险
APP_DIR="release/mac-arm64/lite-work.app"
PTY_HELPER=$(find "${APP_DIR}/Contents/Resources/app.asar.unpacked/node_modules/node-pty" -name "spawn-helper" 2>/dev/null)
if [ -n "$PTY_HELPER" ]; then
  chmod +x "$PTY_HELPER"
  ok "spawn-helper 权限已修复: $PTY_HELPER"
else
  # asar 内的 prebuilds 路径
  PTY_HELPER=$(find "${APP_DIR}/Contents/Resources" -name "spawn-helper" 2>/dev/null | head -1)
  if [ -n "$PTY_HELPER" ]; then
    chmod +x "$PTY_HELPER"
    ok "spawn-helper 权限已修复: $PTY_HELPER"
  fi
fi

# ------------------------------------------------------------ 结果
DMG=$(ls -t release/lite-work-*.dmg 2>/dev/null | head -1)
if [ -z "$DMG" ]; then
  echo "❌ 未找到产物 DMG，请检查上方输出"
  exit 1
fi
ok "产物: $DMG ($(du -h "$DMG" | cut -f1))"
echo
echo "安装提示：macOS 包未签名。首次打开如提示「无法验证开发者」，右键应用 →「打开」；"
echo "如提示「已损坏」：xattr -dr com.apple.quarantine \"/Applications/lite-work.app\""
