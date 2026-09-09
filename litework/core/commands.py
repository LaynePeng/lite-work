# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""斜杠命令系统：/skill 显式加载、/help 帮助。

命令表 = 内置命令 + 技能派生命令（每个技能一条 /<name>）。
展开发生在任务启动前（TaskManager.start），/skill 展开为 system prompt
的附加技能段（任务级、不进会话历史、缓存前缀稳定）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

BUILTIN_COMMANDS: List[Dict[str, str]] = [
    {
        "name": "skill",
        "description": "显式加载一个技能并在本任务中生效",
        "argsHint": "<name> [需求描述]",
        "kind": "builtin",
    },
    {
        "name": "compact",
        "description": "手动压缩当前会话上下文（旧轮次摘要化，最近几轮原样保留）",
        "argsHint": "[关注点]",
        "kind": "builtin",
    },
    {
        "name": "goal",
        "description": "设置会话目标：注入每个任务的系统提示，聚焦长程任务（前端本地处理）",
        "argsHint": "<目标描述> · 查看用 /goal · 清除用 /goal clear",
        "kind": "builtin",
    },
    {
        "name": "loop",
        "description": "目标自动循环：任务结束后自动推进直至目标完成（前端本地处理）",
        "argsHint": "[最大轮数，默认10] · 关闭用 /loop off",
        "kind": "builtin",
    },
    {
        "name": "help",
        "description": "显示可用命令（前端本地处理，不消耗 LLM）",
        "argsHint": "",
        "kind": "builtin",
    },
    {
        "name": "history",
        "description": "查看历史输入记录，点击回填输入框（前端本地处理，不消耗 LLM）",
        "argsHint": "",
        "kind": "builtin",
    },
]


def build_command_list(skills: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, str]]:
    """内置命令 + 技能派生命令（/skill-name）。"""
    out = [dict(c) for c in BUILTIN_COMMANDS]
    for skill in skills or []:
        name = skill.get("name") or ""
        if not name or name in ("skill", "help"):
            continue
        out.append({
            "name": name,
            "description": skill.get("description") or "技能",
            "argsHint": "[需求描述]",
            "kind": "skill",
        })
    return out


def parse_skill_command(prompt: str) -> Optional[Dict[str, str]]:
    """解析 `/skill <name> [需求]`，非该命令返回 None。

    仅匹配 prompt 起始位置；`/help` 由前端本地处理不会到达这里，
    未知 `/xxx` 原样透传给 Agent（不拦截）。
    """
    text = (prompt or "").lstrip()
    if not text.startswith("/"):
        return None
    parts = text[1:].split(None, 2)
    if not parts or parts[0].lower() != "skill":
        return None
    if len(parts) < 2:
        return {"name": "", "requirement": ""}
    return {"name": parts[1].strip(), "requirement": parts[2].strip() if len(parts) > 2 else ""}
