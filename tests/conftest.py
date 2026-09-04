"""共享测试工具：Mock LLM 适配器。"""
from __future__ import annotations

from typing import List, Optional, Tuple

from litework.core.events import TypedEventBus
from litework.core.types import Message, ToolCall, ToolDefinition


class MockLLMAdapter:
    """脚本化 LLM：按调用顺序返回 (content, tool_calls)。"""

    def __init__(self, responses: List[Tuple[str, List[ToolCall]]]) -> None:
        self.responses = list(responses)
        self.calls: List[int] = []

    async def chat_stream(
        self,
        messages: List[Message],
        tools: List[ToolDefinition],
        events: Optional[TypedEventBus] = None,
    ) -> tuple:
        idx = len(self.calls)
        self.calls.append(idx)
        if idx < len(self.responses):
            content, calls = self.responses[idx]
        else:
            content, calls = "（模拟完成）", []
        if events and content:
            await events.emit("llm:stream", {"chunk": content})
        return content, calls, None


def tool_call(name: str, args_json: str, cid: str = "call_1") -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=args_json)