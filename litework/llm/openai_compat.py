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
import re
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
from .stream_meter import current_stream_meter

logger = logging.getLogger("litework.llm")


# ---- DeepSeek DSML 工具调用救援 ---------------------------------------------
# deepseek v4.x 经 OpenAI 兼容中转网关（如 opencode zen go）实测：偶发把工具
# 调用以原生 DSML 标记写进 content 通道而非结构化 tool_calls——结果原文泄漏
# 到聊天界面、工具也不执行。全角竖线 ｜（U+FF5C）是其特殊 token 的组成部分。
# 按观测到的格式宽容解析；格式不认识则原样返回（fail-open，不改变既有行为）。
_DSML_BAR = "\uff5c"
_DSML_CALLS_BLOCK_RE = re.compile(
    rf"<{_DSML_BAR}DSML{_DSML_BAR}\s*calls\s*>"
    rf".*?</{_DSML_BAR}DSML{_DSML_BAR}\s*calls\s*>",
    re.DOTALL,
)
_DSML_INVOKE_RE = re.compile(
    rf'<{_DSML_BAR}DSML{_DSML_BAR}\s*invoke\s+name\s*=\s*"([^"]+)"\s*>'
    rf"(.*?)</{_DSML_BAR}DSML{_DSML_BAR}\s*invoke\s*>",
    re.DOTALL,
)
_DSML_PARAM_RE = re.compile(
    rf'<{_DSML_BAR}DSML{_DSML_BAR}\s*parameter\s+name\s*=\s*"([^"]+)"'
    rf'(?:\s+string\s*=\s*"(true|false)")?\s*>'
    rf"(.*?)</{_DSML_BAR}DSML{_DSML_BAR}\s*parameter\s*>",
    re.DOTALL,
)


def _parse_dsml_value(raw: str, is_string: bool) -> Any:
    """string="true" 为字面字符串；否则按 JSON 字面量解析，失败回退原样。"""
    raw = raw.strip()
    if is_string or not raw:
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _extract_dsml_tool_calls(content: str) -> Tuple[str, List[ToolCall]]:
    """从 content 提取 DSML 工具调用标记，返回（剔除标记后的正文, 调用列表）。"""
    invokes = list(_DSML_INVOKE_RE.finditer(content))
    if not invokes:
        return content, []
    calls: List[ToolCall] = []
    for m in invokes:
        args: Dict[str, Any] = {}
        for pm in _DSML_PARAM_RE.finditer(m.group(2)):
            args[pm.group(1)] = _parse_dsml_value(
                pm.group(3), (pm.group(2) or "false") == "true"
            )
        calls.append(
            ToolCall(
                id=f"call_dsml_{uuid.uuid4().hex[:12]}",
                name=m.group(1),
                arguments=json.dumps(args, ensure_ascii=False),
            )
        )
    cleaned = _DSML_CALLS_BLOCK_RE.sub("", content)
    cleaned = _DSML_INVOKE_RE.sub("", cleaned)
    return cleaned.strip(), calls


