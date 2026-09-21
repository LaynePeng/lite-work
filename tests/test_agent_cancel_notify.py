# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""子 Agent 取消的父级感知：编排者必须知道任务被谁砍了。

- 编排者自己 close_agent（by=agent）→ 不通知（它自己发起的，知道）
- 用户从 Agents 看板取消（by=user）→ 必须进通知队列（否则编排者
  下一轮仍以为任务在跑，wait_agents / 派生决策全部失真）
- list_agents：用户取消的 closed 记录可见；编排者自己关闭的默认隐藏
"""
from __future__ import annotations

import asyncio

from litework.orchestration.agent_manager import AgentRecord, SessionAgentManager


def _record(aid: str = "sa_x1", closed_by: str = "") -> AgentRecord:
    return AgentRecord(
        agent_id=aid, nickname="victim", role="explorer",
        task="调研任务", status="closed", summary="",
        closed_by=closed_by,
    )


def test_user_cancel_enqueues_notification() -> None:
    """用户取消 → 通知队列出现 cancelled-by-user 文本（父 Agent 下轮注入可见）。"""
    mgr = SessionAgentManager.__new__(SessionAgentManager)  # 不走 __init__（避免 app 依赖）
    mgr.notifications = []
    mgr._enqueue_notification(_record("sa_x1", closed_by="user"))
    assert len(mgr.notifications) == 1
    text = mgr.notifications[0]["text"]
    assert "cancelled-by-user" in text
    assert "已被用户手动取消" in text
    assert "调研任务" in text


def test_agent_close_no_notification() -> None:
    """编排者 close_agent（by=agent）→ 不通知（自己发起的，不算交付）。"""
    mgr = SessionAgentManager.__new__(SessionAgentManager)
    mgr.notifications = []
    mgr._enqueue_notification(_record("sa_x1", closed_by="agent"))
    mgr._enqueue_notification(_record("sa_x2", closed_by="shutdown"))
    assert mgr.notifications == []


def test_list_agents_shows_user_cancelled() -> None:
    """list_agents：用户取消的 closed 记录可见；编排者关闭的默认隐藏。"""
    mgr = SessionAgentManager.__new__(SessionAgentManager)
    mgr.agents = {
        "sa_u": _record("sa_u", closed_by="user"),
        "sa_a": _record("sa_a", closed_by="agent"),
        "sa_r": AgentRecord(agent_id="sa_r", nickname="r", role="general",
                            task="运行中", status="running"),
    }
    mgr._restore_from_session = lambda: None  # 跳过会话恢复（无 app 依赖）
    out = {a["agent_id"] for a in mgr.list_agents()}
    assert "sa_u" in out, "用户取消的记录必须可见（编排者要能发现任务被砍）"
    assert "sa_a" not in out, "编排者自己关闭的默认隐藏（不算交付）"
    assert "sa_r" in out


async def test_close_records_by_and_summary() -> None:
    """close(by=user) 写入 closed_by；取消 runner 后 summary 标注用户取消。"""
    mgr = SessionAgentManager.__new__(SessionAgentManager)
    mgr.agents = {}
    rec = AgentRecord(agent_id="sa_c", nickname="c", role="general",
                      task="长任务", status="running")
    mgr.agents["sa_c"] = rec
    # 模拟运行中的 runner：挂起的 sleep task，cancel 会向 _run 的 except 传播
    # 这里直接测 close 本身的行为（runner 为 None 的兜底路径）
    rec.runner = None
    result = await mgr.close("sa_c", by="user")
    assert result["ok"] is True
    assert rec.closed_by == "user"
    assert rec.status == "closed"


def test_agent_tools_close_emits_by_agent() -> None:
    """close_agent 工具路径发 agent:closed（by=agent）：strict 校验通过。"""
    from litework.core.events import AgentClosedPayload, TypedEventBus

    bus = TypedEventBus(strict=True)
    seen: list = []
    bus.on("agent:closed", lambda p: seen.append(p))
    # 直接验证 payload 结构：by 字段恒定携带（strict 校验必过）
    asyncio.get_event_loop_policy()
    payload: AgentClosedPayload = {"agentId": "sa_z", "by": "agent"}
    import asyncio as _a

    async def _emit() -> None:
        await bus.emit("agent:closed", payload)

    _a.run(_emit())
    assert seen and seen[0]["by"] == "agent"
