# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""OpenAI 兼容适配器（DeepSeek / OpenAI / Kimi / 通义千问 / GLM 等）。

手写 SSE 流式解析 + tool_calls 按 index 增量拼接。
兼容任何遵循 OpenAI 聊天完成接口格式的 API。
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


class OpenAICompatAdapter(BaseLLMAdapter):
    name = "openai-compat"
    provider_id = ""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-v4-flash",
        timeout: float = 120.0,
        temperature: float = 0.2,
        provider_id: str = "deepseek",
        enable_cache: bool = True,
        custom_headers: Optional[Dict[str, str]] = None,
        reasoning_effort: str = "",
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.provider_id = provider_id
        self.enable_cache = enable_cache
        # 自定义请求头：清洗后叠加在默认头之上（可覆盖 Authorization，适配网关自定义鉴权）
        self.custom_headers = clean_custom_headers(custom_headers)
        # 推理强度（"low"/"medium"/"high"）：透传 reasoning_effort 字段；
        # 启用时省略 temperature（多数推理模型不接受 temperature）
        self.reasoning_effort = (reasoning_effort or "").strip().lower()
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # read=None：流式响应中"块间静默"属正常现象（推理模型首 token 前可能长时间思考），
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
                "Authorization": f"Bearer {self.api_key}",
            },
            expand_header_templates(self.custom_headers),
        )

    def _build_payload(
        self, messages: List[Message], tools: List[ToolDefinition]
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_dict() for m in messages],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self.reasoning_effort:
            # 推理模型：透传 reasoning_effort，省略 temperature
            payload["reasoning_effort"] = self.reasoning_effort
        else:
            payload["temperature"] = self.temperature
        if tools:
            payload["tools"] = [
                {"type": "function", "function": t.__dict__} for t in tools
            ]
        return payload

    async def chat_stream(
        self,
        messages: List[Message],
        tools: List[ToolDefinition],
        events: Optional[TypedEventBus] = None,
    ) -> Tuple[str, List[ToolCall]]:
        client = self._get_client()
        payload = self._build_payload(messages, tools)

        try:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            ) as response:
                if response.status_code != 200:
                    body = (await response.aread()).decode("utf-8", errors="replace")[:500]
                    raise LLMError(
                        f"[LLM Error] HTTP {response.status_code}: {body}",
                        retryable=response.status_code in RETRYABLE_STATUS,
                    )
                return await self._parse_sse(response, events)
        except httpx.TimeoutException as exc:
            raise LLMError(f"[LLM Error] 请求超时: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"[LLM Error] 网络错误: {exc}", retryable=True) from exc

    @staticmethod
    def _extract_usage(parsed: Dict[str, Any]) -> Optional[Dict[str, int]]:
        """从流式 chunk 提取 usage（末帧返回，choices 为空）。统一字段格式。"""
        usage = parsed.get("usage")
        if not usage or not isinstance(usage, dict):
            return None
        prompt = usage.get("prompt_tokens")
        if not isinstance(prompt, int):
            return None
        details = (
            usage.get("prompt_tokens_details")
            or usage.get("prompt_token_details")
            or usage.get("input_tokens_details")
            or usage.get("cache_details")
            or {}
        )
        if not isinstance(details, dict):
            details = {}
        hit = (
            usage.get("prompt_cache_hit_tokens")
            or usage.get("cache_hit_tokens")
            or usage.get("cache_read_tokens")
            or usage.get("cached_tokens")
            or usage.get("cache_read")
            or usage.get("cache_read_input_tokens")
            or details.get("cached_tokens")
            or details.get("cache_hit_tokens")
            or details.get("cache_read_tokens")
            or details.get("cache_read")
            or details.get("cache_read_input_tokens")
            or 0
        )
        if not isinstance(hit, int):
            hit = 0
        return {
            "prompt_tokens": prompt,
            "completion_tokens": usage.get("completion_tokens", 0),
            "prompt_cache_hit_tokens": hit,
        }

    async def _parse_sse(
        self, response: httpx.Response, events: Optional[TypedEventBus]
    ) -> Tuple[str, List[ToolCall], Optional[Dict[str, int]]]:
        full_content = ""
        tool_calls_map: Dict[int, ToolCall] = {}
        usage: Optional[Dict[str, int]] = None
        buffer = ""
        byte_buffer = b""

        async for chunk in response.aiter_bytes():
            text, byte_buffer = decode_utf8_incremental(byte_buffer, chunk)
            buffer += text
            lines = buffer.split("\n")
            buffer = lines.pop()

            for line in lines:
                line = line.strip()
                if not line or line.startswith(":"):
                    continue
                if line == "data: [DONE]":
                    return full_content, self._finalize_tool_calls(tool_calls_map), usage
                if not line.startswith("data: "):
                    continue

                try:
                    parsed = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue

                extracted = self._extract_usage(parsed)
                if extracted is not None:
                    usage = extracted

                delta = (parsed.get("choices") or [{}])[0].get("delta")
                if not delta:
                    continue

                # 不同 OpenAI 兼容供应商对思考增量使用不同字段名。
                # 工具调用前的内容也要转发，否则 UI 只会显示零散片段。
                stream_content = delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning") or delta.get("thinking")
                if stream_content:
                    full_content += stream_content
                    if events:
                        await events.emit("llm:stream", {"chunk": stream_content})

                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    target = tool_calls_map.get(idx)
                    if target is None:
                        target = ToolCall(id=tc.get("id", ""), name="", arguments="")
                        tool_calls_map[idx] = target
                    if tc.get("id"):
                        target.id = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        target.name += fn["name"]
                    if fn.get("arguments"):
                        target.arguments += fn["arguments"]

        # 响应没有 [DONE] 时也不能静默丢掉最后一个不完整的 UTF-8 字符。
        if byte_buffer:
            full_content += byte_buffer.decode("utf-8", errors="replace")
        return full_content, self._finalize_tool_calls(tool_calls_map), usage

    @staticmethod
    def _finalize_tool_calls(tool_calls_map: Dict[int, ToolCall]) -> List[ToolCall]:
        calls = [tool_calls_map[i] for i in sorted(tool_calls_map)]
        calls = [c for c in calls if c.name]
        # 部分供应商（Kimi/GLM/通义等）流式响应可能不携带 tool_call id：
        # 补齐合成 id，否则 assistant(tool_calls) 无法与后续 tool 消息匹配，
        # API 直接返回 "insufficient tool messages following tool_calls message"。
        for c in calls:
            if not c.id:
                c.id = f"call_{uuid.uuid4().hex[:12]}"
        return calls

    async def test_connection(self) -> Tuple[bool, str, float]:
        start = time.time()
        try:
            client = self._get_client()
            async with client.stream(
                "GET",
                f"{self.base_url}/models",
                headers=self._headers(),
                timeout=15.0,
            ) as resp:
                elapsed = (time.time() - start) * 1000
                if resp.status_code == 200:
                    return True, f"连接成功 ({int(elapsed)}ms)", elapsed
                body = (await resp.aread()).decode("utf-8", errors="replace")[:200]
                return False, f"HTTP {resp.status_code}: {body}", elapsed
        except Exception as exc:
            elapsed = (time.time() - start) * 1000
            return False, str(exc)[:150], elapsed
