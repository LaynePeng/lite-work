# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""安全中间件插件（对应课程第18课（安全沙箱实战） SecurityPlugin，Web 审批版）。

挂载到 Kernel.beforeTool 管道：
- 文件/编辑工具 → 敏感路径检查（HIGH 阻断）
- execute_command → 高危黑名单（HIGH 阻断）/ 中危（Web 审批）/ 白名单放行
- sudo 提权、强制推送、删库等中危操作 → 弹审批卡等待用户确认
"""
from __future__ import annotations

import logging
import os
from typing import Any

from ..core.kernel import Kernel
from ..core.types import Plugin
from .approval import ApprovalGate
from .guard import SecurityCheckResult, SecurityGuard, ThreatLevel

logger = logging.getLogger("litework.security")


class SecurityPlugin(Plugin):
    name = "security-plugin"

    def __init__(self, guard: SecurityGuard, approval_gate: ApprovalGate, workspace: str,
                 skill_perm_resolver=None) -> None:
        self.guard = guard
        self.approval_gate = approval_gate
        self.workspace = os.path.abspath(workspace)
        # 技能权限规则解析器（返回 {glob: allow/deny/ask}），None 时 load_skill 不做权限过滤
        self.skill_perm_resolver = skill_perm_resolver
        # 受信路径（免"项目外"审批）：技能目录是产品自身的合法位置——
        # load_skill 返回技能目录后 Agent 读脚本/执行渲染是设计内行为，
        # 不该被当敏感路径拦截。包含用户级 ~/.agents/skills 与安装包内置
        # 技能目录（LITEWORK_SKILLS_SOURCE 指向）。
        self._trusted_prefixes = self._collect_trusted_skill_paths()

    def _collect_trusted_skill_paths(self) -> list:
        prefixes = []
        seen = set()

        def _add(p: str) -> None:
            p = os.path.abspath(os.path.expanduser(p))
            if p and p not in seen:
                seen.add(p)
                prefixes.append(p)

        _add(os.path.join("~", ".agents", "skills"))
        src = os.environ.get("LITEWORK_SKILLS_SOURCE", "")
        if src:
            _add(src)
        return prefixes

    def _is_trusted_skill_path(self, path: str) -> bool:
        """路径位于受信技能目录内（读取/执行免审批；写入仍走审批）。"""
        try:
            raw = os.path.abspath(os.path.expanduser(path or ""))
        except Exception:
            return False
        for prefix in self._trusted_prefixes:
            if raw == prefix or raw.startswith(prefix + os.sep):
                return True
        return False

    def install(self, kernel: Kernel) -> None:
        @kernel.before_tool.use
        async def _middleware(ctx, data, next):
            tool_name = data.get("toolName", "")
            args = data.get("args", {}) or {}

            # 技能权限（对齐 OpenCode permission.skill）：deny 拒绝 / ask 审批
            if tool_name == "load_skill" and self.skill_perm_resolver is not None:
                action = self._skill_action(str(args.get("skillName") or ""))
                if action == "deny":
                    data["cancel"] = True
                    data["reason"] = (f"[Skill Denied]: 技能 {args.get('skillName')!r} "
                                      f"被权限规则禁用（skill_permissions）")
                    return await next(data)
                if action == "ask":
                    skill_name = str(args.get("skillName") or "")
                    approved = await self._request_approval(
                        kernel, f'load_skill("{skill_name}")',
                        "该技能的权限规则为 ask，加载前需要确认。",
                    )
                    if not approved:
                        data["cancel"] = True
                        data["reason"] = "[User Rejected]: 技能加载已被拒绝。"
                        return await next(data)

            # 路径型工具过滤
            path_result = self.guard.check_tool(tool_name, args)
            if path_result.level == ThreatLevel.HIGH:
                data["cancel"] = True
                data["reason"] = f"[SecurityGuard]: {path_result.reason}"
                return await next(data)

            # 项目外文件默认不访问。读取和写入是两种独立授权，且授权只
            # 附着于本次调用的精确路径，不能被后续调用复用或升级。
            if tool_name in {
                "read_file", "list_dir", "get_file_outline", "read_focused_symbol",
            } or tool_name in {"write_file", "apply_search_replace", "apply_unified_diff"}:
                path = args.get("filePath") or args.get("path") or ""
                if path and self.guard.is_external_path(self.workspace, path):
                    # 受信技能目录（用户级/安装包内置）的读取免审批：Agent
                    # 读技能脚本是设计内行为；写入仍需单独授权
                    if (not tool_name in {"write_file", "apply_search_replace", "apply_unified_diff"}
                            and self._is_trusted_skill_path(path)):
                        pass  # 放行读取
                    else:
                        write = tool_name in {"write_file", "apply_search_replace", "apply_unified_diff"}
                        access = "写入" if write else "读取"
                        approved = await self._request_approval(
                            kernel,
                            f'{access}项目外路径 "{path}"',
                            f'工具 {tool_name} 请求{access}项目目录之外的路径。'
                            + ("写入需要单独授权。" if write else "批准后仅允许本次读取。"),
                        )
                        if not approved:
                            data["cancel"] = True
                            data["reason"] = f"[User Rejected]: 项目外路径{access}已被拒绝。"
                            return await next(data)
                        args["_approved_external_access"] = "write" if write else "read"

            # Shell 指令过滤
            if tool_name == "execute_command":
                command = args.get("command", "")
                result: SecurityCheckResult = self.guard.check_shell_command(command)

                if result.level == ThreatLevel.HIGH:
                    data["cancel"] = True
                    data["reason"] = f"[Blocked by SecurityGuard]: {result.reason}"
                    return await next(data)

                if result.level == ThreatLevel.MEDIUM:
                    approved = await self._request_approval(
                        kernel, f'execute_command("{command}")', result.reason or "中危操作"
                    )
                    if not approved:
                        data["cancel"] = True
                        data["reason"] = "[User Rejected]: 操作被操作员明确拒绝。"
                        return await next(data)

            if tool_name.startswith("mcp_"):
                approved = await self._request_approval(
                    kernel,
                    f"调用 MCP 工具 {tool_name}",
                    "MCP 工具由外部进程提供，可能访问文件、网络或其他本地资源。",
                )
                if not approved:
                    data["cancel"] = True
                    data["reason"] = "[User Rejected]: MCP 工具调用已被拒绝。"
                    return await next(data)

            return await next(data)

        kernel.register_service("security_guard", self.guard)

    def _skill_action(self, skill_name: str) -> str:
        """解析技能权限动作（allow/deny/ask），规则解析失败按 allow 放行。"""
        if not skill_name.strip():
            return "allow"
        try:
            from .skill_permissions import resolve
            return resolve(self.skill_perm_resolver() or {}, skill_name)
        except Exception:
            logger.exception("[Security] 技能权限解析失败，按 allow 放行: %s", skill_name)
            return "allow"

    async def _request_approval(self, kernel: Kernel, action: str, reason: str) -> bool:
        future = self.approval_gate.request_approval(action, reason)
        approval_id = self.approval_gate.current_id(future)
        # 广播审批请求，Web UI 弹出确认卡片
        await kernel.events.emit("approval:request", {
            "id": approval_id,
            "action": action,
            "reason": reason,
        })
        approved = await future
        # 广播审批结果，UI 关闭确认卡片
        await kernel.events.emit("approval:resolved", {
            "id": approval_id,
            "approved": approved,
        })
        return approved
