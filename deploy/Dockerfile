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

# git：Agent 的 git_commit / worktree；libreoffice：office 工具的格式转换
# （soffice）；fonts-noto-cjk：matplotlib 图表中文兜底（office.py 自带内嵌
# 字体，这里只兜 findfont 系统路径）；curl：compose healthcheck
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git libreoffice fonts-noto-cjk curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY litework ./litework
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
