# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""内核 Kernel（对应课程第10课（插件架构））。"""
from __future__ import annotations

import logging
from typing import Any, Dict

from .events import TypedEventBus
from .pipeline import Pipeline
from .types import Context, Message, Plugin

logger = logging.getLogger("litework.kernel")


class Kernel:
    """空间解耦的核心：只维护 Context / 事件总线 / 三个阶段的拦截管道。

    - before_llm    : LLM 调用前的消息修改（动态 Prompt 注入、记忆补充）
    - before_tool   : 工具执行前的安全审查与权限判决
    - after_tool    : 工具执行后的结果修饰（截断、格式化）
    """

    def __init__(self, session_id: str) -> None:
        self.events = TypedEventBus()
        self.ctx = Context(session_id=session_id)
        self.before_llm = Pipeline("before_llm")
        self.before_tool = Pipeline("before_tool")
        self.after_tool = Pipeline("after_tool")
        self._plugins: Dict[str, Plugin] = {}

    @property
    def session_id(self) -> str:
        return self.ctx.session_id

    def use(self, plugin: Plugin) -> "Kernel":
        if plugin.name in self._plugins:
            logger.warning('[Kernel] Plugin "%s" already registered.', plugin.name)
            return self
        self._plugins[plugin.name] = plugin
        plugin.install(self)
        logger.info('[Kernel] Plugin "%s" loaded.', plugin.name)
        return self

    def register_service(self, name: str, service: Any) -> None:
        self.ctx.services[name] = service

    def get_service(self, name: str) -> Any:
        service = self.ctx.services.get(name)
        if service is None:
            raise KeyError(f'[Kernel] Service "{name}" is not registered.')
        return service

    def has_service(self, name: str) -> bool:
        return name in self.ctx.services

    def push_message(self, message: Message) -> None:
        self.ctx.messages.append(message)