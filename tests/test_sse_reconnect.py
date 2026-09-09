# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""SSE 断开重连测试（P2-6）：客户端中途断连后重连，事件不丢、无假 [DONE]。

回归背景：_stream 的 CancelledError 分支曾在断连时向队列投 None 哨兵，
重连的客户端会立即收到 [DONE]，把仍在运行的任务误判为已结束。
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import uvicorn

from litework.app import AgentApp
from litework.core.types import Message, ToolCall, ToolDefinition
from litework.server.app import create_app


class SlowMockAdapter:
    """每轮 LLM 调用前延时：给测试留出断开 SSE 的窗口。

    脚本：第 1 轮调用 read_file 工具，第 2 轮输出终答。"""

    def __init__(self, delay: float = 0.4) -> None:
        self.delay = delay
        self.calls = 0

    async def chat_stream(self, messages, tools, events=None):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.calls == 1:
            if events:
                await events.emit("llm:stream", {"chunk": "先读文件…"})
            return "", [ToolCall(id="c1", name="read_file",
                                 arguments='{"filePath": "x.txt"}')], None
        if events:
            await events.emit("llm:stream", {"chunk": "完成了。"})
        return "任务完成总结", [], {"prompt_tokens": 10, "completion_tokens": 5,
                                    "prompt_cache_hit_tokens": 0}


@pytest.fixture
async def live_client(tmp_path):
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    app._mock_adapter = SlowMockAdapter(delay=0.8)
    app.refresh_model_meta = lambda: False
    fast_app = create_app(app, token=None)
    config = uvicorn.Config(fast_app, host="127.0.0.1", port=0, log_level="error",
                            timeout_graceful_shutdown=2)
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    for _ in range(400):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]

    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=20) as c:
        yield c, app

    server.should_exit = True
    await asyncio.wait_for(server_task, timeout=10)


async def _read_events_until(resp, predicate, collected):
    """从 SSE 流读取事件直到谓词命中（跨 chunk 行分裂安全）。"""
    buffer = ""
    async for chunk in resp.aiter_text():
        buffer += chunk
        lines = buffer.split("\n")
        buffer = lines.pop()  # 末行可能不完整，留待下一 chunk
        for line in lines:
            line = line.strip()
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                collected.append("[DONE]")
                return True
            ev = json.loads(data)
            collected.append(ev)
            if predicate(ev):
                return True
    return False


def _types_of(collected):
    """收集列表中的事件类型（过滤 [DONE] 字符串哨兵）。"""
    return [ev.get("type") for ev in collected if ev != "[DONE]"]


async def test_sse_disconnect_and_reconnect_no_premature_done(live_client):
    """中途断连 → 重连：收到真实 task:done 后才 [DONE]，工具事件不丢。"""
    c, app = live_client
    r = await c.post("/api/sessions", json={})
    sid = r.json()["session_id"]
    r = await c.post("/api/chat", json={"session_id": sid, "prompt": "读取并总结 x.txt"})
    task_id = r.json()["task_id"]

    # 阶段一：连接后读到首个事件即断开（任务仍在第 1 轮 LLM 延时中）
    first = []
    async with c.stream("GET", f"/api/tasks/{task_id}/events") as resp:
        assert resp.status_code == 200
        await _read_events_until(resp, lambda ev: ev.get("type") == "task:start", first)
    assert "task:start" in _types_of(first)

    await asyncio.sleep(0.1)  # 让服务端感知断连（generator cancel 传播）

    # 阶段二：重连，读完整流
    second = []
    async with c.stream("GET", f"/api/tasks/{task_id}/events") as resp:
        assert resp.status_code == 200
        done = await _read_events_until(resp, lambda ev: False, second)
    assert done, "重连后应能读到 [DONE] 结束标记"

    # 无假 [DONE]：[DONE] 之前必先观察到 task:done
    types = _types_of(second)
    assert "[DONE]" not in types, "重连客户端收到了过早的 [DONE] 哨兵（回归）"
    assert "task:done" in types, "重连后应收到真实的 task:done"
    assert second[-1] == "[DONE]"
    # 订阅后的事件完整送达：工具执行链与终态都到达重连的客户端
    # （重连订阅者不回放断连期间的事件——前端 UI 无去重能力，回放会重复渲染）
    assert "tool:before_execute" in types
    assert "tool:after_execute" in types
    done_ev = next(ev for ev in second if ev != "[DONE]" and ev.get("type") == "task:done")
    assert "任务完成总结" in done_ev["data"]["content"]

    # 阶段三：[DONE] 后任务句柄清理，再次连接 404（前端探测路径）
    r = await c.get(f"/api/tasks/{task_id}/events")
    assert r.status_code == 404
