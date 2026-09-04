"""LLM 供应商自定义 Header 测试：合并/覆盖/清洗 + 端到端请求头 + 配置 round-trip。"""
from __future__ import annotations

import httpx
import pytest

from litework.app import AgentApp
from litework.core.types import header_context
from litework.llm.anthropic import AnthropicAdapter
from litework.llm.base import clean_custom_headers, expand_header_templates, merge_headers
from litework.llm.openai_compat import OpenAICompatAdapter
from litework.llm.registry import LLMRegistry


# ---------------------------------------------------------------- 清洗

def test_clean_custom_headers_filters_invalid():
    assert clean_custom_headers(None) == {}
    assert clean_custom_headers("not-a-dict") == {}
    assert clean_custom_headers([("X-A", "1")]) == {}
    # 空键/空值/非 str 值被丢弃，其余 strip
    assert clean_custom_headers({
        "": "v", "X-Empty": "  ", "X-Ok": " value ", "X-Num": 123, None: "x",
    }) == {"X-Ok": "value"}


# ---------------------------------------------------------------- 合并语义

def test_openai_headers_merge_and_override():
    adapter = OpenAICompatAdapter(
        api_key="sk-test",
        custom_headers={"X-Title": "My App", "authorization": "Bearer gateway-token"},
    )
    headers = adapter._headers()
    assert headers["Content-Type"] == "application/json"
    # 自定义头追加
    assert headers["X-Title"] == "My App"
    # HTTP 头名大小写不敏感：小写自定义头覆盖默认 Authorization，且不重复
    assert headers["authorization"] == "Bearer gateway-token"
    assert "Authorization" not in headers


def test_merge_headers_is_case_insensitive():
    headers = merge_headers(
        {"Content-Type": "application/json", "X-Test": "default"},
        {"content-type": "text/plain", "x-test": "custom"},
    )
    assert headers == {"content-type": "text/plain", "x-test": "custom"}


def test_anthropic_headers_merge_and_keep_defaults():
    adapter = AnthropicAdapter(
        api_key="sk-ant",
        custom_headers={"anthropic-beta": "output-128k-2025-02-19"},
    )
    headers = adapter._headers()
    assert headers["x-api-key"] == "sk-ant"
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["anthropic-beta"] == "output-128k-2025-02-19"


def test_no_custom_headers_keeps_defaults():
    oa = OpenAICompatAdapter(api_key="sk-test")._headers()
    assert oa == {"Content-Type": "application/json", "Authorization": "Bearer sk-test"}
    an = AnthropicAdapter(api_key="sk-ant")._headers()
    assert an == {
        "Content-Type": "application/json",
        "x-api-key": "sk-ant",
        "anthropic-version": "2023-06-01",
    }


# ---------------------------------------------------------------- 端到端请求头

def _sse_ok(request) -> httpx.Response:
    return httpx.Response(
        200,
        text='data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n',
        headers={"content-type": "text/event-stream"},
    )


