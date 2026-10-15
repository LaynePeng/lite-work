#!/usr/bin/env bash
# lite-work Linux 容器镜像构建入口：源码构建（deploy/Dockerfile）。
# 与「Linux 仅后端 tar.gz」的分工：裸机/已有容器 → build-linux-backend.sh；
# 服务器/NAS/云主机 → 本脚本（容器镜像）。
#
# 用法（在目标架构本机执行）：
#   ./scripts/build-linux-image.sh                  # 构建并打本机架构标签
#   ./scripts/build-linux-image.sh --tag my:tag     # 自定义镜像标签（可重复）
#   ./scripts/build-linux-image.sh --push           # 构建后推送
#
# 产物: 本地镜像 lite-work-core:<版本>-linux-<arch>
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

TAGS=()
PUSH=0
while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAGS+=("${2:-}"); [ -n "${2:-}" ] || { echo "--tag 需要值"; exit 1; }; shift 2 ;;
    --push) PUSH=1; shift ;;
    *) echo "未知参数: $1（支持 --tag <name> / --push）"; exit 1 ;;
  esac
done

step() { printf "\033[1;34m==> %s\033[0m\n" "$1"; }
ok()   { printf "\033[1;32m✔ %s\033[0m\n" "$1"; }
die()  { printf "\033[1;31m❌ %s\033[0m\n" "$1"; exit 1; }

command -v docker >/dev/null 2>&1 || die "未找到 docker（本入口需本机 Docker）"

case "$(uname -m)" in
  x86_64|amd64)  ARCH="x64" ;;
  aarch64|arm64) ARCH="arm64" ;;
  *)             ARCH="$(uname -m)" ;;
esac

VERSION="$(node -p "require('./package.json').version")"

if [ "${#TAGS[@]}" -eq 0 ]; then
  TAGS=("lite-work-core:${VERSION}-linux-${ARCH}")
fi

step "源码构建镜像（deploy/Dockerfile）"
echo "  标签: ${TAGS[*]}"
echo "  平台: linux/${ARCH}（本机架构，首次构建约 10-20 分钟）"

for t in "${TAGS[@]}"; do
  build_args+=(-t "$t")
done
docker build -f deploy/Dockerfile "${build_args[@]}" "$ROOT"

step "镜像体检（关键外部程序 + 服务可用性）"
PRIMARY="${TAGS[0]}"
docker run --rm --entrypoint /bin/sh "$PRIMARY" -c '
  for c in rg git soffice curl; do
    printf "  %-8s: %s\n" "$c" "$(command -v "$c" || echo 缺失)"
  done
  printf "  %-8s: %s\n" "skills" "$(ls /app/skills 2>/dev/null | wc -l) 个技能目录"
'

if [ "$PUSH" -eq 1 ]; then
  for t in "${TAGS[@]}"; do
    step "推送 $t"
    docker push "$t"
  done
fi

SIZE="$(docker image inspect "$PRIMARY" --format '{{.Size}}' 2>/dev/null || echo 0)"
ok "产物镜像: $PRIMARY（约 $((SIZE / 1024 / 1024)) MB）"
echo
echo "运行（同 compose 的安全边界：端口默认只绑回环，公网必须走反代 + TLS）："
echo "  docker run -d --name lite-work-core -p 127.0.0.1:8787:8787 \\"
echo "    -v lite-work-data:/data --shm-size=1g --memory=3500m $PRIMARY"
echo "  docker logs lite-work-core | grep 访问令牌"
