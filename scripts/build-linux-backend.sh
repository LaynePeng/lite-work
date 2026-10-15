#!/usr/bin/env bash
# lite-work Linux 后端打包入口：前端构建 → PyInstaller --onedir → tar.gz 分发归档。
#
# 只产**后端**（不含 Electron 桌面壳、不含 DMG/AppImage/deb）：解包即
#   ./lite-work-backend/lite-work-backend serve --host 0.0.0.0 --port 8787 --token xxx \
#     --workspace /data/workspace --config-dir /data/.lite-work
# 裸机 / 已有容器直跑；容器镜像走 scripts/build-linux-image.sh（源码构建）。
#
# 用法:
#   ./scripts/build-linux-backend.sh               # 增量构建（复用 PyInstaller 分析缓存）
#   ./scripts/build-linux-backend.sh --clean       # 干净发布构建（清 PyInstaller 缓存）
#   ./scripts/build-linux-backend.sh --skip-web    # 跳过前端构建（web/dist 已最新时）
#   ./scripts/build-linux-backend.sh --skip-deps   # 跳过依赖检查与安装
#   ./scripts/build-linux-backend.sh --skip-build  # 复用已有 release/backend，只重打包归档
#   ./scripts/build-linux-backend.sh --dry-run     # 只打印计划与依赖就绪情况，不做构建
#
# 产物: release/lite-work-backend-<版本>-linux-<arch>.tar.gz（含同名 .sha256）
set -euo pipefail

cd "$(dirname "$0")/.."

# ------------------------------------------------------------ 参数
CLEAN=""
SKIP_WEB=0
SKIP_DEPS=0
SKIP_BUILD=0
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --clean)       CLEAN="--clean" ;;
    --skip-web)    SKIP_WEB=1 ;;
    --skip-deps)   SKIP_DEPS=1 ;;
    --skip-build)  SKIP_BUILD=1 ;;
    --dry-run)     DRY_RUN=1 ;;
    *) echo "未知参数: $arg（支持 --clean / --skip-web / --skip-deps / --skip-build / --dry-run）"; exit 1 ;;
  esac
done

step() { printf "\033[1;34m==> %s\033[0m\n" "$1"; }
ok()   { printf "\033[1;32m✔ %s\033[0m\n" "$1"; }
die()  { printf "\033[1;31m❌ %s\033[0m\n" "$1"; exit 1; }

# ------------------------------------------------------------ 平台与架构
KERNEL="$(uname -s)"
MACHINE="$(uname -m)"
case "$MACHINE" in
  x86_64|amd64)  ARCH="x64" ;;
  aarch64|arm64) ARCH="arm64" ;;
  *)             ARCH="$MACHINE" ;;
esac

VERSION="$(node -p "require('./package.json').version")"
ARCHIVE_NAME="lite-work-backend-${VERSION}-linux-${ARCH}.tar.gz"

# ------------------------------------------------------------ dry-run
# 放在平台闸门之前：dry-run 只报告计划，任何平台都该能跑（用于核对参数与产物名）。
if [ "$DRY_RUN" -eq 1 ]; then
  step "dry-run：仅报告，不执行构建"
  echo "  内核 / 架构 : $KERNEL / $MACHINE → linux-${ARCH}"
  echo "  版本        : $VERSION"
  echo "  产物        : release/${ARCHIVE_NAME}"
  for probe in ".venv/bin/python" "web/dist/index.html" "node_modules" "web/node_modules"; do
    if [ -e "$probe" ]; then echo "  ✔ $probe"; else echo "  ✗ $probe（缺失，正式构建会先安装/构建）"; fi
  done
  exit 0
fi

# ------------------------------------------------------------ 平台闸门
# 非 Linux 上 PyInstaller 产出的是本机二进制，默认拒绝；冒烟检查用
# LITEWORK_ALLOW_FOREIGN_PLATFORM=1 显式放行。
if [ "$KERNEL" != "Linux" ] && [ "${LITEWORK_ALLOW_FOREIGN_PLATFORM:-0}" != "1" ]; then
  die "本入口只在 Linux 上产出 Linux 后端（当前内核: $KERNEL）。
   只做冒烟检查请加 LITEWORK_ALLOW_FOREIGN_PLATFORM=1（此时产物不可用于 Linux）。"
