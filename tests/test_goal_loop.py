# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""会话目标（/goal）与目标循环（/loop）测试。

- /goal：API 设置/清除 + 任务 system prompt 注入 + 命令面板注册
- /loop：纯前端驱动（task:done 后续发），后端无状态——这里验证其命令注册
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from litework.app import AgentApp
from litework.server.app import create_app
from tests.conftest import MockLLMAdapter


@pytest.fixture
def client_and_app(tmp_path):
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    app.refresh_model_meta = lambda: False
    fast = create_app(app, token=None)
    with TestClient(fast) as client:
        yield client, app


def _create_session(client) -> str:
    r = client.post("/api/sessions", json={})
    assert r.status_code == 200
    return r.json()["session_id"]


def test_goal_set_and_clear_roundtrip(client_and_app):
    client, app = client_and_app
    sid = _create_session(client)

    # 设置
    r = client.post(f"/api/sessions/{sid}/goal", json={"goal": "完成周报并生成 Word 文档"})
    assert r.status_code == 200
    assert r.json()["goal"] == "完成周报并生成 Word 文档"
    snap = app.session_store.load(sid)
    assert snap.metadata["goal"] == "完成周报并生成 Word 文档"

    # 会话快照可恢复（前端 selectSession 从 metadata 读取）
    r = client.get(f"/api/sessions/{sid}")
    assert r.json()["metadata"]["goal"] == "完成周报并生成 Word 文档"

    # 清除
    r = client.post(f"/api/sessions/{sid}/goal", json={"goal": "  "})
    assert r.status_code == 200
    assert r.json()["goal"] is None
    assert "goal" not in app.session_store.load(sid).metadata


def test_goal_requires_existing_session(client_and_app):
    client, _ = client_and_app
    r = client.post("/api/sessions/no-such-session/goal", json={"goal": "x"})
    assert r.status_code == 404


def test_goal_injected_into_system_prompt(client_and_app):
    """设置目标后启动任务：system prompt 携带目标段（跨任务持续生效）。"""
    import time

    client, app = client_and_app
    sid = _create_session(client)
    client.post(f"/api/sessions/{sid}/goal", json={"goal": "把 README 翻译成英文"})

    app._mock_adapter = MockLLMAdapter([("好的，开始。", [])])
    r = client.post("/api/chat", json={"session_id": sid, "prompt": "开始"})
    assert r.status_code == 200

    # TestClient 的 portal 在上下文管理器存续期间保持事件循环存活，
    # 任务在后台线程跑完；这里轮询会话落盘结果验证 system prompt 注入
    msgs = []
    for _ in range(200):
        snap = app.session_store.load(sid)
        msgs = snap.messages if snap else []
        if any(m.role == "assistant" for m in msgs):
            break
        time.sleep(0.05)
    system = next((m for m in msgs if m.role == "system"), None)
    assert system is not None, "system prompt 未落盘"
    assert "当前会话目标" in system.content
    assert "把 README 翻译成英文" in system.content


def test_goal_and_loop_registered_in_command_palette(client_and_app):
    client, _ = client_and_app
    r = client.get("/api/commands")
    assert r.status_code == 200
    names = [c["name"] for c in r.json()["commands"]]
    assert "goal" in names
    assert "loop" in names
    assert "compact" in names
