"""强类型异步事件总线（对应课程第10课（插件架构） TypedEventEmitter，asyncio 版）。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Set

logger = logging.getLogger("litework.events")

Listener = Callable[..., Any]


class TypedEventBus:
    """支持 async/同步监听器的强类型事件总线。

    - 事件名与负载类型集中声明（EVENT_MAP），杜绝拼写错误
    - emit 时按注册顺序依次 await 所有监听器，单点异常不影响整体
    """

    EVENT_MAP: Dict[str, Any] = {
        "session:start": dict,
        "session:end": dict,
        "message:added": dict,
        "llm:stream": dict,
        "llm:turn_start": dict,
        "tool:before_execute": dict,
        "tool:after_execute": dict,
        "approval:request": dict,
        "approval:resolved": dict,
        "task:start": dict,
        "task:done": dict,
        "task:error": dict,
        "task:stop": dict,
        "stats:update": dict,
        "context:stats": dict,
        "subagent:started": dict,
        "subagent:progress": dict,
        "subagent:completed": dict,
        "skill:loaded": dict,
        # 交互类：ask_user 提问 / TODO 看板（TaskHandle 订阅转发到前端）
        "question:request": dict,
        "question:resolved": dict,
        "todo:updated": dict,
        "agent:spawned": dict,
        "agent:closed": dict,
    }

    def __init__(self) -> None:
        self._listeners: Dict[str, Set[Listener]] = {}

    def on(self, event: str, listener: Listener) -> "TypedEventBus":
        if event not in self.EVENT_MAP:
            logger.warning("[EventBus] Unknown event name: %s", event)
        self._listeners.setdefault(event, set()).add(listener)
        return self

    def off(self, event: str, listener: Listener) -> None:
        s = self._listeners.get(event)
        if s:
            s.discard(listener)

    async def emit(self, event: str, data: Any = None) -> None:
        listeners = list(self._listeners.get(event, ()))
        for listener in listeners:
            try:
                result = listener(data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("[EventBus] Listener error on event %s", event)