class OpenAICompatAdapter(BaseLLMAdapter):
    name = "openai-compat"
    provider_id = ""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-flash",
        timeout: float = 120.0,
        temperature: float = 0.2,
        provider_id: str = "deepseek",
        enable_cache: bool = True,
        custom_headers: Optional[Dict[str, str]] = None,
        reasoning_effort: str = "",
        idle_timeout: float = 120.0,
        proxy_url: str = "",
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        # LLM API 出网代理（空串 = 直连；httpx proxy=None 同义）
        self.proxy_url = proxy_url or None
        # 流式空闲看门狗：连续 N 秒未收到任何 chunk 视为连接卡死，
        # 中止并抛可重试错误（由 AgentLoop 的 llm:retry 事件给 UI 可见反馈），
        # 避免旧行为「静默挂起 300s×(retries+1)」让用户干等十分钟。
        self.idle_timeout = max(30.0, float(idle_timeout))
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
                timeout=httpx.Timeout(self.timeout, read=None),
                proxy=self.proxy_url,
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

    @staticmethod
    def _wire_message(m: Message) -> Dict[str, Any]:
        """Message → OpenAI wire 格式：剔除 lite-work 内部字段。

        `name`（tool 消息的工具名打标）是 OpenAI 规范里已废弃的可选字段，
        多数供应商容忍，但部分网关直接拒绝（opencode zen go 实测 HTTP 400：
        `messages[N]: "name" is not supported by this endpoint`）；tool 消息
        与 assistant(tool_calls) 的关联由 tool_call_id 承担，wire 上不需要
        name。`agent` 是会话内 Agent 打标，同样不该出网。落盘/事件仍走
        Message.to_dict()，不受影响。
        """
        d = m.to_dict()
        d.pop("name", None)
        d.pop("agent", None)
        return d

    def _build_payload(
        self, messages: List[Message], tools: List[ToolDefinition]
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [self._wire_message(m) for m in messages],
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
    ) -> Tuple[str, List[ToolCall], Optional[Dict[str, Any]]]:
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
        reasoning_text = ""
        tool_calls_map: Dict[int, ToolCall] = {}
        usage: Optional[Dict[str, int]] = None
        buffer = ""
        byte_buffer = b""
        done = False

        # token 速度计量器（由 AgentLoop 通过 contextvar 注入；没有就纯流式转发）
        meter = current_stream_meter()

        # 空闲看门狗：逐 chunk 限时，卡死连接 120s 内可见中断（而非等 llm_timeout）
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
                if line == "data: [DONE]":
                    done = True
                    break
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
                # 正文与思考分通道累计：思考（reasoning_content/reasoning/thinking）
                # 只做流式转发（UI 实时可见，message:added 后被最终正文替换），
                # 不并入 full_content——推理模型的 CoT 混进 assistant 消息会
                # 同时污染落盘历史与回传上下文（deepseek v4.x 经中转实测）。
                content_delta = delta.get("content")
                reasoning_delta = (
                    delta.get("reasoning_content")
                    or delta.get("reasoning")
                    or delta.get("thinking")
                )
                if content_delta:
                    full_content += content_delta
                if reasoning_delta:
                    reasoning_text += reasoning_delta
                for stream_piece in (content_delta, reasoning_delta):
                    if not stream_piece:
                        continue
                    # 计量（速度）：只对增量做小正则累加；到节流窗口才推一次实时进度
                    due = meter.feed(stream_piece) if meter is not None else False
                    if events:
                        if due and meter is not None:
                            await events.emit("llm:progress", meter.progress())
                        await events.emit("llm:stream", {"chunk": stream_piece})

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

            if done:
                break

        # 响应没有 [DONE] 时也不能静默丢掉最后一个不完整的 UTF-8 字符
        # （收到 [DONE] 提前结束时与旧行为一致：不追加残余字节）。
        if byte_buffer and not done:
            full_content += byte_buffer.decode("utf-8", errors="replace")

        calls = self._finalize_tool_calls(tool_calls_map)
        # 兜底：个别网关会把最终答案放进思考通道（正文全空且没有工具调用）
        # ——此时用思考文本充当 content（维持旧行为）。有工具调用时保持空
        # 正文，不让 CoT 混进 assistant 消息污染历史与回传上下文。
        if not full_content.strip() and reasoning_text.strip() and not calls:
            full_content = reasoning_text
        # DSML 工具调用救援：模型把工具调用以原生 DSML 标记写进 content 通道
        # 时（deepseek v4.x 经 OpenAI 兼容中转实测），解析成 ToolCall 并剔除
        # 原文——工具得以执行，而不是标记泄漏到界面后任务中断。
        if _DSML_INVOKE_RE.search(full_content):
            full_content, dsml_calls = _extract_dsml_tool_calls(full_content)
            calls.extend(dsml_calls)
        return full_content, calls, usage

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

    async def list_models(self) -> Tuple[bool, List[str], str]:
        """拉取供应商可用模型列表（GET /models，游标分页）。

        响应为 OpenAI 兼容惯例：{"data": [{"id": ...}], "has_more": bool,
        "last_id": str}。has_more 时以 after=last_id 续拉，安全上限
        10 页 / 累计 1000 个模型，防游标死循环。
        """
        try:
            client = self._get_client()
            seen: set = set()
            after: Optional[str] = None
            for _ in range(10):
                resp = await client.get(
                    f"{self.base_url}/models",
                    headers=self._headers(),
                    timeout=15.0,
                    params={"after": after} if after else None,
                )
                if resp.status_code != 200:
                    body = resp.text[:200]
                    return False, [], f"HTTP {resp.status_code}: {body}"
                data = resp.json()
                for item in data.get("data") or []:
                    mid = item.get("id") if isinstance(item, dict) else None
                    if isinstance(mid, str) and mid:
                        seen.add(mid)
                if len(seen) >= 1000 or not data.get("has_more"):
                    break
                after = data.get("last_id")
                if not after:
                    break
            return True, sorted(seen), ""
        except Exception as exc:
            return False, [], str(exc)[:150]
