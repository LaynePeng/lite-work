# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""会话派生端点（POST /api/sessions/{id}/derive）：P2 任务卡「派生」后端。

克隆源会话骨架（goal / collab_mode / model / 隔离工作树开关 / TODO 结构）到
新会话；对话历史不复制，TODO 全部重置为 pending。
"""
from __future__ import annotations

import pytest

from litework.app import AgentApp
from litework.server.app import create_app
from tests.conftest import MockLLMAdapter, live_server, tool_call


@pytest.fixture
async def live_client(tmp_path):
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    app._mock_adapter = MockLLMAdapter([("完成", [])])
    app.refresh_model_meta = lambda: False
    fast_app = create_app(app, token=None)
    async with live_server(fast_app) as (client, server):
        yield client, app, server


async def test_derive_copies_goal_and_todos_reset_to_pending(live_client):
    c, app, _ = live_client

    r = await c.post("/api/sessions", json={"name": "季度报告"})
    assert r.status_code == 200
    src = r.json()["session_id"]

    # 源：设 goal + 含完成项的 TODO 看板（completed / in_progress / pending 混合）
    r = await c.post(f"/api/sessions/{src}/goal", json={"goal": "产出季度报告 PDF"})
    assert r.status_code == 200
    app.todo_plugin.seed_board(src, [])  # 先占位（无 effect）
    app.todo_plugin._items[src] = [
        {"content": "收集数据", "status": "completed"},
        {"content": "写正文第三章", "status": "in_progress"},
        {"content": "排版", "status": "pending"},
    ]

    r = await c.post(f"/api/sessions/{src}/derive", json={})
    assert r.status_code == 200
    body = r.json()
    new_id = body["session_id"]
    assert new_id != src
    assert body["workspace"] == app.workspace
    assert body["goal"] == "产出季度报告 PDF"
    assert body["todos_copied"] == 3
    assert body["worktree"] is False

    # 新会话 TODO：结构继承、状态全部重置为 pending
    r = await c.get("/api/todos", params={"session_id": new_id})
    assert r.status_code == 200
    todos = r.json()["todos"]
    assert [t["content"] for t in todos] == ["收集数据", "写正文第三章", "排版"]
    assert all(t["status"] == "pending" for t in todos)

    # 新会话 metadata：goal 继承、标题带「派生」标记（源有 name）
    r = await c.get(f"/api/sessions/{new_id}")
    meta = r.json()["metadata"]
    assert meta.get("goal") == "产出季度报告 PDF"
    assert meta.get("name") == "季度报告 · 派生"


async def test_derive_no_todos(live_client):
    c, app, _ = live_client

    r = await c.post("/api/sessions", json={"name": "无看板任务"})
    src = r.json()["session_id"]

    r = await c.post(f"/api/sessions/{src}/derive", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["todos_copied"] == 0
    assert body["goal"] is None

    r = await c.get("/api/todos", params={"session_id": body["session_id"]})
    assert r.json()["todos"] == []


async def test_derive_skips_worktree_when_not_git(live_client):
    """源在隔离工作树模式，但项目非 git 仓库 → 派生不继承 worktree（静默降级）。"""
    c, app, _ = live_client

    r = await c.post("/api/sessions", json={})
    src = r.json()["session_id"]

    # 直接改源 metadata 模拟「worktree=True」（开启端点会因非 git 拒绝 400）
    snap = app.session_store.load(src)
    assert snap is not None
    meta = dict(snap.metadata)
    meta["worktree"] = True
    app.session_store.save(src, snap.messages, meta)

    r = await c.post(f"/api/sessions/{src}/derive", json={})
    assert r.status_code == 200
    assert r.json()["worktree"] is False


async def test_derive_unknown_source_404(live_client):
    c, app, _ = live_client
    r = await c.post("/api/sessions/does_not_exist/derive", json={})
    assert r.status_code == 404


async def test_derive_custom_name(live_client):
    c, app, _ = live_client
    r = await c.post("/api/sessions", json={"name": "源"})
    src = r.json()["session_id"]

    r = await c.post(f"/api/sessions/{src}/derive", json={"name": "改版 V2"})
    assert r.status_code == 200
    snap = app.session_store.load(r.json()["session_id"])
    assert snap is not None
    assert snap.metadata.get("name") == "改版 V2"
