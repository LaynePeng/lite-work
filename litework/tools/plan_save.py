# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""plan_save：Plan Agent 专属的计划文件写入通道。

只允许写 <workspace>/.lite-work/plans/<session_id>.md（路径服务端硬编码，
不接受用户传入路径参数）——Plan 不拥有通用 write_file，但规划产出需要
落盘供执行型 Agent 交接，这是唯一且安全的写入口。
"""
from __future__ import annotations

import os
from typing import Any, Dict

from ..core.agent_loop import current_session_id
from ..core.types import ToolDefinition


def make_plan_save_handler(app):
    async def _handler(args: Dict[str, Any]) -> str:
        content = str(args.get("content") or "").strip()
        if not content:
            return "[Error]: content 不能为空。"
        try:
            sid = current_session_id.get()
        except LookupError:
            return "[Error]: 无活动会话。"
        if not sid:
            return "[Error]: 无活动会话。"
        workspace = app.workspace
        if not workspace:
            return "[Error]: 工作区未就绪。"
        safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in sid)[:80]
        if not safe or safe in (".", ".."):
            return "[Error]: 非法的会话标识。"
        plans_dir = os.path.join(workspace, ".lite-work", "plans")
        os.makedirs(plans_dir, exist_ok=True)
        path = os.path.join(plans_dir, f"{safe}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content if content.endswith("\n") else content + "\n")
        return f"[OK] 计划已写入 {path}（供后续切换到执行型 Agent 时直接落地）"

    return _handler


def get_plan_save_tool() -> ToolDefinition:
    return ToolDefinition(
        name="plan_save",
        description=(
            "把本会话的规划产出保存为计划文件（.lite-work/plans/<session>.md），"
            "供后续切换到执行型 Agent 时直接落地。仅 Plan Agent 可用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "完整计划内容（Markdown，含可执行步骤）。注意：本工具是整体重写，如需增量请传入完整内容。",
                },
            },
            "required": ["content"],
        },
    )