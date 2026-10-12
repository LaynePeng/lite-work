# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""代理配置（出网分流）。

lite-work 的出网面分六类（scope），可分别选择是否走代理：
  llm       LLM API 调用（anthropic / openai_compat / deepseek 适配器）
  webfetch  Agent 联网工具（webfetch / webfetch_batch）
  meta      models.dev 模型元数据 / 定价数据刷新
  market    社区插件/技能市场（检查更新、下载）
  mcp       MCP 子进程（stdio：以环境变量 HTTP(S)_PROXY 注入）
  shell     Agent 执行的命令（execute_command / 打包脚本：同样 env 注入）

URL 普适性归一化（用户手填格式五花八门，这里统一收口）：
  http://user:pass@host:port     标准格式
  https://user:pass@host:port    HTTPS 代理（隧道）
  socks5://user:pass@host:port   SOCKS5（httpx 需 socksio；未装则该 scope 退回直连并记日志）
  user:pass@https://host:port    凭据在前、协议在后（常见手写习惯）
  user:pass@host:port            无协议 → 按 http 处理
  host:port                      无凭据无协议
  http://host:port               无凭据
归一化输出 httpx 可用的 proxy URL（scheme://user:pass@host:port）。

bypass：不走代理的 host 列表（如内网自建 LLM、公司镜像源）——
对 httpx 场景转化为「命中的请求直接连」（httpx 不支持按 host 的
proxy 例外表，用 Mounts 思路过重；轻量实现：命中 bypass 的场景
不传 proxy）。子进程场景转化为 NO_PROXY env。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger("litework.proxy")

#: 全部出网 scope（配置校验 + 设置页分组展示用）
PROXY_SCOPES = ("llm", "webfetch", "meta", "market", "mcp", "shell")

#: scope 的中文说明（设置页透出用）
SCOPE_LABELS = {
    "llm": "LLM API 调用",
    "webfetch": "Agent 联网工具（webfetch）",
    "meta": "models.dev 模型元数据",
    "market": "插件/技能市场（检查更新）",
    "mcp": "MCP 子进程",
    "shell": "Agent 执行的命令",
}

_SCHEME_RE = re.compile(r"^(https?|socks5)://", re.IGNORECASE)


def normalize_proxy_url(raw: str) -> Optional[str]:
    """任意常见写法 → httpx/子进程 env 通用代理 URL；解析失败返回 None。

    支持格式见模块 docstring。凭据在前（user:pass@https://host）的写法
    会被重排为标准 scheme://user:pass@host。
    """
    s = (raw or "").strip()
    if not s:
        return None
    # 已带协议：只做小写归一 + 校验 host
    if _SCHEME_RE.match(s):
        p = urlparse(s)
        if not p.hostname:
            return None
        # https 代理对 httpx 是合法 scheme；socks5 需要额外依赖，保留原样
        # 由 proxy_for 在运行时探测可用性
        return s
    # 无协议：可能是 user:pass@host:port 或 host:port，也可能是
    # user:pass@https://host:port（协议被写在 @ 之后）
    if "@" in s:
        cred, _, rest = s.partition("@")
        rest = rest.strip()
        if _SCHEME_RE.match(rest):
            # 凭据在前、协议在后：重排
            p = urlparse(rest)
            if not p.hostname:
                return None
            return f"{p.scheme.lower()}://{cred}@{p.netloc}"
        if rest and not rest.startswith("/"):
            return f"http://{cred}@{rest}"
        return None
    # 纯 host:port
    if re.match(r"^[\w.\-]+:\d+$", s):
        return f"http://{s}"
    return None


def proxy_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """读取归一化后的代理配置段（缺省全空）。"""
    raw = config.get("proxy") or {}
    if not isinstance(raw, dict):
        return {}
    return raw


def proxy_for(config: Dict[str, Any], scope: str) -> Optional[str]:
    """某出网 scope 是否走代理；走则返回归一化代理 URL，否则 None。

    httpx 调用方：`httpx.AsyncClient(proxy=proxy_for(cfg, "llm"))`——
    proxy=None 即直连（httpx 语义），调用处无需 if 分支。
    """
    pc = proxy_config(config)
    if not pc.get("enabled"):
        return None
    scopes = pc.get("scope") or []
    if not isinstance(scopes, list) or scope not in scopes:
        return None
    url = normalize_proxy_url(str(pc.get("url") or ""))
    if not url:
        return None
    scheme = urlparse(url).scheme.lower()
    if scheme == "socks5":
        # socks5 需要 httpx[socks]（socksio）。探测一次，缺依赖退回直连。
        try:
            import socksio  # noqa: F401
        except ImportError:
            logger.warning("[Proxy] socks5 需要 socksio 依赖（pip install httpx[socks]），该 scope 退回直连: %s", scope)
            return None
    return url


def proxy_env(config: Dict[str, Any], scope: str) -> Dict[str, str]:
    """子进程（MCP / shell）的代理环境变量注入。

    返回 {"HTTP_PROXY": ..., "HTTPS_PROXY": ..., "NO_PROXY": ...}；
    未启用/该 scope 不走代理返回 {}（调用方 merge 进子进程 env）。
    bypass 列表转 NO_PROXY（curl/npm/pip 等全都认这个变量）。
    """
    url = proxy_for(config, scope)
    if not url:
        return {}
    env: Dict[str, str] = {
        "HTTP_PROXY": url,
        "HTTPS_PROXY": url,
        # 小写变体：Python requests/httpx 与部分工具只认小写
        "http_proxy": url,
        "https_proxy": url,
    }
    bypass = proxy_config(config).get("bypass")
    if isinstance(bypass, list):
        hosts = [str(h).strip() for h in bypass if str(h).strip()]
        if hosts:
            no_proxy = ",".join(hosts)
            env["NO_PROXY"] = no_proxy
            env["no_proxy"] = no_proxy
    return env
