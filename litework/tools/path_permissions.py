# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""批量目录授权（Goals 全局视图配套）。

设计动机：跨项目任务（如「整理两次旅游的照片」）需要读多个项目外的目录。
逐路径触发审批会把授权打断成 N 次（Agent 边做边要权限，要求很强的统筹
能力）——改为 **开工前统一盘点**：Agent 规划第一步列出全部所需目录，经
request_path_permissions 一次性申请，用户一张卡批量批准。

授权落地复用既有「记住并允许同类」机制（path_prefix 规则，读写分离），
后续 read_file/write_file 等命中规则免审批；未盘到的路径照旧走单路径审批
兜底（现状行为不退化）。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

from ..core.types import Plugin, ToolDefinition


class PathPermissionsPlugin(Plugin):
    """request_path_permissions：一次列出全部所需目录，批量申请授权。"""

    name = "path-permissions-plugin"

    def __init__(self, app) -> None:
        self._app = app

    def get_tools(self) -> List[ToolDefinition]:
        return [ToolDefinition(
            name="request_path_permissions",
            description=(
                "批量申请项目外目录的访问授权（跨项目任务的开工第一步）。"
                "用法：在开始执行前，先盘点本任务需要读/写的**全部**项目外目录，"
                "一次性列出统一申请；用户批准后这些目录的后续访问免审批。"
                "规则：①规划阶段完成盘点，不要边做边逐个申请；"
                "②每项注明 access：read（只读）/ write（读写，含写）；"
                "③workspace 内的路径不需要申请；"
                "④批准后本会话内有效，读写按申请的权限分别生效。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "description": "需要授权的目录列表（绝对路径）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string", "description": "目录绝对路径"},
                                "access": {
                                    "type": "string",
                                    "enum": ["read", "write"],
                                    "description": "read=只读授权 / write=读写授权",
                                },
                                "reason": {"type": "string", "description": "为什么需要访问（展示给用户）"},
                            },
                            "required": ["path", "access"],
                        },
                    },
                },
                "required": ["paths"],
            },
        )]

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        if name != "request_path_permissions":
            return f'[Error]: 未知工具 "{name}"。'
        from .todos import current_session_id

        session_id = current_session_id.get("")
        if not session_id:
            return "[Error]: 无会话上下文，无法申请路径授权。"

        raw = args.get("paths") or []
        items: List[Dict[str, Any]] = []
        for it in raw:
            if not isinstance(it, dict):
                continue
            path = str(it.get("path") or "").strip()
            access = str(it.get("access") or "read").strip()
            if not path:
                continue
            if access not in ("read", "write"):
                access = "read"
            items.append({
                "path": os.path.abspath(os.path.expanduser(path)),
                "access": access,
                "reason": str(it.get("reason") or ""),
            })
        if not items:
            return "[Error]: paths 为空——没有需要授权的目录。"

        # 逐项过审批门（同一 approval_gate；一项拒绝不影响已批准项，
        # 拒绝项在返回中明确列出，Agent 可降级或改用单路径审批兜底）。
        gate = getattr(self._app, "approval_gate", None)
        if gate is None:
            return "[Error]: 审批门不可用。"

        approved: List[Dict[str, str]] = []
        denied: List[Dict[str, str]] = []
        for it in items:
            label = f'{"读写" if it["access"] == "write" else "读取"} {it["path"]}'
            reason = it["reason"] or "跨项目任务需要访问项目外目录"
            future = gate.request_approval(
                f"批量目录授权：{label}",
                f"{reason}（{'读写' if it['access'] == 'write' else '仅读取'}；批准后本会话内免审批）",
                session_id=session_id,
            )
            ok = await future
            if ok:
                # 直接落「记住并允许同类」规则：path_prefix + access，读写分离。
                # tool="*" = 路径通配：read_file/write_file/list_dir 等全部路径型
                # 工具均可命中（matcher 对 "*" 跳过工具名检查，但 path_prefix
                # 仍受 access 与前缀约束；tool_exact 语义对 "*" 显式排除）。
                self._app.remember_approval_rule(session_id, {
                    "tool": "*",
                    "kind": "path_prefix",
                    "pattern": it["path"],
                    "access": it["access"],
                })
                approved.append({"path": it["path"], "access": it["access"]})
            else:
                denied.append({"path": it["path"], "access": it["access"]})

        lines = [f"[Path Permissions]: 批量授权完成——批准 {len(approved)} 项，拒绝 {len(denied)} 项。"]
        for a in approved:
            lines.append(f"  ✓ {a['access']:5s} {a['path']}")
        for d in denied:
            lines.append(f"  ✗ {d['access']:5s} {d['path']}（用户拒绝；访问时仍会逐路径审批）")
        if approved:
            lines.append("已批准目录的后续访问免审批（本会话内）。")
        return "\n".join(lines)
