# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""核心类型定义（对应课程第10课（插件架构） types.ts）。"""
from __future__ import annotations

import contextvars
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


# ---------------------------------------------------------------- 消息类型

Role = str  # "system" | "user" | "assistant" | "tool"


def now_ms() -> int:
    """当前 Unix 毫秒（消息 ts / 会话时间戳统一口径）。

    集中一处便于测试打桩（monkeypatch litework.core.types.now_ms），
    也避免各调用点各写一遍 int(time.time() * 1000)。
    """
    return int(time.time() * 1000)


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
    # 消息产生时刻（Unix 毫秒；前端气泡右下角显示到秒）。None = 本字段之前
    # 落盘的老消息，前端不显示时间而不是伪造当前时间。
    #
    # 为什么放后端而不是前端打点：前端气泡时间会被三种情况弄错——切页签重挂载、
    # 刷新后从 /api/sessions/<id> 重拉历史（全部盖成拉取时刻）、任务结束重拉快照
    # 覆盖流式期间的本地消息。时间必须在消息诞生时定死并随会话落盘。
    #
    # 与 agent 同样的纪律：内部字段，不出网（见 openai_compat._wire_message）。
    ts: Optional[int] = None

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
        if self.ts:
            d["ts"] = self.ts
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Message":
        tcs = data.get("tool_calls")
        ts = data.get("ts")
        return cls(
            role=data.get("role", "user"),
            content=data.get("content"),
            name=data.get("name"),
            tool_calls=[ToolCall.from_dict(t) for t in tcs] if tcs else None,
            tool_call_id=data.get("tool_call_id"),
            agent=data.get("agent"),
            # 只认正数：历史快照可能没有该字段，手改的脏值（字符串/bool）按缺失处理
            ts=int(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool)
            and ts > 0 else None,
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

    def settings_action(self, key: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """设置页动作按钮（type: "action" 字段点击时触发）。

        可执行网络请求 / 写配置（与纯读钩子不同）。返回
        {"ok": bool, "message": str}，前端原样回显 message。
        动作只能由用户显式点击触发，不受纯读约定约束。
        """
        return {"ok": False, "message": "该插件未实现此动作"}

    def on_config_changed(self, config: Dict[str, Any]) -> None:
        """主机保存了**本插件命名空间**的配置后回调（可选，用于热生效）。

        什么时候必须实现它：插件在 `install()` / `__init__` 里把配置**拷成成员**
        或据此改模块级状态（限流速率、缓存 TTL、连接参数…）。这类值不会随
        `app.config` 更新而变，没有这个钩子就只能重启 Core。

        不实现也没有害处——主机对未实现的插件完全不调用（默认空实现），也不会
        因为它而阻塞配置保存（异常被捕获并记日志，配置已落盘）。

        `config` 是**全量**配置（读自己的命名空间：`config[self.name]`）。
        纯读约定不适用：这里可以重建缓存/客户端，但应快速返回，不要发网络请求
        （保存配置是同步调用路径）。
        """
        return None