async def test_openai_request_carries_custom_headers(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.headers))
        return _sse_ok(request)

    adapter = OpenAICompatAdapter(
        api_key="sk-test",
        custom_headers={"X-Title": "My App", "Authorization": "Bearer gw"},
    )
    monkeypatch.setattr(
        adapter, "_get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    content, calls, _ = await adapter.chat_stream([], [])
    assert content == "hi"
    assert seen.get("x-title") == "My App"          # httpx 头名小写化
    assert seen.get("authorization") == "Bearer gw"  # 覆盖默认认证头


async def test_anthropic_request_carries_custom_headers(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.headers))
        return httpx.Response(
            200,
            text='event: message_start\ndata: {"type":"message_start"}\n\ndata: [DONE]\n\n',
            headers={"content-type": "text/event-stream"},
        )

    adapter = AnthropicAdapter(
        api_key="sk-ant",
        custom_headers={"anthropic-beta": "output-128k"},
    )
    monkeypatch.setattr(
        adapter, "_get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    # Mock 流不完整没关系，headers 已捕获
    try:
        await adapter.chat_stream([], [])
    except Exception:
        pass
    assert seen.get("anthropic-beta") == "output-128k"
    assert seen.get("x-api-key") == "sk-ant"


# ---------------------------------------------------------------- registry round-trip

def _registry_with_headers() -> LLMRegistry:
    reg = LLMRegistry()
    reg.providers["deepseek"]["api_key"] = "sk-test"
    reg.providers["deepseek"]["custom_headers"] = {"X-Title": "My App"}
    return reg


def test_registry_build_adapter_passes_headers():
    reg = _registry_with_headers()
    adapter = reg.build_adapter("deepseek")
    assert adapter.custom_headers == {"X-Title": "My App"}
    assert adapter._headers()["X-Title"] == "My App"


def test_registry_to_config_exports_headers():
    reg = _registry_with_headers()
    cfg = reg.to_config()  # API 返回（脱敏 key）
    assert cfg["providers"]["deepseek"]["custom_headers"] == {"X-Title": "My App"}
    # 落盘同样保留
    persisted = reg.to_config(persist_key=True)
    assert persisted["providers"]["deepseek"]["custom_headers"] == {"X-Title": "My App"}


def test_registry_apply_config_round_trip():
    reg = _registry_with_headers()
    # to_config → apply_config 完整回路不丢
    cfg = reg.to_config(persist_key=True)
    reg2 = LLMRegistry()
    reg2.apply_config(cfg)
    assert reg2.providers["deepseek"]["custom_headers"] == {"X-Title": "My App"}
    # 空字典（清空所有自定义头）也应被保留，视为显式清空
    reg2.providers["deepseek"]["custom_headers"] = {}
    cfg2 = reg2.to_config(persist_key=True)
    reg3 = LLMRegistry()
    reg3.apply_config(cfg2)
    assert reg3.providers["deepseek"]["custom_headers"] == {}


def test_app_update_llm_config_can_clear_headers(tmp_path):
    app = AgentApp(config_dir=str(tmp_path / ".lite-work"))
    app.llm_registry.providers["deepseek"]["api_key"] = "sk-test"
    app.llm_registry.providers["deepseek"]["custom_headers"] = {"X-Test": "old"}

    app.update_llm_config(providers={"deepseek": {"custom_headers": {}}})
    assert app.llm_registry.providers["deepseek"]["custom_headers"] == {}


def test_app_update_llm_config_ignores_invalid_headers(tmp_path):
    app = AgentApp(config_dir=str(tmp_path / ".lite-work"))
    app.llm_registry.providers["deepseek"]["custom_headers"] = {"X-Test": "old"}

    app.update_llm_config(providers={"deepseek": {"custom_headers": "invalid"}})
    assert app.llm_registry.providers["deepseek"]["custom_headers"] == {"X-Test": "old"}


# ---------------------------------------------------------------- 模板变量展开

def test_expand_header_templates_known_vars():
    ctx = {
        "session_id": "session_abc",
        "conversation_id": "conv123",
        "workspace": "/proj",
        "model": "deepseek-v4-flash",
        "provider": "custom_opencode",
    }
    headers = {
        "X-Session": "{session_id}",
        "X-Conversation": "{conversation_id}",
        "X-Workspace": "{workspace}",
        "X-Model": "{model}",
        "X-Provider": "{provider}",
    }
    assert expand_header_templates(headers, context=ctx) == {
        "X-Session": "session_abc",
        "X-Conversation": "conv123",
        "X-Workspace": "/proj",
        "X-Model": "deepseek-v4-flash",
        "X-Provider": "custom_opencode",
    }


def test_expand_header_templates_empty_context_drops_template_header():
    # 无会话上下文（如测试连接）→ {conversation_id} 展开为空 → 该头被丢弃
    result = expand_header_templates(
        {"X-Session": "{conversation_id}", "X-Static": "keep"},
        context={},
    )
    assert "X-Session" not in result
    assert result["X-Static"] == "keep"


def test_expand_header_templates_static_headers_untouched():
    # 无模板的静态头原样保留，即使 context 为空
    assert expand_header_templates(
        {"X-Title": "My App", "Authorization": "Bearer xxx"},
        context={},
    ) == {"X-Title": "My App", "Authorization": "Bearer xxx"}


def test_expand_header_templates_uses_header_context_var():
    token = header_context.set({
        "session_id": "sess_ctx", "conversation_id": "conv_ctx",
    })
    try:
        result = expand_header_templates({
            "X-Session": "{session_id}",
            "X-C": "{conversation_id}",
        })
    finally:
        header_context.reset(token)
    assert result == {"X-Session": "sess_ctx", "X-C": "conv_ctx"}


async def test_openai_request_expands_session_template(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.headers))
        return _sse_ok(request)

    adapter = OpenAICompatAdapter(
        api_key="sk-test",
        custom_headers={"x-opencode-session": "{conversation_id}", "X-Static": "s"},
    )
    monkeypatch.setattr(
        adapter, "_get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    token = header_context.set({"conversation_id": "conv-e2e", "session_id": "s1"})
    try:
        content, _, _ = await adapter.chat_stream([], [])
    finally:
        header_context.reset(token)
    assert content == "hi"
    assert seen.get("x-opencode-session") == "conv-e2e"
    assert seen.get("x-static") == "s"