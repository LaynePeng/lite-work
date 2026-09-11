# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""Anthropic Claude 适配器。

与 OpenAI 兼容接口不同：使用 x-api-key 头、messages API、不同的 SSE 事件结构。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..core.events import TypedEventBus
from ..core.types import Message, ToolCall, ToolDefinition
from .base import (
    RETRYABLE_STATUS,
    BaseLLMAdapter,
    LLMError,
    clean_custom_headers,
    decode_utf8_incremental,
    expand_header_templates,
    merge_headers,
)

logger = logging.getLogger("litework.llm")


class AnthropicAdapter(BaseLLMAdapter):
    name = "anthropic"
    provider_id = "anthropic"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com/v1",
        model: str = "claude-sonnet-4-20250514",
        timeout: float = 120.0,
        temperature: float = 0.2,
        max_tokens: int = 8192,
        provider_id: str = "anthropic",
        enable_cache: bool = True,
        custom_headers: Optional[Dict[str, str]] = None,
        reasoning_effort: str = "",
        idle_timeout: float = 120.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.provider_id = provider_id
        self.enable_cache = enable_cache
        # 流式空闲看门狗：连续 N 秒未收到任何 chunk 视为连接卡死，中止并抛可重试错误
        self.idle_timeout = max(30.0, float(idle_timeout))
        # 自定义请求头：清洗后叠加在默认头之上（可覆盖 x-api-key / anthropic-version）
        self.custom_headers = clean_custom_headers(custom_headers)
        # 推理强度（"low"/"medium"/"high"）：映射为 thinking budget_tokens；
        # 启用时省略 temperature（Anthropic API 限制）
        self.reasoning_effort = (reasoning_effort or "").strip().lower()
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # read=None：流式响应中"块间静默"属正常现象（扩展思考可能长时间无输出），
            # 不能用固定 read timeout 掐断；卡死连接由 AgentLoop 的 llm_timeout + 重试兜底。
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, read=None)
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> Dict[str, str]:
        return merge_headers(
            {
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            expand_header_templates(self.custom_headers),
        )

    @staticmethod
    def _to_anthropic_messages(messages: List[Message]) -> List[Dict[str, Any]]:
        """把统一 Message 结构转成 Anthropic 的 user/assistant 消息格式。"""
        out: List[Dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                continue
            if m.role == "tool":
                # Anthropic 用 user 消息携带 tool_result
                out.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": m.tool_call_id or "",
                        "content": m.content or "",
                    }],
                })
            elif m.role == "assistant":
                content: List[Dict[str, Any]] = []
                if m.content:
                    content.append({"type": "text", "text": m.content})
                for tc in m.tool_calls or []:
                    content.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": json.loads(tc.arguments) if tc.arguments else {},
                    })
                out.append({"role": "assistant", "content": content})
            else:  # user
                out.append({"role": "user", "content": m.content or ""})
        return out

    # 推理强度 → thinking budget_tokens 映射
    _THINKING_BUDGETS = {"low": 2048, "medium": 8192, "high": 32000, "max": 64000}

    def _build_payload(
        self,
        messages: List[Message],
        tools: List[ToolDefinition],
        system: Optional[str],
        enable_cache: bool = True,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "stream": True,
            "messages": self._to_anthropic_messages(messages),
        }
        if self.reasoning_effort:
            # 扩展思考：映射 budget_tokens；启用时省略 temperature（API 限制）
            budget = self._THINKING_BUDGETS.get(self.reasoning_effort, 8192)
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
            # max_tokens 必须大于 budget_tokens（Anthropic 校验），否则自动抬高
            if payload["max_tokens"] <= budget:
                payload["max_tokens"] = budget + 4096
        else:
            payload["temperature"] = self.temperature
        if system:
            if enable_cache:
                payload["system"] = [
                    {"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}
                ]
            else:
                payload["system"] = [{"type": "text", "text": system}]
        if tools:
            payload["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters,
                }
                for t in tools
            ]
            if enable_cache and payload["tools"]:
                payload["tools"][-1]["cache_control"] = {"type": "ephemeral"}
        return payload

    async def chat_stream(
        self,
        messages: List[Message],
        tools: List[ToolDefinition],
        events: Optional[TypedEventBus] = None,
    ) -> Tuple[str, List[ToolCall]]:
        client = self._get_client()

        # 提取 system 消息（Anthropic 单独字段）
        system = None
        if messages and messages[0].role == "system":
            system = messages[0].content

        payload = self._build_payload(messages, tools, system, enable_cache=self.enable_cache)

        try:
            async with client.stream(
                "POST",
                f"{self.base_url}/messages",
                headers=self._headers(),
                json=payload,
            ) as response:
                if response.status_code != 200:
                    body = (await response.aread()).decode("utf-8", errors="replace")[:500]
                    raise LLMError(
                        f"[Anthropic Error] HTTP {response.status_code}: {body}",
                        retryable=response.status_code in RETRYABLE_STATUS,
                    )
                return await self._parse_sse(response, events)
        except httpx.TimeoutException as exc:
            raise LLMError(f"[Anthropic Error] 请求超时: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"[Anthropic Error] 网络错误: {exc}", retryable=True) from exc

    @staticmethod
    def _start_usage(parsed: Dict[str, Any]) -> Optional[Dict[str, int]]:
        """message_start 事件的 input/cache_read 统计。"""
        msg = parsed.get("message") or {}
        m_usage = msg.get("usage") or {}
        if not isinstance(m_usage.get("input_tokens"), int):
            return None
        return {
            "prompt_tokens": m_usage["input_tokens"],
            "completion_tokens": m_usage.get("output_tokens", 0),
            "prompt_cache_hit_tokens": m_usage.get("cache_read_input_tokens", 0),
        }

    @staticmethod
    def _delta_usage(parsed: Dict[str, Any]) -> Optional[Dict[str, int]]:
        """message_delta 事件的 output/cache_read 增量。"""
        d_usage = parsed.get("usage") or {}
        if not isinstance(d_usage.get("output_tokens"), int):
            return None
        return {
            "output_tokens": d_usage.get("output_tokens", 0),
            "cache_read_input_tokens": d_usage.get("cache_read_input_tokens", 0),
        }

    async def _parse_sse(
        self, response: httpx.Response, events: Optional[TypedEventBus]
    ) -> Tuple[str, List[ToolCall], Optional[Dict[str, int]]]:
        full_content = ""
        tool_calls: List[ToolCall] = []
        current_tool: Optional[ToolCall] = None
        usage: Optional[Dict[str, int]] = None
        buffer = ""
        byte_buffer = b""

        # 空闲看门狗：逐 chunk 限时，卡死连接可见中断（而非等 llm_timeout 静默超时）
        aiter = response.aiter_bytes()
        while True:
            try:
                chunk = await asyncio.wait_for(aiter.__anext__(), timeout=self.idle_timeout)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError as exc:
                raise LLMError(
                    f"[LLM Error] 流式响应空闲超过 {int(self.idle_timeout)}s，连接可能已卡死",
                    retryable=True,
                ) from exc
            text, byte_buffer = decode_utf8_incremental(byte_buffer, chunk)
            buffer += text
            lines = buffer.split("\n")
            buffer = lines.pop()

            for line in lines:
                line = line.strip()
                if not line or line.startswith(":"):
                    continue
                if line == "event: message_stop":
                    break
                if not line.startswith("data: "):
                    continue

                try:
                    parsed = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue

                ev_type = parsed.get("type", "")
                if ev_type == "message_start":
                    start = self._start_usage(parsed)
                    if start is not None:
                        usage = start
                elif ev_type == "message_delta":
                    delta = self._delta_usage(parsed)
                    if delta is not None:
                        usage = usage or {"prompt_tokens": 0, "completion_tokens": 0,
                                          "prompt_cache_hit_tokens": 0}
                        # message_delta.usage 为请求累计值（非增量），直接覆盖
                        usage["completion_tokens"] = delta.get("output_tokens", 0)
                        usage["prompt_cache_hit_tokens"] = delta.get("cache_read_input_tokens", 0)
                elif ev_type == "content_block_start":
                    block = parsed.get("content_block", {})
                    if block.get("type") == "tool_use":
                        current_tool = ToolCall(
                            id=block.get("id", ""),
                            name=block.get("name", ""),
                            arguments="",
                        )
                        tool_calls.append(current_tool)
                elif ev_type == "content_block_delta":
                    delta = parsed.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        full_content += text
                        if events:
                            await events.emit("llm:stream", {"chunk": text})
                    elif delta.get("type") == "thinking_delta":
                        # 扩展思考内容：转发给 UI（与 OpenAI 的 reasoning_content 对齐）
                        thinking = delta.get("thinking", "")
                        full_content += thinking
                        if events:
                            await events.emit("llm:stream", {"chunk": thinking})
                    elif delta.get("type") == "input_json_delta" and current_tool:
                        current_tool.arguments += delta.get("partial_json", "")
                elif ev_type == "content_block_stop":
                    current_tool = None

        calls = [tc for tc in tool_calls if tc.name]
        # 兜底：补齐缺失的 tool_use id（正常情况下 Anthropic 一定带 id）
        for tc in calls:
            if not tc.id:
                tc.id = f"call_{uuid.uuid4().hex[:12]}"
        return full_content, calls, usage

    async def test_connection(self) -> Tuple[bool, str, float]:
        start = time.time()
        try:
            client = self._get_client()
            payload = {
                "model": self.model,
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
            }
            async with client.stream(
                "POST",
                f"{self.base_url}/messages",
                headers=self._headers(),
                json=payload,
                timeout=30.0,
            ) as resp:
                elapsed = (time.time() - start) * 1000
                if resp.status_code in (200, 201):
                    return True, f"连接成功 ({int(elapsed)}ms)", elapsed
                body = (await resp.aread()).decode("utf-8", errors="replace")[:200]
                return False, f"HTTP {resp.status_code}: {body}", elapsed
        except Exception as exc:
            elapsed = (time.time() - start) * 1000
            return False, str(exc)[:150], elapsed