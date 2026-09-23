# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

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
    # 产生该消息的 Agent id（仅 assistant 消息由 AgentLoop 打标；None = 旧快照/无标记）。
    # 用于会话内 Agent 切换检测：新 Agent 接手时知道历史操作是谁做的。
    agent: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            d["name"] = self.name
        if self.tool_calls:
            d["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.agent:
            d["agent"] = self.agent
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
            agent=data.get("agent"),
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

Middleware = Callable[[Context, Any, Callable], Any]



# ---------------------------------------------------------------- 插件

PluginInstallFn = Callable[[Any], None]


class Plugin:
    name: str = "plugin"

    #: 声明式 UI 贡献（通用插件 UI 协议，可选）：
    #:   {"settings": [{"key","type","label","default","hint","options","secret"}],
    #:    "panels":   [{"id","title","icon"}]}
    #: settings 的 type ∈ str | secret | number | boolean | select | map
    #: （map 用于自定义 HTTP headers 这类键值表）。
    #: 设置页与右栏有通用渲染器消费它——插件**无需改前端**即可带配置项与面板。
    contributes: Dict[str, Any] = {}

    def install(self, kernel: Any) -> None:  # pragma: no cover - 抽象基类
        pass

    # -------------------------------------------------- 可选 UI 钩子（未实现则对应 UI 不出现）

    def status_from_config(self, config: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """当前配置下的运行状态（设置页据此显示「未启动 + 原因」）。

        返回 {"state": "running"|"not_started", "reason": "..."}；None = 不声明状态。
        **纯读约定**：不得发网络请求、不得写盘（调用方已对异常兜底）。
        """
        return None

    def panel_content(self, panel_id: str, config: Dict[str, Any]) -> str:
        """面板内容的 Markdown（右栏通用渲染器显示）。

        同样**纯读 + 快返回**；重活请自行缓存。未实现的面板返回空串即可。
        """
        return ""