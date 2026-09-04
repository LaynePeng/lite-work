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
    # 20 基础工具 + todo_write + ask_user（Agent 提问）+ 6 办公工具（docx/xlsx/pptx/pdf/data_analyze/chart_make）
    assert len(registry.get_tools()) == 28
    assert registry.has("todo_write")
    assert registry.has("ask_user")
    # 办公工具（GAI 通用入口）已注册
    for name in ("docx_create", "xlsx_create", "pptx_create", "pdf_create",
                 "data_analyze", "chart_make"):
        assert registry.has(name), f"缺少办公工具 {name}"


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
