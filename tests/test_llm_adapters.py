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
from litework.core.types import Message
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


# ---------------------------------------------------------------- 与 AgentLoop 重试的衔接

async def test_retryable_flag_drives_agent_loop_retry(tmp_path):
    """适配器 retryable 标记 → AgentLoop._call_llm_with_retry 的重试语义。"""
    from tests.test_agent_loop_phases import _make_loop

    class Flaky:
        def __init__(self):
            self.attempts = 0

        async def chat_stream(self, messages, tools, events=None):
            self.attempts += 1
            if self.attempts == 1:
                raise __import__("litework.llm", fromlist=["LLMError"]).LLMError(
                    "HTTP 429", retryable=True)
            return "ok", [], None

    loop, _, _ = _make_loop(tmp_path, Flaky(), llm_retries=2)
    content, _, _ = await loop._call_llm_with_retry(
        [Message(role="user", content="x")], [])
    assert content == "ok"
