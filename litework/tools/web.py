"""Web 抓取工具（对标 OpenCode 的 webfetch 工具）。

解决 Agent 缺少联网能力时凭记忆/臆测回答外部信息的问题：
- 只允许 http/https（阻止 file:// 等本地协议读取本地文件）
- SSRF 防护：解析主机名后拒绝回环 / 私网 / 链路本地 / 保留地址
- HTML → Markdown 轻量转换（纯标准库，不依赖 bs4），超长输出截断
- 磁盘缓存：命中 TTL 内的抓取结果直接返回，避免重复请求
- webfetch_batch：并发批量抓取多个 URL，单页失败不影响其余页面
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import logging
import os
import re
import socket
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from .. import __version__
from ..core.types import ToolDefinition

logger = logging.getLogger("litework.tools")

MAX_READ_BYTES = 2 * 1024 * 1024  # 最多读取 2MB
DEFAULT_MAX_CHARS = 12_000
TIMEOUT = 15.0
USER_AGENT = f"lite-work-agent/{__version__} (web research)"
CACHE_TTL = 3600  # 缓存有效期：1 小时
MAX_BATCH_URLS = 8  # 单次批量抓取上限
BATCH_CONCURRENCY = 4  # 批量并发数（温和限速）

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
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(
                follow_redirects=True,
                timeout=TIMEOUT,
                headers={"User-Agent": USER_AGENT},
            )
        )
        self._cache_dir = cache_dir
        self._cache_ttl = cache_ttl

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

    async def _fetch_one(self, url: str, max_chars: int) -> str:
        """抓取单个 URL（带缓存），返回格式化结果串。"""
        try:
            target = self.validate_url(url)
            cached = self._cache_get(target)
            if cached is not None:
                status, content_type, text = cached
                flag = "cache=hit"
            else:
                async with self._client_factory() as client:
                    resp = await client.get(target)
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
                flag = "cache=miss"

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