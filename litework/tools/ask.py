# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""ask_user 工具：Agent 向用户提问，支持选项选择与自定义输入。"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..core.types import ToolDefinition
from .plugin import ToolPlugin

logger = logging.getLogger("litework.tools.ask")


def make_ask_user_handler(question_gate, events=None):
    """构造 ask_user 工具处理器。

    events 为任务 kernel 的事件总线（用于向 UI 广播 question:request/resolved）。
    与 spawn_sub_agent 类似：build_registry 引导阶段注册工具定义时 events 为 None，
    create_kernel 装配真实任务内核时通过 set_handler 注入 kernel.events。
    """

    async def _handler(args: Dict[str, Any]) -> str:
        question = str(args.get("question", "")).strip()
        options = args.get("options") or []
        if not question:
            return "[Error] 问题不能为空"
        if not isinstance(options, list):
            options = []
        options = [str(o) for o in options if str(o).strip()]

        future = question_gate.request(question, options)
        qid = question_gate.current_id(future)

        if events is not None:
            await events.emit("question:request", {
                "id": qid,
                "question": question,
                "options": options,
            })

        # 等待用户回答（异步挂起，不阻塞其他协程）
        answer = await future

        if events is not None:
            await events.emit("question:resolved", {
                "id": qid,
                "answer": answer,
            })

        return f"[用户回答] {answer}"

    return _handler


class QuestionPlugin(ToolPlugin):
    """ask_user 工具插件：注册工具定义（实际 handler 由 create_kernel 注入真实 events）。"""

    name = "question-plugin"
    version = "1.0.0"

    def __init__(self, question_gate) -> None:
        self._gate = question_gate

    def install(self, kernel) -> None:
        """注册 ask_user 工具（handler 绑定默认实现，create_kernel 会重设为真实内核版）。"""
        super().install(kernel)
        kernel.register_service("question_gate", self._gate)

    def get_tools(self) -> List[ToolDefinition]:
        return [ToolDefinition(
            name="ask_user",
            description=(
                "向用户提问，并等待用户回答。"
                "可提供选项列表供用户选择，用户也可输入自定义回答。"
                "多个问题可同时提出，用户会逐个回答。"
                "适用于需要用户确认、选择或提供信息才能继续的场景。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "要向用户提出的问题，清晰说明需要用户提供什么信息",
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选的选项列表，用户可直接选择其中之一，也可自定义输入",
                    },
                },
                "required": ["question"],
            },
        )]

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        # 兜底：无注入 events 时直接调用默认 handler（真实任务会经 set_handler 覆盖）
        handler = make_ask_user_handler(self._gate, None)
        return await handler(args)