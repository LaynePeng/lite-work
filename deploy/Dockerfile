# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors
#
# lite-work Core 云端镜像（x86-64，Ubuntu 24.04 noble 基底）。
# 多阶段：node 构建前端 → playwright/python 基底装运行层。
#
# 版本纪律：基底镜像 tag 与 pip 的 playwright 必须**同版本**，否则
# Playwright 找不到浏览器可执行文件。升级时只改 PLAYWRIGHT_VERSION，
# 两者会一起变。

ARG PLAYWRIGHT_VERSION=1.63.0

# ---- 前端构建 ----
FROM node:22-slim AS web
WORKDIR /build
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# ---- 运行层 ----
FROM mcr.microsoft.com/playwright/python:v${PLAYWRIGHT_VERSION}-noble
ARG PLAYWRIGHT_VERSION

ENV PYTHONUNBUFFERED=1 \
    LITEWORK_WEB_DIST=/opt/lite-work/web-dist \
    MPLCONFIGDIR=/data/.lite-work/mpl

# git：Agent 的 git 工具；libreoffice：office 转换/重算；fonts-noto-cjk：图表中文；
# libgl1 / libglib2.0-0t64 / libgomp1：cv2 / onnxruntime 的 .so；curl：healthcheck。
# ripgrep 在 noble 的 universe 源，fail-soft 安装。
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        git libreoffice fonts-noto-cjk \
        libgl1 libglib2.0-0t64 libgomp1 \
        curl ca-certificates; \
    apt-get install -y --no-install-recommends ripgrep \
      || echo "warn: ripgrep 安装失败——search_code 将不可用（其余功能不受影响）" >&2; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY litework ./litework
# 不 COPY skills 则容器内技能列表为空（_builtin_skills_dir() 候选 /app/skills）
COPY skills ./skills
# 先锁 playwright（与基底镜像同版本），再装本包：其 playwright>=1.40 约束
# 已被满足，pip 不会拉别的版本
RUN pip install --no-cache-dir "playwright==${PLAYWRIGHT_VERSION}" .

COPY --from=web /build/dist /opt/lite-work/web-dist
COPY deploy/docker-entrypoint.sh /usr/local/bin/lite-work-entrypoint
RUN chmod +x /usr/local/bin/lite-work-entrypoint

# /data：会话/插件/技能/日志/工作区（compose 挂卷持久化）
VOLUME /data
EXPOSE 8787
ENTRYPOINT ["lite-work-entrypoint"]
