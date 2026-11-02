# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""把 lite-work 的审批门接到 ACP 的 ``session/request_permission``。

**这是 ACP 模式下能否真正干活的关键一环。** lite-work 的写文件/执行命令等
操作都要经过审批门（``ApprovalGate``，600s 超时后拒绝）。Web UI 里由前端弹
审批卡，但 ACP 模式下没有前端——如果不接这个桥，Agent 一碰写操作就会挂住
直到超时被拒，用户看到的是「Agent 卡了十分钟然后失败」。

协议要点（agentclientprotocol.com/protocol/v1/tool-calls#requesting-permission）：

- 这是 **Agent → Client 的反向请求**，不是通知：必须带 id、等响应；
- Client 回 ``{"outcome": {"outcome": "selected", "optionId": "..."}}``
  或 ``{"outcome": {"outcome": "cancelled"}}``；
- 用户在编辑器里点「取消」时必须回 ``cancelled``，且此时应视为**拒绝**。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("litework.acp")

#: 协议定义的 option kind（客户端据此决定按钮样式）
KIND_ALLOW_ONCE = "allow_once"
KIND_ALLOW_ALWAYS = "allow_always"
KIND_REJECT_ONCE = "reject_once"
KIND_REJECT_ALWAYS = "reject_always"

ALLOW_ONCE = "allow-once"
ALLOW_ALWAYS = "allow-always"
REJECT_ONCE = "reject-once"


def build_options(rememberable: bool) -> List[Dict[str, str]]:
    """构造权限选项列表。

    只在内部提供规则（``rememberable``）时才给「总是允许」——否则那个按钮
    点了也没有记忆能力，属于骗用户。
    """
    options = [
        {"optionId": ALLOW_ONCE, "name": "允许本次", "kind": KIND_ALLOW_ONCE},
        {"optionId": REJECT_ONCE, "name": "拒绝", "kind": KIND_REJECT_ONCE},
    ]
    if rememberable:
        options.insert(1, {
            "optionId": ALLOW_ALWAYS, "name": "总是允许（记住同类）",
            "kind": KIND_ALLOW_ALWAYS,
        })
    return options


def interpret_outcome(result: Any) -> bool:
    """把 client 的响应翻译成「是否批准」。

    非 selected（含 cancelled / 畸形响应）一律当**拒绝**：审批门的语义是
    「没被明确批准就不能做」，把不确定当放行是危险的。
    """
    if not isinstance(result, dict):
        return False
    outcome = result.get("outcome")
    if not isinstance(outcome, dict):
        return False
    if outcome.get("outcome") != "selected":
        return False
    return outcome.get("optionId") in (ALLOW_ONCE, ALLOW_ALWAYS)


class PermissionBridge:
    """订阅 ``approval:request`` 事件 → 发 ACP 权限请求 → 回填审批门。"""

    def __init__(self, transport: Any, relay: Any, session_id: str) -> None:
        """
        transport: StdioTransport（要用它的 ``request`` 做反向请求）
        relay: 用于把「批准/拒绝」写回 ApprovalGate 的调用方（见 ``set_relay``）
        """
        self._transport = transport
        self._relay = relay
        self._session_id = session_id
        self._detached = False
        #: 正在等待用户决定的审批 id 集合（取消时统一清理）
        self._pending: set[str] = set()

    def attach(self, kernel: Any) -> None:
        events = getattr(kernel, "events", None)
        if events is None:
            return
        events.on("approval:request", self._on_request)

    def _on_request(self, payload: Optional[Dict[str, Any]]) -> None:
        if self._detached:
            return
        p = payload or {}
        approval_id = str(p.get("id") or "")
        if not approval_id:
            return
        # 子 Agent 的审批也汇聚到主会话（规则同 security/plugin.py：用 root）
        self._pending.add(approval_id)
        self._fire(self._ask(p))

    def _fire(self, coro) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(coro)

    async def _ask(self, payload: Dict[str, Any]) -> None:
        approval_id = str(payload.get("id") or "")
        action = str(payload.get("action") or "未知操作")
        reason = str(payload.get("reason") or "")
        try:
            result = await self._transport.request("session/request_permission", {
                "sessionId": self._session_id,
                "toolCall": {
                    "toolCallId": payload.get("callId") or approval_id,
                    "title": action,
                    "kind": "other",
                    "status": "pending",
                    "rawInput": {"reason": reason} if reason else {},
                },
                "options": build_options(bool(payload.get("rememberable"))),
            })
            approved = interpret_outcome(result)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # 通道断了 / client 没实现该方法 → 按拒绝处理，
            # 否则工具会一直挂到 600s 超时（用户完全不知道发生了什么）
            logger.warning("[ACP] 权限请求失败，按拒绝处理", exc_info=True)
            approved = False
        finally:
            self._pending.discard(approval_id)
        try:
            self._relay(approval_id, approved)
        except Exception:  # noqa: BLE001
            logger.debug("[ACP] 回填审批结果失败", exc_info=True)

    def detach(self) -> None:
        self._detached = True


def make_gate_relay(app: Any) -> Any:
    """构造把决定写回 ``ApprovalGate`` 的回调。

    gate 内部用 ``resolve(approval_id, approved)`` 收尾（见 security/approval.py）。
    """
    gate = app.approval_gate

    def relay(approval_id: str, approved: bool) -> None:
        # by="acp"：与 Web UI 的 "user" 区分，日志/审计里能看出决定来自编辑器
        gate.resolve(approval_id, approved, by="acp")

    return relay
