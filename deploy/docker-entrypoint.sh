#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors
#
# lite-work Core 容器入口：令牌持久化 + 预热 + 启动 serve。
# 令牌策略：LITEWORK_TOKEN 显式指定优先；否则首启生成一次、写入数据卷
# （重启不变，客户端无需重新配对）。core 的 serve 启动横幅会打印令牌，
# docker logs 可查。
set -e

CONFIG_DIR="${LITEWORK_CONFIG_DIR:-/data/.lite-work}"
WORKSPACE="${LITEWORK_WORKSPACE:-/data/workspace}"
PORT="${LITEWORK_PORT:-8787}"
TOKEN_FILE="$CONFIG_DIR/server-token"

mkdir -p "$CONFIG_DIR" "$WORKSPACE"

# 首启播种云端默认配置（卷内无 config.json 时才写，不覆盖用户后续改动）：
# 4GB 小内存机的顶层任务护栏；桌面版默认 0=不限
if [ ! -f "$CONFIG_DIR/config.json" ]; then
  echo '{"max_running_tasks": 4}' > "$CONFIG_DIR/config.json"
fi

if [ -n "$LITEWORK_TOKEN" ]; then
  TOKEN="$LITEWORK_TOKEN"
else
  if [ ! -s "$TOKEN_FILE" ]; then
    python3 -c "import secrets; print(secrets.token_urlsafe(24))" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
  fi
  TOKEN="$(cat "$TOKEN_FILE")"
fi

# 预热 matplotlib 字体缓存（装了 fonts-noto-cjk 后首建较慢，缓存放数据卷）；
# fail-open：失败不阻断启动，首次画图时会在后台重建
lite-work warmup || echo "warn: warmup 失败（不阻断启动）" >&2

exec lite-work serve --host 0.0.0.0 --port "$PORT" --token "$TOKEN" \
  --workspace "$WORKSPACE" --config-dir "$CONFIG_DIR" "$@"
