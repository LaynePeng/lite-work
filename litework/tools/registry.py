# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""工具注册表（Tool Registry）：统一注册 / 汇总 Schema / 分发执行。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List

from ..core.types import ToolDefinition

logger = logging.getLogger("litework.tools")

Handler = Callable[..., Awaitable[str]]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, ToolDefinition] = {}
        self._handlers: Dict[str, Handler] = {}

    def register(self, name: str, description: str, parameters: Dict[str, Any], handler: Handler) -> None:
        self._tools[name] = ToolDefinition(name=name, description=description, parameters=parameters)
        self._handlers[name] = handler

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)
        self._handlers.pop(name, None)

    def get_tools(self) -> List[ToolDefinition]:
        return list(self._tools.values())

    def has(self, name: str) -> bool:
        return name in self._tools

    def set_handler(self, name: str, handler: Handler) -> None:
        if name not in self._tools:
            raise KeyError(f'未注册的工具 "{name}"')
        self._handlers[name] = handler

    def names(self) -> List[str]:
        return list(self._tools.keys())

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        handler = self._handlers.get(name)
        if handler is None:
            return f'[Error]: 未注册的工具 "{name}"。'
        try:
            # handler 既可能是协程函数也可能是普通函数（都在此处统一调度）：
            # 标注 Any 让 mypy 明白分支重赋值后类型收窄，而不是 Awaitable[str] 残留
            result: Any = handler(args)
            if asyncio.iscoroutine(result):
                result = await result
            elif callable(result):
                # 同步 handler 一律丢线程池：阻塞实现（长 CPU / 同步 I/O /
                # 卡住的子进程等待）不再冻住事件循环——否则 SSE 心跳、HTTP
                # 服务全部无响应，且 agent_loop 的 wait_for 超时永远无法触发
                # （事件循环线程被占住，超时协程得不到调度）。这是「调用工具
                # 整个应用卡死」类问题的架构级兜底。
                result = await asyncio.to_thread(result)
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[Tool] %s 执行异常", name)
            return f"[Execution Exception]: {exc}"
