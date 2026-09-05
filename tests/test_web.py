"""webfetch 工具测试：HTML→Markdown / 协议与 SSRF 防护 / 截断 / Cordis 插件注册。"""
from __future__ import annotations

import httpx
import pytest

from litework.app import AgentApp
from litework.security.guard import SecurityGuard, ThreatLevel
from litework.tools.web import WebFetchTools


@pytest.fixture
def http_ok():
    def _handler(request):
        return httpx.Response(
            200,
            text="<html><head><title>示例页</title></head>"
                 "<body><h1>标题一</h1><p>正文 <a href='https://a.com/b'>链接</a></p>"
                 "<pre>code line</pre><ul><li>项一</li></ul></body></html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    return _handler


@pytest.fixture
def web_tools(monkeypatch, http_ok):
    tools = WebFetchTools(
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(http_ok))
    )
    # 跳过 DNS 解析（SSRF 校验单测里单独覆盖）
    monkeypatch.setattr(tools, "validate_url", lambda url: url)
    return tools


# ---------------------------------------------------------------- HTML → Markdown

def test_html_to_markdown_basic():
    html = ("<html><head><title>文档标题</title></head><body>"
            "<h2>小节</h2><p>一段 <b>粗体</b> 与 <em>斜体</em></p>"
            "<pre>print('hi')</pre><li>列表项</li></body></html>")
    md = WebFetchTools.html_to_markdown(html)
    assert md.startswith("# 文档标题")
    assert "## 小节" in md
    assert "一段 **粗体** 与 *斜体*" in md
    assert "```" in md and "print('hi')" in md
    assert "- 列表项" in md


def test_html_to_markdown_strips_scripts_and_entities():
    html = "<html><body><script>evil()</script><p>a &amp; b &lt; c</p></body></html>"
    md = WebFetchTools.html_to_markdown(html)
    assert "evil" not in md
    assert "a & b < c" in md


def test_html_to_markdown_table_and_links():
    html = ("<html><body><table><tr><td>列A</td><td>列B</td></tr>"
            "<tr><td>1</td><td>2</td></tr></table>"
            "<p><a href='https://x.dev'>外链</a></p></body></html>")
    md = WebFetchTools.html_to_markdown(html)
    assert "| 列A | 列B |" in md
    assert "[外链](https://x.dev)" in md


# ---------------------------------------------------------------- 协议与 SSRF 防护

def test_validate_url_rejects_bad_scheme():
    tools = WebFetchTools()
    with pytest.raises(PermissionError):
        tools.validate_url("file:///etc/passwd")
    with pytest.raises(PermissionError):
        tools.validate_url("ftp://example.com/x")


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/",
    "http://localhost/",
    "http://192.168.1.10/",
    "http://10.0.0.1/",
    "http://[::1]/",
])
def test_validate_url_rejects_ssrf(url):
    with pytest.raises(PermissionError):
        WebFetchTools().validate_url(url)


def test_validate_url_accepts_public_host(monkeypatch):
    import socket

    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))
    ])
    assert WebFetchTools().validate_url("https://example.com/doc") == "https://example.com/doc"


# ---------------------------------------------------------------- 执行

async def test_execute_html_page(web_tools):
    result = await web_tools.execute("webfetch", {"url": "https://example.com/"})
    assert result.startswith("[Fetch OK]: https://example.com/")
    assert "# 示例页" in result
    assert "标题一" in result
    assert "- 项一" in result


async def test_execute_plain_text(web_tools, monkeypatch):
    def _handler(request):
        return httpx.Response(200, text="hello world", headers={"content-type": "text/plain"})

    tools = WebFetchTools(client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler)))
    monkeypatch.setattr(tools, "validate_url", lambda url: url)
    result = await tools.execute("webfetch", {"url": "https://example.com/raw"})
    assert "hello world" in result


async def test_execute_truncates_long_output(web_tools, monkeypatch):
    def _handler(request):
        return httpx.Response(
            200, text="x" * 5000, headers={"content-type": "text/plain"}
        )

    tools = WebFetchTools(client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler)))
    monkeypatch.setattr(tools, "validate_url", lambda url: url)
    result = await tools.execute("webfetch", {"url": "https://example.com/", "maxChars": 1000})
    assert "输出截断" in result
    assert len(result) <= 1000 + 200  # 截断提示追加在尾部


async def test_execute_missing_url(web_tools):
    result = await web_tools.execute("webfetch", {})
    assert "缺少 url" in result


async def test_execute_http_error(web_tools, monkeypatch):
    def _handler(request):
        return httpx.Response(404)

    tools = WebFetchTools(client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler)))
    monkeypatch.setattr(tools, "validate_url", lambda url: url)
    result = await tools.execute("webfetch", {"url": "https://example.com/missing"})
    assert result.startswith("[Fetch Error]")


# ---------------------------------------------------------------- 缓存

