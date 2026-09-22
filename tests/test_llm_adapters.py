# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""LLM 适配器真实 API 协议测试（P2-6 测试盲区）。

用 httpx.MockTransport 模拟 OpenAI 兼容 / Anthropic 的真实 SSE 协议，
覆盖：流式文本、reasoning 内容、tool_calls 增量拼接、usage 口径映射、
错误分类（retryable 标记）——不依赖任何真实网络。
"""
from __future__ import annotations

import json

import httpx
import pytest

from litework.core.events import TypedEventBus
from litework.core.types import Message, ToolCall
from litework.llm.anthropic import AnthropicAdapter
from litework.llm.base import RETRYABLE_STATUS
from litework.llm.openai_compat import OpenAICompatAdapter


def _sse(events: list) -> bytes:
    """把帧列表编码为 SSE 字节流（每帧一个 data: 行）。"""
    return ("\n".join(f"data: {json.dumps(e, ensure_ascii=False)}" for e in events)
            + "\ndata: [DONE]\n\n").encode("utf-8")


def _openai_adapter(handler) -> OpenAICompatAdapter:
    adapter = OpenAICompatAdapter(api_key="sk-test", base_url="https://mock.test")
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return adapter


def _anthropic_adapter(handler) -> AnthropicAdapter:
    adapter = AnthropicAdapter(api_key="sk-ant-test", base_url="https://mock.test")
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return adapter


# ---------------------------------------------------------------- OpenAI 兼容

async def test_openai_stream_text_and_usage():
    """流式文本 + 末帧 usage（choices 为空）+ cache 命中口径（DeepSeek 风格）。"""
    def handler(request):
        return httpx.Response(200, content=_sse([
            {"choices": [{"delta": {"content": "你好"}}]},
            {"choices": [{"delta": {"content": "，世界"}}]},
            {"choices": [], "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 60},
            }},
        ]), headers={"content-type": "text/event-stream"})

    adapter = _openai_adapter(handler)
    content, calls, usage = await adapter.chat_stream(
        [Message(role="user", content="hi")], [])
    assert content == "你好，世界"
    assert calls == []
    assert usage == {"prompt_tokens": 100, "completion_tokens": 5,
                     "prompt_cache_hit_tokens": 60}


async def test_openai_reasoning_content_streamed():
    """思考增量（reasoning_content）也并入正文并转发 llm:stream 事件。"""
    def handler(request):
        return httpx.Response(200, content=_sse([
            {"choices": [{"delta": {"reasoning_content": "思考中…"}}]},
            {"choices": [{"delta": {"content": "答案"}}]},
        ]), headers={"content-type": "text/event-stream"})

    bus = TypedEventBus(strict=True)
    chunks = []
    bus.on("llm:stream", lambda p: chunks.append(p["chunk"]))
    adapter = _openai_adapter(handler)
    content, _, _ = await adapter.chat_stream([Message(role="user", content="hi")], [], bus)
    assert "思考中…" in content and "答案" in content
    assert chunks == ["思考中…", "答案"]


async def test_openai_tool_calls_incremental_assembly():
    """tool_calls 按 index 增量拼接：id/name/arguments 跨帧累积。"""
    args_json = '{"filePath": "x.txt", "content": "hello"}'
    half = len(args_json) // 2
    def handler(request):
        return httpx.Response(200, content=_sse([
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1", "function": {"name": "write_file", "arguments": args_json[:half]}}
            ]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": args_json[half:]}}
            ]}}]},
        ]), headers={"content-type": "text/event-stream"})

    adapter = _openai_adapter(handler)
    content, calls, _ = await adapter.chat_stream([Message(role="user", content="hi")], [])
    assert len(calls) == 1
    assert calls[0].id == "call_1"
    assert calls[0].name == "write_file"
    assert calls[0].arguments == args_json


async def test_openai_error_classification():
    """429/503 → retryable=True；401 → retryable=False（立即失败不重试）。"""
    for status in (429, 503):
        def handler(request, status=status):
            return httpx.Response(status, text="rate limited")
        adapter = _openai_adapter(handler)
        with pytest.raises(Exception) as ei:
            await adapter.chat_stream([Message(role="user", content="hi")], [])
        assert ei.value.retryable is True, f"HTTP {status} 应可重试"
        assert status in RETRYABLE_STATUS

    def handler_401(request):
        return httpx.Response(401, text="invalid api key")
    adapter = _openai_adapter(handler_401)
    with pytest.raises(Exception) as ei:
        await adapter.chat_stream([Message(role="user", content="hi")], [])
    assert ei.value.retryable is False


async def test_openai_wire_payload_strips_internal_fields():
    """wire 消息不得携带 lite-work 内部字段（name / agent）。

    回归（用户报障）：tool 消息带 name 打标是 OpenAI 已废弃的可选字段，
    opencode zen go 网关直接 HTTP 400 `messages[N]: "name" is not supported
    by this endpoint`；agent 是会话内打标，同样不该出网。
    tool 消息的关联由 tool_call_id 承担，必须保留。
    """
    captured = {}

    def handler(request):
        captured["payload"] = json.loads(request.read())
        return httpx.Response(200, content=_sse([
            {"choices": [{"delta": {"content": "ok"}}]},
        ]), headers={"content-type": "text/event-stream"})

    adapter = _openai_adapter(handler)
    msgs = [
        Message(role="user", content="hi"),
        Message(role="assistant", content=None, agent="build",
                tool_calls=[ToolCall(id="c1", name="read_file", arguments="{}")]),
        Message(role="tool", name="read_file", tool_call_id="c1",
                content="data", agent="build"),
    ]
    _, _, _ = await adapter.chat_stream(msgs, [])

    wire = captured["payload"]["messages"]
    assert all("name" not in m for m in wire)
    assert all("agent" not in m for m in wire)
    assert wire[2]["tool_call_id"] == "c1"
    # 源对象不受影响：落盘/事件仍带 name/agent（会话回放、观察打包要用）
    assert msgs[2].name == "read_file" and msgs[2].agent == "build"


# ---------------------------------------------------------------- Anthropic

def _anthropic_sse(events: list) -> bytes:
    return ("\n".join(f"data: {json.dumps(e, ensure_ascii=False)}" for e in events)
            + "\nevent: message_stop\n\n").encode("utf-8")


async def test_anthropic_stream_text_thinking_and_tool_use():
    """文本 + thinking 增量 + tool_use input_json_delta 拼接 + usage 合并。"""
    def handler(request):
        return httpx.Response(200, content=_anthropic_sse([
            {"type": "message_start", "message": {"usage": {
                "input_tokens": 80, "output_tokens": 0, "cache_read_input_tokens": 40,
            }}},
            {"type": "content_block_start", "content_block": {"type": "text"}},
            {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "先想…"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "结论"}},
            {"type": "content_block_stop"},
            {"type": "content_block_start", "content_block": {
                "type": "tool_use", "id": "toolu_1", "name": "read_file"}},
            {"type": "content_block_delta", "delta": {"type": "input_json_delta",
                                                      "partial_json": '{"filePa'}},
            {"type": "content_block_delta", "delta": {"type": "input_json_delta",
                                                      "partial_json": 'th": "a.py"}'}},
            {"type": "content_block_stop"},
            {"type": "message_delta", "usage": {"output_tokens": 12,
                                                "cache_read_input_tokens": 40}},
        ]), headers={"content-type": "text/event-stream"})

    bus = TypedEventBus(strict=True)
    adapter = _anthropic_adapter(handler)
    content, calls, usage = await adapter.chat_stream(
        [Message(role="user", content="hi")], [])
    assert "先想…" in content and "结论" in content
    assert len(calls) == 1
    assert calls[0].id == "toolu_1"
    assert calls[0].name == "read_file"
    assert json.loads(calls[0].arguments) == {"filePath": "a.py"}
    # usage：message_start 的 input/cache_read + message_delta 的 output 覆盖
    assert usage == {"prompt_tokens": 80, "completion_tokens": 12,
                     "prompt_cache_hit_tokens": 40}


async def test_anthropic_error_classification():
    def handler(request):
        return httpx.Response(500, text="server error")
    adapter = _anthropic_adapter(handler)
    with pytest.raises(Exception) as ei:
        await adapter.chat_stream([Message(role="user", content="hi")], [])
    assert ei.value.retryable is True

    def handler_400(request):
        return httpx.Response(400, text="bad request")
    adapter = _anthropic_adapter(handler_400)
    with pytest.raises(Exception) as ei:
        await adapter.chat_stream([Message(role="user", content="hi")], [])
    assert ei.value.retryable is False


# ---------------------------------------------------------------- custom_headers 会话模板

async def test_custom_header_conversation_id_expanded_and_sent():
    """{conversation_id} 模板 + 上下文有值 → 请求头被展开发送。

    守卫 OpenCode Go（x-opencode-session）一类「必需会话头」的回归：
    头丢失会导致上游 400、子 Agent 秒死。
    """
    from litework.core.types import header_context

    seen: dict = {}

    def handler(request):
        seen["x-opencode-session"] = request.headers.get("x-opencode-session")
        return httpx.Response(200, content=_sse([
            {"choices": [{"delta": {"content": "ok"}}]},
        ]), headers={"content-type": "text/event-stream"})

    adapter = OpenAICompatAdapter(
        api_key="sk-test", base_url="https://mock.test",
        custom_headers={"x-opencode-session": "{conversation_id}"})
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    token = header_context.set({"conversation_id": "conv-abc"})
    try:
        content, _, _ = await adapter.chat_stream([Message(role="user", content="hi")], [])
    finally:
        header_context.reset(token)

    assert content == "ok"
    assert seen.get("x-opencode-session") == "conv-abc"


async def test_custom_header_conversation_id_missing_drops_and_warns(caplog):
    """上下文无 conversation_id → 该头被丢弃（不发空头）且记录 WARNING。"""
    import logging

    from litework.core.types import header_context

    sent: dict = {}

    def handler(request):
        sent["header_present"] = "x-opencode-session" in request.headers
        return httpx.Response(200, content=_sse([
            {"choices": [{"delta": {"content": "ok"}}]},
        ]), headers={"content-type": "text/event-stream"})

    adapter = OpenAICompatAdapter(
        api_key="sk-test", base_url="https://mock.test",
        custom_headers={"x-opencode-session": "{conversation_id}"})
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    token = header_context.set({})
    try:
        with caplog.at_level(logging.WARNING, logger="litework.llm"):
            await adapter.chat_stream([Message(role="user", content="hi")], [])
    finally:
        header_context.reset(token)

    assert sent.get("header_present") is False
    assert any(
        r.levelno == logging.WARNING and "x-opencode-session" in r.getMessage()
        for r in caplog.records
    )


# ---------------------------------------------------------------- list_models 模型列表

async def test_openai_list_models_parse_dedupe_sort():
    """正常解析：data[].id 去重 + 排序，has_more=False 单页结束。"""
    def handler(request):
        return httpx.Response(200, json={
            "data": [{"id": "m-c"}, {"id": "m-a"}, {"id": "m-c"}, {"id": "m-b"}],
            "has_more": False,
        })

    adapter = _openai_adapter(handler)
    ok, models, msg = await adapter.list_models()
    assert ok is True
    assert msg == ""
    assert models == ["m-a", "m-b", "m-c"]


async def test_openai_list_models_cursor_pagination():
    """游标分页：第一页 has_more + last_id → 第二页带 after=last_id 参数。"""
    calls = []

    def handler(request):
        after = request.url.params.get("after")
        calls.append(after)
        if after is None:
            return httpx.Response(200, json={
                "data": [{"id": "page1-a"}, {"id": "page1-b"}],
                "has_more": True, "last_id": "cursor-1",
            })
        return httpx.Response(200, json={
            "data": [{"id": "page2-a"}],
            "has_more": False,
        })

    adapter = _openai_adapter(handler)
    ok, models, msg = await adapter.list_models()
    assert ok is True and msg == ""
    assert calls == [None, "cursor-1"]
    assert models == ["page1-a", "page1-b", "page2-a"]


async def test_openai_list_models_http_error():
    """非 200：返回 (False, [], "HTTP code: body")。"""
    def handler(request):
        return httpx.Response(403, text="forbidden: no api key")

    adapter = _openai_adapter(handler)
    ok, models, msg = await adapter.list_models()
    assert ok is False
    assert models == []
    assert msg.startswith("HTTP 403:") and "forbidden" in msg


async def test_openai_list_models_exception():
    """网络/解析异常：返回 (False, [], str(exc)[:150])。"""
    def handler(request):
        raise httpx.ConnectError("connection refused")

    adapter = _openai_adapter(handler)
    ok, models, msg = await adapter.list_models()
    assert ok is False
    assert models == []
    assert "connection refused" in msg


async def test_anthropic_list_models_parse_and_pagination():
    """正常解析 + after_id 游标分页（has_more/last_id → after_id 参数）。"""
    calls = []

    def handler(request):
        after = request.url.params.get("after_id")
        calls.append(after)
        if after is None:
            return httpx.Response(200, json={
                "data": [{"id": "claude-a"}, {"id": "claude-b"}],
                "has_more": True, "last_id": "cursor-9",
            })
        return httpx.Response(200, json={
            "data": [{"id": "claude-c"}],
            "has_more": False,
        })

    adapter = _anthropic_adapter(handler)
    ok, models, msg = await adapter.list_models()
    assert ok is True and msg == ""
    assert calls == [None, "cursor-9"]
    assert models == ["claude-a", "claude-b", "claude-c"]


async def test_registry_list_models_overrides_reach_transport(monkeypatch):
    """overrides 生效：fake api_key + base_url 打到 mock transport。"""
    import litework.llm.registry as registry_mod

    def handler(request):
        assert request.headers.get("authorization") == "Bearer sk-fake"
        return httpx.Response(200, json={
            "data": [{"id": "m-b"}, {"id": "m-a"}], "has_more": False,
        })

    captured = {}

    class FakeAdapter(OpenAICompatAdapter):
        def __init__(self, **kwargs):
            captured.update(kwargs)
            super().__init__(**kwargs)
            self._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(registry_mod, "OpenAICompatAdapter", FakeAdapter)

    reg = registry_mod.LLMRegistry()
    ok, models, msg = await reg.list_models(
        "deepseek",
        overrides={"api_key": "sk-fake", "base_url": "https://mock.test"},
    )
    assert ok is True and msg == ""
    assert models == ["m-a", "m-b"]
    # overrides 覆盖了注册表默认配置传入适配器
    assert captured["api_key"] == "sk-fake"
    assert captured["base_url"] == "https://mock.test"


async def test_registry_list_models_missing_key_returns_value_error():
    """未配置 API Key：build_adapter 抛 ValueError → (False, [], msg)。"""
    from litework.llm.registry import LLMRegistry

    reg = LLMRegistry()
    # custom_ 前缀供应商不在 PROVIDER_META 中（不受环境变量兜底影响）
    ok, models, msg = await reg.list_models("custom_nokey")
    assert ok is False
    assert models == []
    assert "API Key" in msg
