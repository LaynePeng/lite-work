# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""Web 抓取工具（对标 OpenCode 的 webfetch 工具）。

解决 Agent 缺少联网能力时凭记忆/臆测回答外部信息的问题：
- 只允许 http/https（阻止 file:// 等本地协议读取本地文件）
- SSRF 防护：解析主机名后拒绝回环 / 私网 / 链路本地 / 保留地址
- HTML → Markdown 轻量转换（纯标准库，不依赖 bs4），超长输出截断
- 磁盘缓存：命中 TTL 内的抓取结果直接返回，避免重复请求
- webfetch_batch：并发批量抓取多个 URL，单页失败不影响其余页面
- 反爬三级对抗：① 浏览器指纹池（真实 UA + 同族导航头）轮换请求；
  ② 403 时自动换指纹重试；③ curl_cffi 以 libcurl-impersonate 模拟真实
  Chrome TLS 握手（对抗 JA3 指纹检测）终极兜底。仍被拦截说明站点需要
  执行 JS 质询（Cloudflare 严格模式），返回可操作提示引导换源/用浏览器
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import logging
import os
import random
import re
import socket
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from ..core.types import ToolDefinition

logger = logging.getLogger("litework.tools")

# TLS 指纹级兜底（可选依赖：缺 curl_cffi 时自动跳过该层）
try:
    from curl_cffi.requests import AsyncSession as _CurlAsyncSession

    _HAS_CURL_CFFI = True
except ImportError:
    _CurlAsyncSession = None  # type: ignore[assignment]
    _HAS_CURL_CFFI = False

MAX_READ_BYTES = 2 * 1024 * 1024  # 最多读取 2MB
DEFAULT_MAX_CHARS = 12_000
TIMEOUT = 15.0
CACHE_TTL = 3600  # 缓存有效期：1 小时
MAX_BATCH_URLS = 8  # 单次批量抓取上限
BATCH_CONCURRENCY = 4  # 批量并发数（温和限速）

# ---------------------------------------------------------------- 浏览器指纹池
# 真实浏览器导航请求的完整头组合（UA 与 Sec-Ch-Ua 必须同族，否则反而更像机器人）。
# 裸 UA 或自报家门的 agent 名会被 Cloudflare 等反爬直接 403。
# 注意：不设置 Accept-Encoding——httpx 按已安装解码器自动协商，虚报 br/zstd 会解码失败。

_NAV_COMMON = {
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",   # none + User=?1 = 地址栏直接导航（最无嫌疑的请求形态）
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

BROWSER_PROFILES: List[Dict[str, str]] = [
    {  # Chrome 131 / Windows
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,image/apng,*/*;q=0.8,"
                   "application/signed-exchange;v=b3;q=0.7"),
        "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        **_NAV_COMMON,
    },
    {  # Chrome 131 / macOS
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,image/apng,*/*;q=0.8,"
                   "application/signed-exchange;v=b3;q=0.7"),
        "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        **_NAV_COMMON,
    },
    {  # Firefox 133 / Windows
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8"),
        **_NAV_COMMON,
    },
    {  # Safari 17.6 / macOS
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                       "(KHTML, like Gecko) Version/17.6 Safari/605.1.15"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        **{k: v for k, v in _NAV_COMMON.items() if k != "Sec-Fetch-User"},
    },
]

_SKIP_TAGS = re.compile(
    r"<(script|style|noscript|svg|template|head)\b[^>]*>.*?</\1\s*>", re.I | re.S
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.I | re.S)
_HEADING_RE = re.compile(r"<(h[1-6])[^>]*>(.*?)</\1\s*>", re.I | re.S)
_LINK_RE = re.compile(r"<a\s+[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a\s*>", re.I | re.S)
_IMG_RE = re.compile(r"<img\s+[^>]*alt=[\"']([^\"']*)[\"'][^>]*/?>", re.I)
_CODE_BLOCK_RE = re.compile(r"<pre[^>]*>(.*?)</pre\s*>", re.I | re.S)
_CODE_INLINE_RE = re.compile(r"<code[^>]*>(.*?)</code\s*>", re.I | re.S)
_STRONG_RE = re.compile(r"<(strong|b)\s*>(.*?)</\1\s*>", re.I | re.S)
_EM_RE = re.compile(r"<(em|i)\s*>(.*?)</\1\s*>", re.I | re.S)
_TABLE_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr\s*>", re.I | re.S)
_TABLE_CELL_RE = re.compile(r"<(t[hd])[^>]*>(.*?)</\1\s*>", re.I | re.S)
_LI_RE = re.compile(r"<li[^>]*>(.*?)</li\s*>", re.I | re.S)
_BLOCKQUOTE_RE = re.compile(r"<blockquote[^>]*>(.*?)</blockquote\s*>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")

