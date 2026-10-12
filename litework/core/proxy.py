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

    凭据特殊字符（# @ : / 等密码常见字符）统一 percent-encode——# 是
    URL fragment 分隔符，不编码会被 httpx 拆成 host=user/port=pass 直接
    不可用（Invalid port）。
    """
    s = (raw or "").strip()
    if not s:
        return None
    from urllib.parse import quote

    def _enc_cred(cred: str) -> str:
        """user:pass 段安全编码：只编码 URL 保留字符，字母数字与常见符号保留。
        分隔 user 与 pass 的第一个冒号保留（userinfo 语法位），其余全编。"""
        user, sep, pw = cred.partition(":")
        if not sep:
            return quote(cred, safe=".-_~!$&'()*+,;=")
        return f"{quote(user, safe='.-_~!$&()*+,;=')}:{quote(pw, safe='.-_~!$&()*+,;=')}"

    # 已带协议：先按「第一个 @」手工拆凭据段（URL 解析会按 # 截断密码，
    # 如 http://user:pass#word@host 的 password 只剩 pass——必须拿原始段编码），
    # 再校验 host（无凭据部分交给 urlparse 解析）
    if _SCHEME_RE.match(s):
        # 拆法：scheme://cred@host——cred 从 :// 后到第一个 @（编码前原样；
        # 不走 URL 解析取凭据，那会把 # 后当 fragment 截断密码）
        scheme_part, sep_scheme, after_scheme = s.partition("://")
        if sep_scheme and "@" in after_scheme:
            # 最后一个 @ 才是 userinfo/host 边界（URL 规范：userinfo 里的 @ 必须编码）
            raw_cred_seg, sep, host_seg = after_scheme.rpartition("@")
            if not sep or not host_seg:
                return None
            # host 校验（凭据已拆走；尾随 path/fragment 由 urlparse 剥离）
            hp = urlparse(f"http://{host_seg}")
            if not hp.hostname:
                return None
            return f"{scheme_part.lower()}://{_enc_cred(raw_cred_seg)}@{hp.netloc}"
        p = urlparse(s)
        if not p.hostname:
            return None
        return f"{p.scheme.lower()}://{p.netloc}"
    # 无协议：可能是 user:pass@host:port 或 host:port，也可能是
    # user:pass@https://host:port（协议被写在 @ 之后）
    if "@" in s:
        # 最后一个 @ 才是凭据/host 边界（密码里裸写 @ 的场景按规范本该编码，
        # 但 rpartition 至少让 host 部分总是对的）
        cred, _, rest = s.rpartition("@")
        rest = rest.strip()
        if _SCHEME_RE.match(rest):
            # 凭据在前、协议在后：重排 + 编码
            p = urlparse(rest)
            if not p.hostname:
                return None
            return f"{p.scheme.lower()}://{_enc_cred(cred)}@{p.netloc}"
        if rest and not rest.startswith("/") and re.match(r"^[\w.\-]+:\d+$", rest):
            return f"http://{_enc_cred(cred)}@{rest}"
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


def proxy_mounts(config: Dict[str, Any], scope: str) -> Optional[Dict[str, Any]]:
    """httpx 场景的 bypass 分流（Bug 修复：bypass 此前只在 env 注入生效，
    LLM/webfetch/meta 的 httpx client 整体走代理，直连列表形同虚设）。

    返回 httpx 的 mounts 参数（官方按 scheme://host 路由的机制）：
      - bypass 命中的 host → 显式 None（直连）
      - 其余全部流量（all://）→ 代理
    未启用代理 / scope 未勾选 → None（调用方不传 mounts，行为不变）。
    """
    url = proxy_for(config, scope)
    if not url:
        return None
    bypass = proxy_config(config).get("bypass")
    if not isinstance(bypass, list):
        return None
    mounts: Dict[str, Any] = {"all://": url}
    for h in bypass:
        host = str(h).strip()
        if not host:
            continue
        # 支持 user 写通配（*.example.com）与裸域名两种形式：
        # httpx mounts 的 key 是 scheme://host 通配模式
        if host.startswith("*."):
            mounts[f"all://{host[2:]}"] = None
        else:
            mounts[f"all://{host}"] = None
    return mounts


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
