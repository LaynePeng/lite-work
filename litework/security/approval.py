# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""人机交互审批门（对应课程第18课（安全沙箱实战） HumanApprovalGate，Web 化）。

原课程的 readline 控制台确认升级为 asyncio.Future 挂起：
- AgentLoop 调用 request_approval 挂起等待
- Server 通过 SSE 向 Web UI 广播 approval:request
- 用户在 UI 点击允许/拒绝 → POST /api/approve → Future 被 resolve
- 带超时保护：超时未确认自动拒绝，避免任务永久挂起
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("litework.approval")


class ApprovalGate:
    def __init__(self, timeout_seconds: float = 600.0) -> None:
        self.timeout_seconds = timeout_seconds
        self._ids = itertools.count(1)
        self._pending: Dict[str, Dict[str, Any]] = {}

    def request_approval(
        self, action: str, risk_reason: str, auto_approve: bool = False
    ) -> "asyncio.Future":
        """挂起等待 Web UI 的人工确认，返回 future（await 后得到 bool）。"""
        approval_id = f"apv_{next(self._ids)}"
        future: asyncio.Future = asyncio.get_event_loop().create_future()

        if auto_approve:
            logger.info("[Approval] 自动放行(auto_approve): %s", action[:120])
            future.set_result(True)
            return future

        self._pending[approval_id] = {
            "id": approval_id,
            "action": action,
            "reason": risk_reason,
            "created_at": int(time.time() * 1000),
            "future": future,
        }

        # 超时保护：超过时限未确认，自动拒绝
        async def _timeout_guard() -> None:
            await asyncio.sleep(self.timeout_seconds)
            if not future.done():
                logger.warning("[Approval] %s 审批超时，自动拒绝", approval_id)
                self.resolve(approval_id, approved=False, by="timeout")

        asyncio.ensure_future(_timeout_guard())
        return future

    def current_id(self, future: asyncio.Future) -> str:
        """根据 future 反查审批 ID（用于事件广播）。"""
        for aid, entry in self._pending.items():
            if entry["future"] is future:
                return aid
        return ""

    def resolve(self, approval_id: str, approved: bool, by: str = "user") -> bool:
        entry = self._pending.pop(approval_id, None)
        if entry is None:
            return False
        entry["resolved_by"] = by
        entry["approved"] = approved
        if not entry["future"].done():
            entry["future"].set_result(approved)
        return True

    def get_pending_info(self, approval_id: str) -> Optional[Dict[str, Any]]:
        entry = self._pending.get(approval_id)
        if entry is None:
            return None
        return {"id": entry["id"], "action": entry["action"], "reason": entry["reason"],
                "created_at": entry["created_at"]}

    def pending_count(self) -> int:
        return len(self._pending)