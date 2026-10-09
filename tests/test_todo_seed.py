# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""会话 TODO 预置端点（POST /api/sessions/{id}/todos）：新建任务向导「计划项」落看板。

计划项全部置为 pending（新任务从零推进），经 TodoPlugin.seed_board 落盘。
"""
from __future__ import annotations

import pytest

from litework.app import AgentApp
from litework.server.app import create_app
from tests.conftest import MockLLMAdapter, live_server


@pytest.fixture
async def live_client(tmp_path):
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    app._mock_adapter = MockLLMAdapter([("完成", [])])
    app.refresh_model_meta = lambda: False
    fast_app = create_app(app, token=None)
    async with live_server(fast_app) as (client, server):
        yield client, app, server


async def test_seed_todos_all_pending(live_client):
    c, app, _ = live_client
    r = await c.post("/api/sessions", json={})
    sid = r.json()["session_id"]

    r = await c.post(f"/api/sessions/{sid}/todos", json={"items": [
        "  收集数据  ",
        "写正文第三章",
        "",
        "排版",
    ]})
    assert r.status_code == 200
    assert r.json()["seeded"] == 3

    r = await c.get("/api/todos", params={"session_id": sid})
    todos = r.json()["todos"]
    assert [t["content"] for t in todos] == ["收集数据", "写正文第三章", "排版"]
    assert all(t["status"] == "pending" for t in todos)


async def test_seed_empty_items(live_client):
    c, app, _ = live_client
    r = await c.post("/api/sessions", json={})
    sid = r.json()["session_id"]

    r = await c.post(f"/api/sessions/{sid}/todos", json={"items": []})
    assert r.status_code == 200
    assert r.json()["seeded"] == 0

    r = await c.get("/api/todos", params={"session_id": sid})
    assert r.json()["todos"] == []


async def test_seed_unknown_session_404(live_client):
    c, app, _ = live_client
    r = await c.post("/api/sessions/nope/todos", json={"items": ["x"]})
    assert r.status_code == 404
