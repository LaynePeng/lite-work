"""核心类型定义（对应课程第10课（插件架构） types.ts）。"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


# ---------------------------------------------------------------- 消息类型

Role = str  # "system" | "user" | "assistant" | "tool"

# 当前任务/会话的请求头模板上下文（custom_headers 的 {var} 插值数据源）。
# 由 AgentLoop.run_task 在每个任务开始时设置；适配器 _headers() 读取后展开模板。
# 键：session_id / conversation_id / workspace / model / provider（值均为 str）。
header_context: contextvars.ContextVar[Dict[str, str]] = contextvars.ContextVar(
    "header_context", default={}
)


@dataclass
class ToolCall:
    id: str
    type: str = "function"
    name: str = ""
    arguments: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "function": {"name": self.name, "arguments": self.arguments},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ToolCall":
        fn = data.get("function") or {}
        return cls(
            id=data.get("id", ""),
            type=data.get("type", "function"),
            name=fn.get("name", ""),
            arguments=fn.get("arguments", ""),
        )


@dataclass
class Message:
    role: Role
    content: Optional[str] = None
    name: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    tool_call_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            d["name"] = self.name
        if self.tool_calls:
            d["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Message":
        tcs = data.get("tool_calls")
        return cls(
            role=data.get("role", "user"),
            content=data.get("content"),
            name=data.get("name"),
            tool_calls=[ToolCall.from_dict(t) for t in tcs] if tcs else None,
            tool_call_id=data.get("tool_call_id"),
        )


# ---------------------------------------------------------------- 工具类型


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: Dict[str, Any]


# ---------------------------------------------------------------- 上下文


@dataclass
class Context:
    session_id: str
    messages: List[Message] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    services: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- 中间件

NextFn = Callable[["Context", Any], "Any"]
Middleware = Callable[[Context, Any, Callable], Any]


class NextMiddleware:
    """包装 next 调用的辅助类，兼容异步/同步中间件。"""

    def __init__(self, fn: Callable) -> None:
        self._fn = fn

    def __call__(self, data: Any = None) -> Any:
        return self._fn(data)


# ---------------------------------------------------------------- 插件

PluginInstallFn = Callable[[Any], None]


class Plugin:
    name: str = "plugin"

    def install(self, kernel: Any) -> None:  # pragma: no cover - 抽象基类
        pass