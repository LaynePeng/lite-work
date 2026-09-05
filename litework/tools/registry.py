"""工具注册表（Tool Registry）：统一注册 / 汇总 Schema / 分发执行。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

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
            result = handler(args)
            if asyncio.iscoroutine(result):
                result = await result
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[Tool] %s 执行异常", name)
            return f"[Execution Exception]: {exc}"
