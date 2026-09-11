# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""强类型异步事件总线（对应课程第10课（插件架构） TypedEventEmitter，asyncio 版）。

EVENT_MAP 值为带结构的 TypedDict，emit() 做运行时负载校验。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import (
    Any,
    Dict,
    List,
    Optional,
    TypedDict,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

logger = logging.getLogger("litework.events")

Listener = Any  # Callable[..., Any]，同步或 async 均可


# ---------------------------------------------------------------- 负载定义
# 每个事件的 payload 结构：required 键必须存在且类型匹配；继承式
# （total=False）声明的键为可选键，存在时才校验。字段与各 emit 调用点
# 一一对应，漂移会在 emit 时被运行时校验捕获。


class MessageAddedPayload(TypedDict):
    message: Dict[str, Any]


class LLMStreamPayload(TypedDict):
    chunk: str


class LLMTurnStartPayload(TypedDict):
    turn: int


class LLMRetryPayload(TypedDict):
    attempt: int
    max_retries: int
    reason: str
    wait: int


class ToolBeforeExecutePayload(TypedDict):
    toolName: str
    args: Dict[str, Any]
    callId: str


class ToolAfterExecutePayload(TypedDict):
    toolName: str
    durationMs: int
    callId: str
    status: str
    result: str


class ApprovalRequestPayload(TypedDict):
    id: str
    action: str
    reason: str


class ApprovalResolvedPayload(TypedDict):
    id: str
    approved: bool


class TaskStartPayload(TypedDict):
    session_id: str


class TaskDonePayload(TypedDict):
    content: str
    stats: Dict[str, Any]


class TaskErrorPayload(TypedDict):
    message: str


class StatsUpdatePayload(TypedDict):
    input_tokens: int
    output_tokens: int
    tool_calls: int
    turns: int
    blocked: int
    cache_hit_tokens: int
    cache_miss_tokens: int
    cost_estimate: float
    status: str


class ContextStatsPayload(TypedDict):
    model: str
    context_window: int
    task: Dict[str, Any]
    # 实际计费单价（每 M token，美元）：面板展示成本依据（models.dev 或配置回退价）
    pricing: Dict[str, float]


class SubagentStartedPayload(TypedDict):
    task: str
    role: str
    subagentId: str
    nickname: str
    mode: str
    callId: Optional[str]  # spawn_agent（非工具调用）派生时无 callId


class SubagentProgressPayload(TypedDict, total=False):
    """subagent:progress：转发子 Agent 的各类进度（按 kind 分字段）。"""
    subagentId: str
    role: str
    kind: str
    callId: Optional[str]  # 直接调用 run_task（非工具派生）时无 callId
    turn: int
    tool: str
    brief: str
    status: str
    durationMs: int
    text: str


class SubagentCompletedPayload(TypedDict):
    task: str
    role: str
    subagentId: str
    nickname: str
    mode: str
    changed_files: List[str]
    callId: Optional[str]  # spawn_agent（非工具调用）派生时无 callId
    tokens_used: int
    turns: int
    summary: str


class SkillLoadedPayload(TypedDict):
    names: List[str]


class QuestionRequestPayload(TypedDict):
    id: str
    question: str
    options: List[str]


class QuestionResolvedPayload(TypedDict):
    id: str
    answer: str


class TodoUpdatedPayload(TypedDict):
    todos: List[Dict[str, Any]]


class AgentSpawnedPayload(TypedDict):
    agentId: str
    nickname: Optional[str]
    role: str
    task: str
    allowedDirs: Optional[List[str]]  # 并行派发无目录隔离时为 None
    mode: str
    model: Optional[str]


class AgentClosedPayload(TypedDict):
    agentId: str


class OpenPayload(TypedDict, total=False):
    """保留事件（当前内核无 emit 点）：仅校验 payload 是 dict。"""


class SessionStartPayload(OpenPayload):
    pass


class SessionEndPayload(OpenPayload):
    pass


class TaskStopPayload(OpenPayload):
    pass


class ChatQueuedPayload(TypedDict):
    text: str
    count: int


# ---------------------------------------------------------------- 校验


class EventPayloadError(TypeError):
    """事件负载与 TypedDict 声明不符（缺字段 / 类型漂移）。"""


def _check_type(value: Any, annotation: Any) -> bool:
    if annotation is Any or annotation is object:
        return True
    if annotation is None or annotation is type(None):
        return value is None
    origin = get_origin(annotation)
    if origin is Union:
        return any(_check_type(value, a) for a in get_args(annotation))
    if origin in (list, List):
        if not isinstance(value, list):
            return False
        (elem,) = get_args(annotation) or (Any,)
        return all(_check_type(v, elem) for v in value)
    if origin in (dict, Dict):
        if not isinstance(value, dict):
            return False
        args = get_args(annotation)
        if len(args) == 2 and args[1] is not Any:
            return all(_check_type(v, args[1]) for v in value.values())
        return True
    if isinstance(annotation, type) and annotation.__name__.endswith("Payload"):
        # 嵌套 TypedDict：按其声明递归校验
        try:
            return _validate_against(value, annotation, strict=False)
        except EventPayloadError:
            return False
    if annotation is bool:
        return isinstance(value, bool)
    if annotation in (int, float):
        # bool 是 int 子类：数值字段不接受 True/False
        return isinstance(value, annotation) and not isinstance(value, bool)
    if isinstance(annotation, type):
        return isinstance(value, annotation)
    return True


def _validate_against(data: Any, payload_type: type, strict: bool = True) -> None:
    if not isinstance(data, dict):
        raise EventPayloadError(
            f"payload 必须是 dict，实际 {type(data).__name__}（声明 {payload_type.__name__}）")
    hints = get_type_hints(payload_type, include_extras=False)
    required = getattr(payload_type, "__required_keys__", frozenset())
    optional = getattr(payload_type, "__optional_keys__", frozenset())
    for key in required:
        if key not in data:
            raise EventPayloadError(
                f"缺少必填字段 {key!r}（声明 {payload_type.__name__}）：{data!r:.200}")
    for key, value in data.items():
        if key in required or key in optional:
            hint = hints.get(key)
            if hint is not None and not _check_type(value, hint):
                raise EventPayloadError(
                    f"字段 {key!r} 期望 {hint}，实际 {type(value).__name__}"
                    f"（声明 {payload_type.__name__}）：{value!r:.100}")
        elif strict:
            raise EventPayloadError(
                f"出现声明外的字段 {key!r}（声明 {payload_type.__name__}）：{data!r:.200}")


def validate_payload(event: str, data: Any, payload_type: Optional[type] = None) -> None:
    """校验事件负载结构，不合规抛 EventPayloadError（供 emit 与测试使用）。"""
    ptype = payload_type
    if ptype is None:
        ptype = TypedEventBus.EVENT_MAP.get(event)
    if ptype is None:
        raise EventPayloadError(f"未注册的事件名: {event!r}")
    _validate_against(data, ptype)


# ---------------------------------------------------------------- 总线


class TypedEventBus:
    """支持 async/同步监听器的强类型事件总线。

    - 事件名与负载类型集中声明（EVENT_MAP），杜绝拼写错误
    - emit 时按注册顺序依次 await 所有监听器，单点异常不影响整体
    - emit 前运行时校验 payload 结构：默认记录错误日志（含 traceback）
      并继续分发；strict 模式（环境变量 LITEWORK_STRICT_EVENTS=1 或
      构造参数）下直接抛出，供测试与开发期 fail-fast
    """

    EVENT_MAP: Dict[str, Any] = {
        "session:start": SessionStartPayload,
        "session:end": SessionEndPayload,
        "message:added": MessageAddedPayload,
        "llm:stream": LLMStreamPayload,
        "llm:turn_start": LLMTurnStartPayload,
        "llm:retry": LLMRetryPayload,
        "tool:before_execute": ToolBeforeExecutePayload,
        "tool:after_execute": ToolAfterExecutePayload,
        "approval:request": ApprovalRequestPayload,
        "approval:resolved": ApprovalResolvedPayload,
        "task:start": TaskStartPayload,
        "task:done": TaskDonePayload,
        "task:error": TaskErrorPayload,
        "task:stop": TaskStopPayload,
        "stats:update": StatsUpdatePayload,
        "context:stats": ContextStatsPayload,
        "subagent:started": SubagentStartedPayload,
        "subagent:progress": SubagentProgressPayload,
        "subagent:completed": SubagentCompletedPayload,
        "skill:loaded": SkillLoadedPayload,
        # 交互类：ask_user 提问 / TODO 看板（TaskHandle 订阅转发到前端）
        "question:request": QuestionRequestPayload,
        "question:resolved": QuestionResolvedPayload,
        "todo:updated": TodoUpdatedPayload,
        "chat:queued": ChatQueuedPayload,
        "agent:spawned": AgentSpawnedPayload,
        "agent:closed": AgentClosedPayload,
    }

    def __init__(self, strict: Optional[bool] = None) -> None:
        if strict is None:
            strict = os.environ.get("LITEWORK_STRICT_EVENTS", "") == "1"
        self.strict = strict
        self._listeners: Dict[str, List[Listener]] = {}

    def on(self, event: str, listener: Listener) -> "TypedEventBus":
        if event not in self.EVENT_MAP:
            logger.warning("[EventBus] Unknown event name: %s", event)
        self._listeners.setdefault(event, []).append(listener)
        return self

    def off(self, event: str, listener: Listener) -> None:
        listeners = self._listeners.get(event)
        if listeners:
            try:
                listeners.remove(listener)
            except ValueError:
                pass

    async def emit(self, event: str, data: Any = None) -> None:
        payload_type = self.EVENT_MAP.get(event)
        if payload_type is not None:
            has_required = bool(getattr(payload_type, "__required_keys__", None))
            if data is not None or has_required:
                try:
                    _validate_against(data, payload_type)
                except EventPayloadError:
                    if self.strict:
                        raise
                    # 生产态：完整记录（含 traceback），事件继续分发——
                    # UI 事件结构漂移不应杀死正在运行的 Agent 任务
                    logger.error("[EventBus] payload 校验失败 event=%s", event,
                                 exc_info=sys.exc_info())
        elif data is not None:
            logger.warning("[EventBus] emit 未注册事件 %s（仅警告）", event)
        # 注册顺序依次调用（保留重复注册的同一监听器：_forward 等场景依赖）
        for listener in list(self._listeners.get(event, ())):
            try:
                result = listener(data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("[EventBus] Listener error on event %s", event)
