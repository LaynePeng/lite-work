# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""人机交互审批门（对应课程第18课（安全沙箱实战） HumanApprovalGate，Web 化）。

原课程的 readline 控制台确认升级为 asyncio.Future 挂起：
- AgentLoop 调用 request_approval 挂起等待
- Server 通过 SSE 向 Web UI 广播 approval:request
- 用户在 UI 点击允许/拒绝 → POST /api/approve → Future 被 resolve
- 带超时保护：超时未确认自动拒绝，避免任务永久挂起
- 「记住并允许同类」（Claude Code "Always allow" 模式）：approve 时携带
  remember 标记，把该审批的 rule 写入会话级自动同意规则（见 plugin.py）
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("litework.approval")


class ApprovalGate:
    def __init__(self, timeout_seconds: float = 600.0) -> None:
        self.timeout_seconds = timeout_seconds
        self._ids = itertools.count(1)
        self._pending: Dict[str, Dict[str, Any]] = {}
        # resolve 统一回调（app 注入）：无论用户确认还是超时拒绝都广播
        # approval:resolved —— 超时路径原本只打日志，前端审批卡会永远挂着
        self.on_resolve: Optional[Callable[[str, Dict[str, Any]], Any]] = None

    def request_approval(
        self, action: str, risk_reason: str, auto_approve: bool = False,
        rule: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> "asyncio.Future":
        """挂起等待 Web UI 的人工确认，返回 future（await 后得到 bool）。

        rule：审批上下文（{"tool", "kind", "pattern", ...}），供「记住并允许
        同类」按钮在确认时写入自动同意规则；None 表示该操作不提供记住能力
        （如不可逆删除）。session_id：审批所属会话（记住规则的作用域）。
        """
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
            "rule": rule,
            "session_id": session_id,
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
        # 统一广播：超时拒绝也走这里（此前只打日志，前端卡片会永远挂着）
        if self.on_resolve is not None:
            try:
                self.on_resolve(approval_id, {
                    "id": approval_id,
                    "approved": approved,
                    "by": by,
                    "action": entry.get("action", ""),
                })
            except Exception:
                logger.debug("[Approval] on_resolve 回调失败", exc_info=True)
        return True

    def get_pending_info(self, approval_id: str) -> Optional[Dict[str, Any]]:
        entry = self._pending.get(approval_id)
        if entry is None:
            return None
        return {"id": entry["id"], "action": entry["action"], "reason": entry["reason"],
                "created_at": entry["created_at"], "rule": entry.get("rule"),
                "session_id": entry.get("session_id")}

    def list_pending(self) -> List[Dict[str, Any]]:
        """当前全部挂起审批（SSE 重连后前端兜底同步用）。"""
        return [{"id": e["id"], "action": e["action"], "reason": e["reason"],
                 "created_at": e["created_at"], "rule": e.get("rule"),
                 "session_id": e.get("session_id")}
                for e in self._pending.values()]

    def pending_count(self) -> int:
        return len(self._pending)