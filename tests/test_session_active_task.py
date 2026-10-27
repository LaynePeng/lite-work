# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""会话级「当前是否有任务在跑」查询端点（前端 SSE 补连用）。

背景：SSE 连接只活在前端内存里——切项目、关闭再打开对话页签、页面恢复之后，
连接就断了，而后端任务照常运行。没有这个端点，前端只能显示历史快照：既看不到
后续输出，也接不到审批卡（表现为「这个对话回不来了，而且是死的历史」）。

不变量：
1. 没有运行中任务时返回 running=False 且 task_id 为空串（前端据此不连接）；
2. 有运行中任务时返回该任务的 task_id（前端据此补连 `/api/tasks/{id}/events`）；
3. 任务结束后不再命中（避免前端反复补连一个已收尾的任务）。
"""
from __future__ import annotations

import asyncio
from typing import List, Optional

from litework.app import AgentApp
from litework.core.types import Message, ToolDefinition
from litework.server.app import create_app
from litework.server.tasks import TaskManager
from tests.conftest import live_server


class _SlowAdapter:
    """慢速 mock LLM：让任务真的处于运行中，供「运行中查询」断言。"""

    def __init__(self, delay: float = 3.0) -> None:
        self.delay = delay

    async def chat_stream(
        self,
        messages: List[Message],
        tools: List[ToolDefinition],
        events: Optional[object] = None,
    ) -> tuple:
        await asyncio.sleep(self.delay)
        return "完成", [], None


def _make_app(tmp_path) -> AgentApp:
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    app._mock_adapter = _SlowAdapter(delay=0.05)
    app.refresh_model_meta = lambda: False
    return app


async def test_session_active_task_idle(tmp_path):
    """无运行中任务：端点返回 running=False、task_id 空串（前端不补连）。"""
    fast_app = create_app(_make_app(tmp_path), token=None)
    async with live_server(fast_app) as (client, _server):
        r = await client.get("/api/sessions/no-such-session/task")
        assert r.status_code == 200
        data = r.json()
        assert data["running"] is False
        assert data["task_id"] == ""
        assert data["session_id"] == "no-such-session"


async def test_session_active_task_reports_running_and_clears_after_done(tmp_path):
    """运行中返回 task_id；任务结束后回到「无任务」（不反复补连已收尾任务）。"""
    app = _make_app(tmp_path)
    app._mock_adapter = _SlowAdapter(delay=1.0)
    fast_app = create_app(app, token=None)
    async with live_server(fast_app) as (client, _server):
        sid = (await client.post("/api/sessions", json={})).json()["session_id"]
        chat = await client.post("/api/chat", json={"session_id": sid, "prompt": "hi"})
        assert chat.status_code == 200
        task_id = chat.json()["task_id"]

        r = await client.get(f"/api/sessions/{sid}/task")
        assert r.status_code == 200
        data = r.json()
        assert data["running"] is True
        assert data["task_id"] == task_id

        # 等任务跑完（慢 adapter 1s，加余量）
        for _ in range(60):
            await asyncio.sleep(0.1)
            cur = (await client.get(f"/api/sessions/{sid}/task")).json()
            if not cur["running"]:
                break
        assert cur["running"] is False
        assert cur["task_id"] == ""


async def test_active_for_session_ignores_finished_task(tmp_path):
    """TaskManager.active_for_session 只认运行中的任务（端点语义的数据源）。"""
    app = _make_app(tmp_path)
    tasks = TaskManager(app)
    handle = tasks.start("sess-done", "hi")
    await handle.task

    assert tasks.active_for_session("sess-done") is None