_BLOCK_TAGS = [
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "section", "article",
    "ul", "ol", "li", "pre", "blockquote", "table", "tr", "hr", "br",
    "form", "header", "footer", "nav", "aside", "main", "details", "summary",
]


def _strip_tags(text: str) -> str:
    return _TAG_RE.sub("", text)


class WebFetchTools:
    def __init__(
        self,
        client_factory: Optional[Callable[[], httpx.AsyncClient]] = None,
        cache_dir: Optional[str] = None,
        cache_ttl: float = CACHE_TTL,
    ) -> None:
        self._profile_turn = random.randrange(len(BROWSER_PROFILES))
        self._client_factory = client_factory or self._default_client_factory
        self._cache_dir = cache_dir
        self._cache_ttl = cache_ttl

    def _default_client_factory(self) -> httpx.AsyncClient:
        """轮换浏览器指纹创建客户端：批量抓取时各请求指纹不同，
        降低单指纹被风控的概率。"""
        profile = BROWSER_PROFILES[self._profile_turn % len(BROWSER_PROFILES)]
        self._profile_turn += 1
        return httpx.AsyncClient(
            follow_redirects=True,
            timeout=TIMEOUT,
            headers=dict(profile),
        )

    # ------------------------------------------------------------ 工具定义

    def get_tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="webfetch",
                description=(
                    "抓取指定 URL 的网页/文本内容并转换为 Markdown 返回"
                    "（仅支持 http/https，超长输出自动截断，结果带磁盘缓存）。"
                    "用于查证外部文档、API 规范、最新信息，避免凭空臆测。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "要抓取的完整 URL（http/https）"},
                        "maxChars": {"type": "number", "description": "返回内容最大字符数，默认 12000"},
                    },
                    "required": ["url"],
                },
            ),
            ToolDefinition(
                name="webfetch_batch",
                description=(
                    "批量抓取多个 URL（最多 8 个，http/https），并发执行并合并返回各页面"
                    " Markdown；单页失败不影响其余页面。用于对比/汇总多个文档、多来源查证。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "urls": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "要抓取的 URL 列表（http/https，最多 8 个）",
                        },
                        "maxChars": {"type": "number", "description": "每个 URL 返回内容最大字符数，默认 12000"},
                    },
                    "required": ["urls"],
                },
            ),
        ]

    # ------------------------------------------------------------ 磁盘缓存

    def _cache_path(self, url: str) -> str:
        return os.path.join(self._cache_dir, hashlib.sha256(url.encode("utf-8")).hexdigest() + ".json")

    def _cache_get(self, url: str) -> Optional[Tuple[int, str, str]]:
        """返回 (status, content_type, text)；未命中或过期返回 None。"""
        if not self._cache_dir:
            return None
        try:
            with open(self._cache_path(url), "r", encoding="utf-8") as f:
                entry = json.load(f)
            if time.time() - float(entry.get("fetched_at", 0)) <= self._cache_ttl:
                return int(entry["status"]), str(entry["content_type"]), str(entry["text"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def _cache_set(self, url: str, status: int, content_type: str, text: str) -> None:
        if not self._cache_dir:
            return
        try:
            os.makedirs(self._cache_dir, exist_ok=True)
            with open(self._cache_path(url), "w", encoding="utf-8") as f:
                json.dump(
                    {"url": url, "status": status, "content_type": content_type,
                     "text": text, "fetched_at": time.time()},
                    f, ensure_ascii=False,
                )
        except OSError:
            logger.warning("[WebFetch] 缓存写入失败: %s", url)

    # ------------------------------------------------------------ 安全校验

    def validate_url(self, url: str) -> str:
        """协议白名单 + SSRF 防护（拒绝内网/回环/链路本地/保留地址）。"""
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise PermissionError(
                f"仅允许 http/https 协议，收到: {parsed.scheme or '无协议'}"
            )
        host = parsed.hostname
        if not host:
            raise ValueError("URL 缺少主机名")
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            raise ValueError(f"无法解析主机名: {host}") from exc
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if (
                ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified
            ):
                raise PermissionError(f"SSRF 防护: 拒绝访问内网/回环地址 {ip}")
        return url

    # ------------------------------------------------------------ 执行

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        if name == "webfetch":
            url = str(args.get("url") or "").strip()
            if not url:
                return "[Error]: 缺少 url 参数。"
            max_chars = max(1_000, int(args.get("maxChars") or DEFAULT_MAX_CHARS))
            return await self._fetch_one(url, max_chars)
        if name == "webfetch_batch":
            return await self._fetch_batch(args)
        raise ValueError(f"Unknown Web Tool: {name}")

    async def _curl_cffi_fetch(self, url: str) -> Optional[Tuple[int, str, str]]:
        """TLS 指纹级兜底：curl_cffi 模拟真实 Chrome 的 TLS 握手。

        httpx 的 Python TLS 栈握手特征（JA3）会被 Cloudflare 等识别——
        请求头再像浏览器也无用；libcurl-impersonate 复刻 Chrome 的完整
        握手指纹。仅在 2xx/3xx 成功时返回 (status, content_type, text)，
        未安装/失败/仍被拦截一律返回 None（调用方走拦截提示）。
        """
        if not _HAS_CURL_CFFI:
            return None
        try:
            async with _CurlAsyncSession(impersonate="chrome", timeout=TIMEOUT) as session:
                resp = await session.get(url)
            if not (200 <= resp.status_code < 400):
                return None
            raw = (resp.content or b"")[:MAX_READ_BYTES]
            if not raw:
                return None
            text = raw.decode("utf-8", errors="replace")
            content_type = resp.headers.get("content-type", "")
            if "html" in content_type.lower():
                text = self.html_to_markdown(text)
            else:
                text = _WS_RE.sub(" ", text).strip()
            return resp.status_code, content_type, text
        except Exception as exc:
            logger.debug("[WebFetch] curl_cffi 兜底失败 %s: %s", url, exc)
            return None

    async def _fetch_one(self, url: str, max_chars: int) -> str:
        """抓取单个 URL（带缓存），返回格式化结果串。

        三级递进：① 指纹池请求；② 403 → 换指纹重试一次；
        ③ 仍 403 → curl_cffi Chrome TLS 指纹兜底；再失败说明站点需要
        执行 JS 质询，返回可操作提示让 Agent 换源或求助用户。
        """
        try:
            target = self.validate_url(url)
            cached = self._cache_get(target)
            if cached is not None:
                status, content_type, text = cached
                flag = "cache=hit"
            else:
                resp = None
                retried = False
                for attempt in range(2):
                    async with self._client_factory() as client:
                        resp = await client.get(target)
                    if resp.status_code != 403:
                        break
                    if attempt == 0:
                        retried = True
                        logger.info("[WebFetch] %s 返回 403，切换浏览器指纹重试", target)

                if resp.status_code == 403:
                    curl = await self._curl_cffi_fetch(target)
                    if curl is None:
                        return (
                            f"[Fetch Blocked]: {target} 拒绝访问（403，站点启用了反爬保护）。\n"
                            f"建议：1) 更换其他来源的 URL 重试；"
                            f"2) 改用搜索引擎缓存页（如该页的 Google/Bing 快照）；"
                            f"3) 该站内容必需时，请用户在浏览器手动打开后另存到工作区，"
                            f"或配置浏览器自动化 MCP Server（如 @playwright/mcp）。"
                        )
                    status, content_type, text = curl
                    self._cache_set(target, status, content_type, text)
                    flag = "cache=miss,curl-impersonate"
                else:
                    resp.raise_for_status()
                    status = resp.status_code
                    content_type = resp.headers.get("content-type", "")
                    raw = resp.content[:MAX_READ_BYTES]
                    if not raw:
                        return f"[Fetch]: {target} 返回空内容。"
                    text = raw.decode("utf-8", errors="replace")
                    if "html" in content_type.lower():
                        text = self.html_to_markdown(text)
                    else:
                        text = _WS_RE.sub(" ", text).strip()
                    self._cache_set(target, status, content_type, text)
                    flag = "cache=miss" + (",retry=1" if retried else "")

            if len(text) > max_chars:
                text = text[:max_chars] + f"\n...[输出截断，仅显示前 {max_chars} 字符]"
            return (
                f"[Fetch OK]: {target} (status={status}, {len(text)} 字符, {flag})\n\n{text}"
            )
        except PermissionError as exc:
            return f"[Security Guard]: {exc}"
        except (httpx.HTTPError, ValueError, OSError) as exc:
            return f"[Fetch Error]: {exc}"

    async def _fetch_batch(self, args: Dict[str, Any]) -> str:
        urls = args.get("urls") or []
        if not isinstance(urls, list) or not urls:
            return "[Error]: 缺少 urls 参数（URL 列表）。"
        urls = [str(u).strip() for u in urls if str(u).strip()]
        if not urls:
            return "[Error]: urls 列表为空。"
        if len(urls) > MAX_BATCH_URLS:
            return f"[Error]: 一次最多抓取 {MAX_BATCH_URLS} 个 URL，收到 {len(urls)} 个。"
        max_chars = max(1_000, int(args.get("maxChars") or DEFAULT_MAX_CHARS))

        sem = asyncio.Semaphore(BATCH_CONCURRENCY)

        async def _one(u: str) -> str:
            async with sem:
                return await self._fetch_one(u, max_chars)

        results = await asyncio.gather(*[_one(u) for u in urls])
        body = "\n\n---\n\n".join(results)
        return f"[Batch Fetch]: {len(results)} 个 URL 抓取完成\n\n{body}"

    # ------------------------------------------------------------ HTML → Markdown

    @staticmethod
    def html_to_markdown(html_text: str) -> str:
        title_m = _TITLE_RE.search(html_text)
        title = _WS_RE.sub(" ", title_m.group(1)).strip() if title_m else ""
        title = html.unescape(title)

        text = _SKIP_TAGS.sub(" ", html_text)

        # 代码块（先于标签剥离）
        text = _CODE_BLOCK_RE.sub(
            lambda m: "\n```\n" + _strip_tags(m.group(1)).strip() + "\n```\n", text
        )

        # 标题
        def _heading(m: re.Match) -> str:
            level = int(m.group(1)[1])
            return "\n" + "#" * level + " " + html.unescape(_strip_tags(m.group(2))).strip() + "\n"

        text = _HEADING_RE.sub(_heading, text)

        # 链接
        def _link(m: re.Match) -> str:
            label = html.unescape(_strip_tags(m.group(2))).strip()
            href = html.unescape(m.group(1)).strip()
            return f"[{label}]({href})" if label and href else (label or href)

        text = _LINK_RE.sub(_link, text)
        text = _IMG_RE.sub(lambda m: f"![{m.group(1)}]" if m.group(1).strip() else "", text)

        # 强调
        text = _STRONG_RE.sub(lambda m: f"**{html.unescape(_strip_tags(m.group(2))).strip()}**", text)
        text = _EM_RE.sub(lambda m: f"*{html.unescape(_strip_tags(m.group(2))).strip()}*", text)

        # 表格（管道语法）
        def _row(m: re.Match) -> str:
            cells = _TABLE_CELL_RE.findall(m.group(1))
            if not cells:
                return ""
            return "| " + " | ".join(
                html.unescape(_strip_tags(c[1])).strip() for c in cells
            ) + " |\n"

        text = _TABLE_ROW_RE.sub(_row, text)

        # 列表项 / 引用
        text = _LI_RE.sub(
            lambda m: "- " + html.unescape(_strip_tags(m.group(1))).strip() + "\n", text
        )
        text = _BLOCKQUOTE_RE.sub(
            lambda m: "> " + html.unescape(_strip_tags(m.group(1))).strip() + "\n", text
        )

        # 块级标签 → 换行
        for tag in _BLOCK_TAGS:
            text = re.sub(rf"</?{tag}[^>]*>", "\n", text, flags=re.I)

        # 行内代码 / 剩余标签
        text = _CODE_INLINE_RE.sub(lambda m: "`" + html.unescape(m.group(1)).strip() + "`", text)
        text = _TAG_RE.sub("", text)
        text = html.unescape(text)

        # 压缩空白：行内折叠、连续空行收敛
        lines = [_WS_RE.sub(" ", line).strip() for line in text.split("\n")]
        out: List[str] = []
        for line in lines:
            if not line:
                if out and out[-1] != "":
                    out.append("")
                continue
            out.append(line)
        while out and out[-1] == "":
            out.pop()

        body = "\n".join(out)
        return f"# {title}\n\n{body}" if title else body