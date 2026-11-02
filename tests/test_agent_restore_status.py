# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""子 Agent 状态跨重启恢复的诚实性。

回归背景：
- `tasks.py` 落盘 subagent_records 时硬编码 `status="completed"`，丢掉了
  sub_agent 算出的 errored；
- `agent_manager._restore_from_session` 恢复时又硬编码 `status="completed"`。

两者叠加的结果：Core 重启后，**正在运行的子 Agent 会显示为「已完成」**，
编排者调用 list_agents 看到谎报状态，可能重复派活或漏接管被中断的工作。
"""
from __future__ import annotations

import pytest

from litework.app import AgentApp
from litework.orchestration.agent_manager import SessionAgentManager


@pytest.fixture
def app(tmp_path):
    a = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    a.refresh_model_meta = lambda: False
    return a


def _seed(app, session_id, records):
    app.session_store.save(session_id, [], {})
    app.session_store.update_metadata(session_id, {"subagent_records": records})


def test_running_agent_restored_as_interrupted(app):
    """Core 重启时仍在 running 的记录 → interrupted，而非 completed。"""
    _seed(app, "s1", [{
        "subagentId": "sa_running", "role": "general", "task": "长任务",
        "nickname": "工人-1", "status": "running", "depth": 1,
    }])
    manager = SessionAgentManager(app, "s1")
    manager._restore_from_session()
    record = manager.agents["sa_running"]
    assert record.status == "interrupted", f"running 被谎报为 {record.status}"


def test_pending_agent_restored_as_interrupted(app):
    """未开始执行就重启的 pending 记录同样算中断。"""
    _seed(app, "s1", [{
        "subagentId": "sa_pending", "role": "general", "task": "排队任务",
        "nickname": "工人-2", "status": "pending", "depth": 1,
    }])
    manager = SessionAgentManager(app, "s1")
    manager._restore_from_session()
    assert manager.agents["sa_pending"].status == "interrupted"


def test_errored_agent_keeps_errored(app):
    """真的失败过（errored）的记录必须保留，不得冒充 completed。"""
    _seed(app, "s1", [{
        "subagentId": "sa_err", "role": "general", "task": "失败任务",
        "nickname": "工人-3", "status": "errored", "depth": 1,
    }])
    manager = SessionAgentManager(app, "s1")
    manager._restore_from_session()
    assert manager.agents["sa_err"].status == "errored"


def test_completed_agent_stays_completed(app):
    """正常完成的记录保持 completed（不误伤）。"""
    _seed(app, "s1", [{
        "subagentId": "sa_ok", "role": "general", "task": "完成的任务",
        "nickname": "工人-4", "status": "completed", "depth": 1, "summary": "做完了",
    }])
    manager = SessionAgentManager(app, "s1")
    manager._restore_from_session()
    assert manager.agents["sa_ok"].status == "completed"


def test_legacy_record_without_status_is_not_completed(app):
    """旧版本落盘没有 status 字段：不能假定 completed（那时正是谎报源头）。"""
    _seed(app, "s1", [{
        "subagentId": "sa_legacy", "role": "general", "task": "老记录",
        "nickname": "工人-5", "depth": 1,
    }])
    manager = SessionAgentManager(app, "s1")
    manager._restore_from_session()
    assert manager.agents["sa_legacy"].status == "interrupted"


def test_interrupted_agent_can_be_woken(app):
    """interrupted 不阻塞唤醒：用户仍可 followup_task 让它接着干。"""
    _seed(app, "s1", [{
        "subagentId": "sa_wake", "role": "general", "task": "被中断的任务",
        "nickname": "工人-6", "status": "running", "depth": 1,
    }])
    manager = SessionAgentManager(app, "s1")
    manager._restore_from_session()
    record = manager.agents["sa_wake"]
    assert record.status == "interrupted"
    # 只有 running / closed 拒绝唤醒
    assert record.status not in ("running", "closed")


def test_list_agents_flags_interrupted(app):
    """list_agents 必须让编排者看到「有活被中断」，否则它以为都完成了。"""
    _seed(app, "s1", [
        {"subagentId": "sa_a", "role": "general", "task": "任务A", "nickname": "甲",
         "status": "running", "depth": 1},
        {"subagentId": "sa_b", "role": "general", "task": "任务B", "nickname": "乙",
         "status": "completed", "depth": 1, "summary": "好了"},
    ])
    manager = SessionAgentManager(app, "s1")
    rows = manager.list_agents()
    by_id = {r["agent_id"]: r for r in rows}
    assert by_id["sa_a"]["status"] == "interrupted"
    assert by_id["sa_b"]["status"] == "completed"
