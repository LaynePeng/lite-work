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
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # 兜底：非运行中的 loop（老式调用方）
            loop = asyncio.get_event_loop()
        approval_id = f"apv_{next(self._ids)}"
        future: asyncio.Future = loop.create_future()

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
        logger.info("[Approval] %s 等待人工确认: %s", approval_id, action[:120])

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
        future = entry["future"]
        if not future.done():
            # call_soon_threadsafe：无论 resolve 来自哪个线程/loop 都能正确唤醒
            # 等待中的协程（直接 set_result 在跨线程场景下回调不会被执行，
            # 表现为「用户点了允许但 Agent 永远卡住」）
            def _set(f: asyncio.Future = future, v: bool = approved) -> None:
                if not f.done():
                    f.set_result(v)
            try:
                future.get_loop().call_soon_threadsafe(_set)
            except RuntimeError:
                logger.warning("[Approval] %s 所在 loop 已关闭，无法唤醒等待方", approval_id)
        logger.info("[Approval] %s 已%s（by=%s）", approval_id,
                    "批准" if approved else "拒绝", by)
        return True

    def get_pending_info(self, approval_id: str) -> Optional[Dict[str, Any]]:
        entry = self._pending.get(approval_id)
        if entry is None:
            return None
        return {"id": entry["id"], "action": entry["action"], "reason": entry["reason"],
                "created_at": entry["created_at"]}

    def pending_count(self) -> int:
        return len(self._pending)