async def test_cache_hit_avoids_second_request(monkeypatch, http_ok, tmp_path):
    calls = {"n": 0}

    def _counting_handler(request):
        calls["n"] += 1
        return http_ok(request)

    tools = WebFetchTools(
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(_counting_handler)),
        cache_dir=str(tmp_path / "cache"),
    )
    monkeypatch.setattr(tools, "validate_url", lambda url: url)

    r1 = await tools.execute("webfetch", {"url": "https://example.com/"})
    r2 = await tools.execute("webfetch", {"url": "https://example.com/"})
    assert calls["n"] == 1, "第二次抓取应命中缓存"
    assert "cache=hit" in r2
    assert "cache=miss" in r1
    assert "# 示例页" in r2


async def test_cache_persists_across_instances(monkeypatch, http_ok, tmp_path):
    cache_dir = str(tmp_path / "cache")
    calls = {"n": 0}

    def _counting_handler(request):
        calls["n"] += 1
        return http_ok(request)

    def _make():
        return WebFetchTools(
            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(_counting_handler)),
            cache_dir=cache_dir,
        )

    t1, t2 = _make(), _make()
    monkeypatch.setattr(t1, "validate_url", lambda url: url)
    monkeypatch.setattr(t2, "validate_url", lambda url: url)

    await t1.execute("webfetch", {"url": "https://example.com/"})
    r2 = await t2.execute("webfetch", {"url": "https://example.com/"})
    assert calls["n"] == 1
    assert "cache=hit" in r2


async def test_cache_expired_after_ttl(monkeypatch, http_ok, tmp_path):
    calls = {"n": 0}

    def _counting_handler(request):
        calls["n"] += 1
        return http_ok(request)

    tools = WebFetchTools(
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(_counting_handler)),
        cache_dir=str(tmp_path / "cache"),
        cache_ttl=-1,  # 立即过期
    )
    monkeypatch.setattr(tools, "validate_url", lambda url: url)

    await tools.execute("webfetch", {"url": "https://example.com/"})
    r2 = await tools.execute("webfetch", {"url": "https://example.com/"})
    assert calls["n"] == 2
    assert "cache=miss" in r2


# ---------------------------------------------------------------- 批量抓取

async def test_batch_fetch_combines_results(monkeypatch, http_ok):
    def _handler(request):
        return httpx.Response(200, text="<html><body><h1>AAA</h1></body></html>",
                              headers={"content-type": "text/html"})

    tools = WebFetchTools(client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler)))
    monkeypatch.setattr(tools, "validate_url", lambda url: url)

    result = await tools.execute("webfetch_batch", {
        "urls": ["https://a.dev/x", "https://b.dev/y"],
    })
    assert result.startswith("[Batch Fetch]: 2 个 URL 抓取完成")
    assert "# AAA" in result
    assert "[Fetch OK]: https://a.dev/x" in result
    assert "[Fetch OK]: https://b.dev/y" in result


async def test_batch_partial_failure(monkeypatch):
    def _handler(request):
        return httpx.Response(200, text="<html><body><h1>BBB</h1></body></html>",
                              headers={"content-type": "text/html"})

    tools = WebFetchTools(client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler)))

    def _validate(url):
        if not url.lower().startswith(("http://", "https://")):
            raise PermissionError(f"仅允许 http/https 协议，收到: {url}")
        return url

    monkeypatch.setattr(tools, "validate_url", _validate)

    result = await tools.execute("webfetch_batch", {
        "urls": ["file:///etc/passwd", "https://ok.dev/x"],
    })
    assert "[Security Guard]" in result
    assert "[Fetch OK]: https://ok.dev/x" in result


async def test_batch_too_many_urls(web_tools):
    result = await web_tools.execute("webfetch_batch", {
        "urls": [f"https://x.dev/{i}" for i in range(9)],
    })
    assert "最多抓取 8 个" in result


async def test_batch_missing_or_empty_urls(web_tools):
    r1 = await web_tools.execute("webfetch_batch", {})
    assert "缺少 urls" in r1
    r2 = await web_tools.execute("webfetch_batch", {"urls": []})
    assert "缺少 urls" in r2


# ---------------------------------------------------------------- 安全卫士

def test_guard_blocks_bad_scheme_url():
    guard = SecurityGuard()
    r = guard.check_tool("webfetch", {"url": "file:///etc/passwd"})
    assert r.level == ThreatLevel.HIGH
    r = guard.check_tool("webfetch", {"url": "https://example.com/"})
    assert r.level == ThreatLevel.SAFE
    r = guard.check_tool("webfetch_batch", {"urls": ["https://a.dev/", "ftp://b.dev/"]})
    assert r.level == ThreatLevel.HIGH
    r = guard.check_tool("webfetch_batch", {"urls": ["https://a.dev/", "https://b.dev/"]})
    assert r.level == ThreatLevel.SAFE


# ---------------------------------------------------------------- Cordis 插件注册

def test_webfetch_registered_in_full_registry(tmp_path):
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    registry = app.build_registry()
    assert registry.has("webfetch")
    assert registry.has("webfetch_batch")
    # 全量注册表基线（新增工具只会更多，不会更少）
    assert len(registry.get_tools()) >= 36
    assert registry.has("todo_write")
    assert registry.has("ask_user")
    # 办公产出工具（GAI 通用入口）
    for name in ("docx_create", "docx_append", "xlsx_create", "pptx_create", "pdf_create",
                 "data_analyze", "chart_make"):
        assert registry.has(name), f"缺少办公工具 {name}"
    # 办公文件读取 + OCR
    for name in ("docx_read", "xlsx_read", "pptx_read", "pdf_read",
                 "ocr_image", "ocr_document", "ocr_pptx"):
        assert registry.has(name), f"缺少读取/OCR 工具 {name}"


