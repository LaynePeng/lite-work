"""洋葱模型中间件管道（对应课程第8课 Skills / 第10课插件架构）。"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, List

from .types import Context, Middleware, NextFn

Callback = Callable[[Any, Any, Callable], Any]


class Pipeline:
    """洋葱模型管道：每个中间件可决定是否调用 next() 继续向下流转。

    支持同步与异步中间件；next() 可携带更新后的数据继续传递。
    """

    def __init__(self, name: str = "pipeline") -> None:
        self.name = name
        self._middlewares: List[Middleware] = []

    def use(self, middleware: Callback) -> None:
        self._middlewares.append(middleware)

    async def run(self, ctx: Context, initial_data: Any) -> Any:
        async def dispatch(index: int, data: Any) -> Any:
            if index >= len(self._middlewares):
                return data
            middleware = self._middlewares[index]

            async def next_call(next_data: Any = None) -> Any:
                return await dispatch(index + 1, next_data if next_data is not None else data)

            result = middleware(ctx, data, next_call)
            if asyncio.iscoroutine(result):
                result = await result
            return result

        return await dispatch(0, initial_data)