fi

# ------------------------------------------------------------ 依赖检查
if [ "$SKIP_DEPS" -eq 0 ]; then
  step "检查依赖"

  if [ ! -x ".venv/bin/python" ]; then
    echo "缺少 .venv（PyInstaller 打包后端需要）。先执行："
    echo "  python3 -m venv .venv && .venv/bin/pip install -e \".[package]\""
    exit 1
  fi
  ok ".venv"

  # 只装前端依赖：根 node_modules 只为 Electron 打包服务，这里省一次
  # 200MB+ 的 Electron 下载；技能引擎由 package-backend.mjs 自己装。
  if [ ! -d "web/node_modules" ]; then
    step "安装 web 前端依赖"
    npm --prefix web ci 2>/dev/null || npm --prefix web install
  fi
  ok "web/node_modules"
fi

# ------------------------------------------------------------ 版本同步
step "同步版本号（litework/__init__.py → npm 侧）"
node scripts/sync-version.mjs
VERSION="$(node -p "require('./package.json').version")"

# ------------------------------------------------------------ 前端构建
# 后端自带 Web UI（server/app.py 从 _internal/web/dist 提供静态资源），因此必须有 dist。
if [ "$SKIP_BUILD" -eq 1 ]; then
  step "跳过前端构建与 PyInstaller（--skip-build：复用已有 release/backend）"
elif [ "$SKIP_WEB" -eq 0 ]; then
  step "构建前端 (vite)"
  npm run build:web
  ok "web/dist"
else
  step "跳过前端构建（--skip-web）"
  [ -f "web/dist/index.html" ] || die "web/dist 不存在，无法跳过前端构建。去掉 --skip-web 重跑。"
fi

# ------------------------------------------------------------ PyInstaller
if [ "$SKIP_BUILD" -eq 0 ]; then
  step "打包后端（node scripts/package-backend.mjs ${CLEAN}）"
  node scripts/package-backend.mjs $CLEAN
fi

# ------------------------------------------------------------ 产物校验
BIN="release/backend/lite-work-backend/lite-work-backend"
[ -x "$BIN" ] || die "未生成可执行文件: $BIN（--skip-build 时请先跑过一次完整构建）"

step "检查随包资源完整性"
for probe in "_internal/skills/diagram-to-office/SKILL.md" \
             "_internal/litework/builtin_plugins/pricing-plugin/pricing.json" \
             "_internal/web/dist/index.html"; do
  [ -e "release/backend/lite-work-backend/$probe" ] || die "缺随包资源: $probe"
done
ok "skills / builtin_plugins 伴生文件 / web dist"

step "冒烟检查（--version）"
"$BIN" --version

# ------------------------------------------------------------ 归档
step "归档 release/${ARCHIVE_NAME}"
rm -f "release/${ARCHIVE_NAME}" "release/${ARCHIVE_NAME}.sha256"
tar -czf "release/${ARCHIVE_NAME}" -C release/backend lite-work-backend

step "生成校验和"
(
  cd release
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "${ARCHIVE_NAME}" > "${ARCHIVE_NAME}.sha256"
  else
    shasum -a 256 "${ARCHIVE_NAME}" > "${ARCHIVE_NAME}.sha256"
  fi
)
ok "$(cat "release/${ARCHIVE_NAME}.sha256")"

# ------------------------------------------------------------ 结果
ok "产物: release/${ARCHIVE_NAME} ($(du -h "release/${ARCHIVE_NAME}" | cut -f1))"
echo
echo "目标机使用："
echo "  tar -xzf ${ARCHIVE_NAME}"
echo "  cd lite-work-backend && ./lite-work-backend serve --host 0.0.0.0 --port 8787 --token <令牌> \\"
echo "    --workspace /data/workspace --config-dir /data/.lite-work"
echo "  依赖 glibc 构建基线：$(ldd --version 2>/dev/null | head -1 || echo '未知')"
echo "  提示：LibreOffice（office 格式转换）、ripgrep（search_code）、Chrome（技能出图）、"
echo "        fonts-noto-cjk（图表中文）为可选外部程序，缺失时对应工具返回安装指引。"
