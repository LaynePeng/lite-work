"""LLM 适配器抽象基类。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..core.events import TypedEventBus
from ..core.types import Message, ToolCall, ToolDefinition, header_context


def decode_utf8_incremental(buffer: bytes, chunk: bytes) -> Tuple[str, bytes]:
    data = buffer + chunk
    try:
        return data.decode("utf-8"), b""
    except UnicodeDecodeError as exc:
        # 只缓存确实未完成的字符。固定保留末尾 3 字节会在一个 chunk
        # 同时包含合法文本和半个汉字时，把前面的完整字符错误地替换掉。
        if exc.reason == "unexpected end of data" and exc.start < len(data):
            return data[:exc.start].decode("utf-8"), data[exc.start:]
        return data.decode("utf-8", errors="replace"), b""


def clean_custom_headers(raw: Optional[Any]) -> Dict[str, str]:
    """清洗用户自定义 HTTP 头：仅保留 str:str、去空键空值。

    作为适配器构造的最终防线——配置层校验失守时保证非法值不进真实请求。
    """
    if not isinstance(raw, dict):
        return {}
    cleaned: Dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        key = k.strip()
        value = v.strip()
        if key and value:
            cleaned[key] = value
    return cleaned


# custom_headers 支持的内置模板变量（值在发送前展开）
HEADER_TEMPLATE_KEYS = ("session_id", "conversation_id", "workspace", "model", "provider")


def expand_header_templates(
    headers: Optional[Any], context: Optional[Dict[str, str]] = None
) -> Dict[str, str]:
    """展开 custom_headers 中的 {var} 模板（var ∈ HEADER_TEMPLATE_KEYS）。

    - 无模板的静态头原样保留；
    - 展开后为空值的头被丢弃（如无会话上下文时 {conversation_id} → 空，
      避免把空 header 发给服务端）。
    context 缺省时读取当前任务的 header_context（AgentLoop.run_task 设置）。
    """
    cleaned = clean_custom_headers(headers)
    if not cleaned:
        return {}
    ctx = context if context is not None else dict(header_context.get())
    out: Dict[str, str] = {}
    for key, value in cleaned.items():
        had_template = False
        for name in HEADER_TEMPLATE_KEYS:
            token = "{" + name + "}"
            if token in value:
                had_template = True
                value = value.replace(token, ctx.get(name, ""))
        if had_template and not value.strip():
            continue
        out[key] = value.strip() if had_template else value
    return out


def merge_headers(defaults: Dict[str, str], custom: Optional[Any]) -> Dict[str, str]:
    """按 HTTP 规范以大小写不敏感方式合并请求头。

    自定义头覆盖同名默认头，同时避免 ``Authorization`` 与
    ``authorization`` 这样的逻辑重复键进入请求。
    """
    merged: Dict[str, str] = dict(defaults)
    key_index = {key.lower(): key for key in merged}
    for key, value in clean_custom_headers(custom).items():
        old_key = key_index.get(key.lower())
        if old_key is not None:
            del merged[old_key]
        merged[key] = value
        key_index[key.lower()] = key
    return merged


class LLMError(Exception):
    """LLM 调用异常。

    retryable=True 表示瞬时故障（超时 / 网络中断 / 限流 / 服务端 5xx），
    AgentLoop 会对这类错误自动退避重试，而不是直接终止任务。
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


# 瞬时性 HTTP 状态码：请求本身没问题，稍后重发可能成功
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


class BaseLLMAdapter:
    """LLM 适配器抽象接口。

    chat_stream(messages, tools) -> (content, tool_calls, usage)
    流式输出通过事件总线以 "llm:stream" 事件实时广播。
    usage 为模型返回的 token 统计（准确值），无返回时可为 None：
      {prompt_tokens, completion_tokens, prompt_cache_hit_tokens}

    reasoning_effort：推理强度控制（"low" / "medium" / "high"，空串表示关闭）。
    不同供应商以不同方式实现：OpenAI 兼容接口透传 reasoning_effort 字段，
    Anthropic 映射为 thinking 的 budget_tokens。
    """

    name = "base"
    provider_id = ""

    def __init__(self, reasoning_effort: str = "", **kwargs) -> None:
        self.reasoning_effort = (reasoning_effort or "").strip().lower()

    async def chat_stream(
        self,
        messages: List[Message],
        tools: List[ToolDefinition],
        events: Optional[TypedEventBus] = None,
    ) -> Tuple[str, List[ToolCall], Optional[Dict[str, Any]]]:
        raise NotImplementedError

    async def test_connection(self) -> Tuple[bool, str, float]:
        """测试连接，返回 (是否成功, 消息, 延迟ms)。"""
        raise NotImplementedError
