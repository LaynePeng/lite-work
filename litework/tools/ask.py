# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""ask_user 工具：Agent 向用户提问，支持选项选择与自定义输入。"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from ..core.types import ToolDefinition
from .plugin import ToolPlugin

logger = logging.getLogger("litework.tools.ask")


def make_ask_user_handler(question_gate, events=None):
    """构造 ask_user 工具处理器。

    events 为任务 kernel 的事件总线（用于向 UI 广播 question:request/resolved）。
    与 spawn_agent 类似：build_registry 引导阶段注册工具定义时 events 为 None，
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
        multi_select = bool(args.get("multi_select", False))

        # 契约校验（不是软提示）：「选择题」是这个工具的唯一形态。
        # 此前只写「可提供选项」，模型于是常只发一句开放式提问，前端只能渲染成
        # 一个空输入框——用户看到的就是「ask_user 过来的问题不是选择题」。
        # 现在缺选项直接失败并教它怎么改，模型会立即重试成带选项的调用。
        if len(options) < 2:
            return (
                "[Error] ask_user 必须提供 options（至少 2 个互斥选项）。\n"
                "这个工具的用途是让用户「选」而不是「写」：没有选项等于把判断成本推回给用户。\n"
                "正确做法——把可能的方向写成 2-4 个具体选项，用户仍可自定义输入兜底：\n"
                '  ask_user(question="用哪个数据库？", options=["PostgreSQL（功能全，需运维）", '
                '"SQLite（零运维，单机）", "按现有技术栈来"])\n'
                "若确实没有任何可枚举方向（例如「请提供 API Key」「请描述你的需求」），"
                "不要用这个工具，直接在回复里提问即可。"
            )
        if multi_select and len(options) < 3:
            # 只有 2 个选项时「多选」几乎等于单选（全选或其一），模型常把
            # 「二选一」误标成多选。直接拒绝，让它明确语义。
            return (
                "[Error] multi_select=true 至少需要 3 个选项。只有 2 个选项时"
                "用户几乎只会全选、语义上就是单选。\n"
                "若确实是「二选一」，请去掉 multi_select；若是多选，请补足 3 个以上候选。"
            )

        future = question_gate.request(question, options, multi_select=multi_select)
        qid = question_gate.current_id(future)

        if events is not None:
            await events.emit("question:request", {
                "id": qid,
                "question": question,
                "options": options,
                "multi_select": multi_select,
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
                "向用户提一个选择题，并等待回答。**这是选择题工具，不是开放式提问**。\n"
                "\n"
                "什么时候用：用户才能在几个明确方向里挑一个（或几个）时——"
                "技术选型、方案取舍、范围确认、偏好设置。\n"
                "什么时候不用：需要用户写字（密钥、路径、需求描述）→ 直接在回复里问；"
                "能从上下文推断或按默认值推进 → 先做，别打断。\n"
                "\n"
                "硬性要求：\n"
                "1. options 至少 2 个，2-4 个最佳，彼此互斥、具体到能直接执行"
                "（「PostgreSQL（需运维）」优于「数据库 A」）；\n"
                "2. 每个选项一句话说清代价/后果，让用户不用追问就能选；\n"
                "3. multi_select 只在「答案允许多项同时成立」时为 true"
                "（如「要启用哪些功能」「可以改哪些文件」），且此时 options 至少 3 个；"
                "「二选一」一律单选，不要滥用多选；\n"
                "4. 一次只问一个问题。有多个待确认点时，按重要性挑最关键的先问，"
                "其余用合理默认值推进——不要在一轮里堆一串问题。\n"
                "\n"
                "用户可以直接点选项，也可以输入自定义答案（兜底）。"
                "选项要覆盖主要方向，但不能指望用户替你想全。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": (
                            "要用户选择的问题本身。写成一句话的选择题"
                            "（如「登录态用哪种方案？」），不要写成一长段背景说明——"
                            "背景放在正文回复里。"
                        ),
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "description": (
                            "**必填，至少 2 个**互斥选项（2-4 个最佳）。"
                            "每个选项要具体、可执行，并带上代价或后果，"
                            "如「PostgreSQL（功能全，需额外运维）」而不是「方案 A」。"
                        ),
                    },
                    "multi_select": {
                        "type": "boolean",
                        "description": (
                            "仅当答案允许多项同时成立时为 true（此时 options 需 ≥3）。"
                            "默认 false 单选。「二选一」「挑一个方案」都是单选。"
                        ),
                    },
                },
                "required": ["question", "options"],
            },
        )]

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        # 兜底：无注入 events 时直接调用默认 handler（真实任务会经 set_handler 覆盖）
        handler = make_ask_user_handler(self._gate, None)
        return await handler(args)