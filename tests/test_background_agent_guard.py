# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""MCP 热重载与工作区切换的后台 Agent 守卫测试。

后台子 Agent 生命周期超出主任务（spawn_agent 异步派生）：
- MCP reload 会 close 它们 registry 捕获的旧 MCPClient → 调用即断管道；
- 工作区切换会移走其 worktree/路径基准。
两处守卫此前只数主任务（tasks.active_count），此处覆盖后台 Agent 场景。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from litework.app import AgentApp
from litework.server.app import create_app


@pytest.fixture
def client_and_app(tmp_path):
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    app.refresh_model_meta = lambda: False
    fast = create_app(app, token=None)
    with TestClient(fast) as client:
        yield client, app


def _fake_running_manager(agents: dict):
    """构造带运行中 agent 记录的假 SessionAgentManager（鸭子类型）。"""
    return SimpleNamespace(agents={
        aid: SimpleNamespace(status=status) for aid, status in agents.items()
    })


def test_background_agent_count(client_and_app):
    client, app = client_and_app
    assert app.background_agent_count() == 0
    app.agent_managers["s1"] = _fake_running_manager({"a1": "running", "a2": "completed"})
    app.agent_managers["s2"] = _fake_running_manager({"a3": "running", "a4": "closed"})
    assert app.background_agent_count() == 2


def test_mcp_update_blocked_while_background_agent_running(client_and_app):
    client, app = client_and_app
    app.agent_managers["s1"] = _fake_running_manager({"a1": "running"})
    r = client.post("/api/mcp", json={"servers": {}})
    assert r.status_code == 409
    assert "后台 Agent" in r.json()["detail"]


def test_mcp_update_allowed_when_background_agents_finished(client_and_app):
    client, app = client_and_app
    app.agent_managers["s1"] = _fake_running_manager({"a1": "completed", "a2": "errored"})
    r = client.post("/api/mcp", json={"servers": {}})
    assert r.status_code == 200


def test_workspace_switch_blocked_while_background_agent_running(client_and_app, tmp_path):
    client, app = client_and_app
    app.agent_managers["s1"] = _fake_running_manager({"a1": "running"})
    r = client.post("/api/workspace", json={"path": str(tmp_path)})
    assert r.status_code == 409
    assert "后台 Agent" in r.json()["detail"]


def test_recent_project_open_blocked_while_background_agent_running(client_and_app, tmp_path):
    client, app = client_and_app
    app.agent_managers["s1"] = _fake_running_manager({"a1": "running"})
    r = client.post("/api/projects/recent", json={"path": str(tmp_path)})
    assert r.status_code == 409
