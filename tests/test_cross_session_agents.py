# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""跨会话 agent 可见性（D 项）——**只读**边界。

设计取舍：跨会话只提供查询，不提供消息投递/唤醒/关闭。理由是 dsh-superagent
的研究结论——所有跨 session 动作必须单向、受中介、带权限语义，它自己刻意
不做自由的 agent 消息总线。我们只补「看不见」这一块。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from litework.app import AgentApp
from litework.server.app import create_app


@pytest.fixture
def app(tmp_path):
    a = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    a.refresh_model_meta = lambda: False
    return a


def _seed(app, session_id, agent_id, status="completed"):
    app.session_store.save(session_id, [], {})
    app.session_store.update_metadata(session_id, {"subagent_records": [{
        "subagentId": agent_id, "role": "general", "task": "活",
        "nickname": f"工人-{agent_id}", "status": status, "depth": 1,
    }]})


def test_all_agents_spans_sessions(app):
    """跨会话总览能看到两个会话各自的 agent。"""
    _seed(app, "s1", "sa_1")
    _seed(app, "s2", "sa_2")
    m1 = app.agent_manager("s1")
    m1._restore_from_session()
    m2 = app.agent_manager("s2")
    m2._restore_from_session()

    overview = app.all_agents()
    assert set(overview) == {"s1", "s2"}
    assert overview["s1"][0]["agent_id"] == "sa_1"
    assert overview["s2"][0]["agent_id"] == "sa_2"


def test_all_agents_does_not_instantiate_managers(app):
    """查询不得凭空造 manager（否则和 release_session 的回收目标打架）。"""
    app.session_store.save("s-only-on-disk", [], {})
    before = dict(app.agent_managers)
    app.all_agents()
    assert app.agent_managers == before
    assert "s-only-on-disk" not in app.agent_managers


def test_find_agent_returns_home_session(app):
    _seed(app, "s1", "sa_1")
    app.agent_manager("s1")._restore_from_session()
    found = app.find_agent("sa_1")
    assert found is not None
    assert found["home_session"] == "s1"
    assert app.find_agent("no-such-agent") is None


def test_cross_session_send_message_still_refused(app):
    """核心边界：跨会话可见 ≠ 可投递。send_message 仍只作用于本会话。"""
    _seed(app, "s1", "sa_1")
    app.agent_manager("s1")._restore_from_session()
    # 另一个会话的 manager 完全不认识 sa_1 → 投递必然被拒
    other = app.agent_manager("s2")
    assert other.get("sa_1") is None
    # 但能看见（只读）
    assert app.find_agent("sa_1") is not None


@pytest.mark.asyncio
async def test_cross_session_send_message_returns_error(app):
    _seed(app, "s1", "sa_1")
    app.agent_manager("s1")._restore_from_session()
    other = app.agent_manager("s2")
    result = await other.send_message("sa_1", "跨会话消息")
    assert result["ok"] is False
    assert "未知 agent" in result["error"]


def test_live_agents_endpoint_is_read_only(app):
    """API 只提供 GET，不存在跨会话的写入口。"""
    _seed(app, "s1", "sa_1")
    app.agent_manager("s1")._restore_from_session()
    fast = create_app(app, token=None)
    with TestClient(fast) as client:
        r = client.get("/api/agents/live")
        assert r.status_code == 200
        assert "sa_1" in [a["agent_id"] for a in r.json()["sessions"]["s1"]]
        # 没有对应的写端点
        assert client.post("/api/agents/live").status_code in (404, 405)


@pytest.mark.asyncio
async def test_list_tool_mentions_other_sessions(app):
    """list_agents 工具要告知「另有 N 个 agent 属于其他会话」。"""
    from litework.tools.agent_tools import make_agent_tool_handlers

    _seed(app, "s1", "sa_mine")
    _seed(app, "s2", "sa_theirs")
    app.agent_manager("s1")._restore_from_session()
    app.agent_manager("s2")._restore_from_session()

    class K:
        root_session_id = "s1"

    handlers = make_agent_tool_handlers(app, kernel=K())
    text = await handlers["list_agents"]({})
    assert "sa_mine" in text
    assert "其他会话" in text