def test_webfetch_kept_in_plan_agent(tmp_path):
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    registry = app.create_agent_registry("plan")
    assert registry.has("webfetch")
    assert registry.has("webfetch_batch")
    assert not registry.has("execute_command")


def test_agent_pruning_removes_webfetch(tmp_path):
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    registry = app.build_registry(allowed=["read_file"])
    assert registry.has("read_file")
    assert not registry.has("webfetch")


# ---------------------------------------------------------------- 反爬：浏览器指纹 + 403 重试

def test_browser_profiles_look_like_real_browsers():
    """指纹池：UA 是真实浏览器形态 + 导航头齐全（Sec-Fetch-Site: none 等）。"""
    from litework.tools.web import BROWSER_PROFILES

    assert len(BROWSER_PROFILES) >= 4
    uas = set()
    for p in BROWSER_PROFILES:
        ua = p["User-Agent"]
        assert ua.startswith("Mozilla/5.0"), f"非浏览器 UA: {ua}"
        assert "lite-work" not in ua.lower()
        uas.add(ua)
        # 导航请求头齐全（Sec-Fetch-User Safari 版豁免）
        assert p["Sec-Fetch-Dest"] == "document"
        assert p["Sec-Fetch-Mode"] == "navigate"
        assert p["Sec-Fetch-Site"] == "none"
        assert p["Accept-Language"]
    # 至少 3 种不同浏览器指纹（Chrome/Firefox/Safari）
    assert len(uas) >= 3


def test_default_client_factory_rotates_fingerprints():
    """默认工厂轮换指纹：连续创建的客户端 UA 不同。"""
    tools = WebFetchTools()
    uas = []
    for _ in range(4):
        client = tools._default_client_factory()
        uas.append(client.headers["User-Agent"])
    assert len(set(uas)) == 4, "连续 4 次应轮换出 4 种不同指纹"


async def test_403_retry_then_success(monkeypatch):
    """首次 403 → 换指纹重试成功，结果带 retry 标记。"""
    calls = {"n": 0}

    def _handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(403)
        return httpx.Response(200, text="retry ok",
                              headers={"content-type": "text/plain"})

    tools = WebFetchTools(client_factory=lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(_handler)))
    monkeypatch.setattr(tools, "validate_url", lambda url: url)

    r = await tools.execute("webfetch", {"url": "https://example.com/blocked"})
    assert "[Fetch OK]" in r
    assert "retry ok" in r
    assert "retry=1" in r
    assert calls["n"] == 2


async def test_403_persistent_returns_actionable_hints(monkeypatch):
    """持续 403（强反爬站）→ 明确提示与替代方案，而非含糊错误。"""
    calls = {"n": 0}

    def _handler(request):
        calls["n"] += 1
        return httpx.Response(403)

    tools = WebFetchTools(client_factory=lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(_handler)))
    monkeypatch.setattr(tools, "validate_url", lambda url: url)
    # 隔离 curl_cffi 层（开发环境已安装会发起真实请求）
    async def _no_curl(url):
        return None
    monkeypatch.setattr(tools, "_curl_cffi_fetch", _no_curl)

    r = await tools.execute("webfetch", {"url": "https://example.com/locked"})
    assert r.startswith("[Fetch Blocked]")
    assert "403" in r
    assert "反爬" in r
    # 给 Agent 的替代建议
    assert "更换其他来源" in r or "其他来源" in r
    assert calls["n"] == 2, "应重试一次后放弃"


async def test_403_falls_back_to_curl_cffi(monkeypatch):
    """指纹重试仍 403 → curl_cffi 兜底成功（curl-impersonate 标记）。"""
    def _handler(request):
        return httpx.Response(403)

    tools = WebFetchTools(client_factory=lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(_handler)))
    monkeypatch.setattr(tools, "validate_url", lambda url: url)

    async def _curl_ok(url):
        return (200, "text/plain", "curl impersonated content")
    monkeypatch.setattr(tools, "_curl_cffi_fetch", _curl_ok)

    r = await tools.execute("webfetch", {"url": "https://example.com/tlsblocked"})
    assert "[Fetch OK]" in r
    assert "curl impersonated content" in r
    assert "curl-impersonate" in r


async def test_404_not_retried(monkeypatch):
    """非 403 错误（404）不重试：一次请求即返回错误。"""
    calls = {"n": 0}

    def _handler(request):
        calls["n"] += 1
        return httpx.Response(404)

    tools = WebFetchTools(client_factory=lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(_handler)))
    monkeypatch.setattr(tools, "validate_url", lambda url: url)

    r = await tools.execute("webfetch", {"url": "https://example.com/missing"})
    assert r.startswith("[Fetch Error]")
    assert calls["n"] == 1
