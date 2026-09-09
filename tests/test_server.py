# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""服务层 API 测试：会话管理 / 聊天 SSE 流 / 审批流程 / 安全规则热更新。

注意：httpx ASGITransport 会缓冲完整响应体才返回，无法交互式消费 SSE 长连接，
因此这里使用真实 uvicorn 服务器 + 网络客户端测试（更接近生产形态）。
"""
import asyncio
import json
import os
import uuid
from pathlib import Path

import httpx
import pytest
import uvicorn

from litework.app import AgentApp
from litework.server.app import create_app
from tests.conftest import MockLLMAdapter, tool_call


@pytest.fixture
async def live_client(tmp_path):
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    app._mock_adapter = MockLLMAdapter([
        ("", [tool_call("write_file", '{"filePath":"x.txt","content":"hello"}', cid="c1")]),
        ("完成", []),
    ])
    # 每个测试的 config_dir 都是独立 tmp_path，models.dev 缓存永远不命中，
    # lifespan 后台线程会真实发起网络请求（最长 10s）；关闭事件循环时
    # shutdown_default_executor() 要等这个线程结束，单个测试 teardown 被拖到 5s+。
    # 测试不依赖在线元数据，直接短路掉。
    app.refresh_model_meta = lambda: False
    fast_app = create_app(app, token=None)
    config = uvicorn.Config(
        fast_app, host="127.0.0.1", port=0, log_level="error",
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    # models.dev 同步已在上面短路，lifespan 启动只含 mcp_manager.start()，应当秒起
    for _ in range(400):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "uvicorn 未在预期时间内启动"
    port = server.servers[0].sockets[0].getsockname()[1]

    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=15) as c:
        yield c, app, server

    server.should_exit = True
    await asyncio.wait_for(server_task, timeout=10)


async def _consume_sse(stream, on_event=None):
    """消费 SSE 文本流，可选回调（用于流内审批）。"""
    events = []
    async for chunk in stream.aiter_text():
        for line in chunk.split("\n"):
            line = line.strip()
            if line.startswith("data: [DONE]"):
                return events
            if line.startswith("data: "):
                ev = json.loads(line[6:])
                events.append(ev)
                if on_event:
                    await on_event(ev)
    return events


async def test_status_and_sessions(live_client):
    c, app, _ = live_client
    r = await c.get("/api/status")
    assert r.status_code == 200
    assert r.json()["version"] == __import__("litework").__version__  # 与包版本一致即可

    r = await c.post("/api/sessions", json={"name": "会话A"})
    assert r.status_code == 200
    sid = r.json()["session_id"]

    r = await c.get("/api/sessions")
    assert all(s["session_id"] != sid for s in r.json())
    r = await c.delete(f"/api/sessions/{sid}")
    assert r.status_code == 200
    r = await c.get("/api/sessions")
    assert all(s["session_id"] != sid for s in r.json())


async def test_sessions_list_strictly_scoped_to_workspace(live_client):
    """会话列表严格绑定项目：无 workspace 绑定（旧版）与其它项目的会话不显示。"""
    c, app, _ = live_client
    ws = app.workspace

    # 1. 正常会话（带首条消息，绑定当前 workspace）→ 显示
    r = await c.post("/api/sessions", json={})
    bound = r.json()["session_id"]
    await c.post("/api/chat", json={"session_id": bound, "prompt": "你好"})
    r = await c.get("/api/sessions", params={"workspace": ws})
    ids = [s["session_id"] for s in r.json()]
    assert bound in ids

    # 2. 其它项目的会话 → 不显示
    other_ws = str(Path(ws).parent / "other-project")
    r = await c.post("/api/sessions", json={"workspace": other_ws})
    other = r.json()["session_id"]
    await c.post("/api/chat", json={"session_id": other, "prompt": "别处的会话"})
    r = await c.get("/api/sessions", params={"workspace": ws})
    ids = [s["session_id"] for s in r.json()]
    assert other not in ids

    # 3. 旧版无 workspace 元数据的会话 → 不显示（点击无法切到对应项目，显示会造成语义错位）
    from litework.core.types import Message
    legacy = f"session_legacy_{uuid.uuid4().hex[:8]}"
    app.session_store.save(legacy, [Message(role="user", content="旧版会话")], {})
    r = await c.get("/api/sessions", params={"workspace": ws})
    ids = [s["session_id"] for s in r.json()]
    assert legacy not in ids


async def test_rapid_session_creation_does_not_overwrite(live_client):
    c, _, _ = live_client
    responses = await asyncio.gather(
        c.post("/api/sessions", json={"name": "A"}),
        c.post("/api/sessions", json={"name": "B"}),
    )
    ids = {r.json()["session_id"] for r in responses}
    assert len(ids) == 2


async def test_session_model_override(live_client):
    c, app, _ = live_client
    app.llm_registry.providers["openai"]["api_key"] = "test-key"
    app.llm_registry.providers["openai"]["models"] = ["gpt-test"]
    r = await c.post("/api/sessions", json={"name": "模型会话"})
    sid = r.json()["session_id"]

    r = await c.post(f"/api/sessions/{sid}/model", json={"provider": "openai", "model": "gpt-test"})
    assert r.status_code == 200
    assert r.json()["override"] == {"provider": "openai", "model": "gpt-test"}

    r = await c.get(f"/api/sessions/{sid}/model")
    assert r.json()["effective"] == {"provider": "openai", "model": "gpt-test"}

    r = await c.post(f"/api/sessions/{sid}/model", json={})
    assert r.status_code == 200
    assert r.json()["override"] is None

    r = await c.post(f"/api/sessions/{sid}/model", json={"provider": "openai", "model": "not-configured"})
    assert r.status_code == 400


def test_default_config_dir_is_stable_across_workspaces(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    if os.name == "nt":
        # Windows 上 expanduser 优先读 USERPROFILE，HOME 会被忽略
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.delenv("HOMEDRIVE", raising=False)
        monkeypatch.delenv("HOMEPATH", raising=False)
    first_workspace = tmp_path / "project-a"
    second_workspace = tmp_path / "project-b"

    first = AgentApp(workspace=str(first_workspace))
    first.llm_registry.providers["openai"]["api_key"] = "persisted-key"
    first._persist_config()

    second = AgentApp(workspace=str(second_workspace))
    assert first.config_dir == second.config_dir == str(home / ".lite-work")
    assert second.llm_registry.providers["openai"]["api_key"] == "persisted-key"


async def test_chat_sse_flow(live_client):
    c, app, _ = live_client
    r = await c.post("/api/sessions", json={"name": "s"})
    sid = r.json()["session_id"]

    r = await c.post("/api/chat", json={"session_id": sid, "prompt": "创建文件"})
    assert r.status_code == 200
    task_id = r.json()["task_id"]

    async with c.stream("GET", f"/api/tasks/{task_id}/events") as stream:
        events = await _consume_sse(stream)

    types = [e["type"] for e in events]
    assert "task:start" in types
    assert "llm:stream" in types
    assert "message:added" in types
    assert "context:stats" in types
    assert any(e["type"] == "tool:before_execute" and e["data"]["toolName"] == "write_file"
               for e in events)
    assert types[-1] == "task:done"
    done = next(e for e in events if e["type"] == "task:done")
    assert done["data"]["content"] == "完成"

    # 上下文统计事件：任务内 + 会话累计
    ctx = next(e for e in events if e["type"] == "context:stats")
    assert ctx["data"]["context_window"] == 1_000_000
    assert ctx["data"]["task"]["prompt_tokens"] >= 0
    assert ctx["data"]["session"]["prompt_tokens"] >= 0

    # 会话累计接口可查
    r = await c.get(f"/api/context/stats?session_id={sid}")
    assert r.json()["session"]["prompt_tokens"] >= 0

    # 会话已持久化，且文件真实写入
    r = await c.get(f"/api/sessions/{sid}")
    assert len(r.json()["messages"]) >= 5
    with open(os.path.join(app.workspace, "x.txt"), encoding="utf-8") as f:
        assert f.read() == "hello"


async def test_multiturn_history_preserved(live_client):
    """多轮对话：第二轮不能覆盖第一轮，标题保持为第一个问题。"""
    c, app, _ = live_client
    r = await c.post("/api/sessions", json={})
    sid = r.json()["session_id"]

    async def _chat(prompt):
        r = await c.post("/api/chat", json={"session_id": sid, "prompt": prompt})
        task_id = r.json()["task_id"]
        async with c.stream("GET", f"/api/tasks/{task_id}/events") as stream:
            await _consume_sse(stream)

    await _chat("第一个问题")
    await _chat("第二个问题")

    r = await c.get(f"/api/sessions/{sid}")
    snap = r.json()
    user_msgs = [m["content"] for m in snap["messages"] if m["role"] == "user"]
    assert user_msgs == ["第一个问题", "第二个问题"], user_msgs
    # 首个 user 消息仍在，说明历史没有被第二轮覆盖
    assert any(m["role"] == "assistant" and m.get("content") for m in snap["messages"])

    r = await c.get("/api/sessions")
    entry = next(s for s in r.json() if s["session_id"] == sid)
    assert entry["title"] == "第一个问题", entry["title"]


async def test_stop_task(live_client):
    c, app, _ = live_client
    r = await c.post("/api/sessions", json={"name": "s"})
    sid = r.json()["session_id"]
    r = await c.post("/api/chat", json={"session_id": sid, "prompt": "创建文件"})
    task_id = r.json()["task_id"]

    r = await c.post(f"/api/tasks/{task_id}/stop")
    assert r.status_code == 200

    async with c.stream("GET", f"/api/tasks/{task_id}/events") as stream:
        events = await _consume_sse(stream)
    types = [e["type"] for e in events]
    assert types[-1] == "task:done" or types[-1] == "task:error"


async def test_approval_flow(live_client):
    """中危命令 → approval:request → 流内用户批准 → approval:resolved → 工具执行。"""
    c, app, _ = live_client
    app._mock_adapter = MockLLMAdapter([
        ("", [tool_call("execute_command", '{"command":"rm temp.txt"}', cid="c2")]),
        ("删好了。", []),
    ])

    r = await c.post("/api/sessions", json={"name": "s2"})
    sid = r.json()["session_id"]
    r = await c.post("/api/chat", json={"session_id": sid, "prompt": "删除文件"})
    task_id = r.json()["task_id"]

    resolved_ids = []

    async def _on_event(ev):
        if ev["type"] == "approval:request":
            apv_id = ev["data"]["id"]
            resp = await c.post("/api/approve", json={"approval_id": apv_id, "approved": True})
            assert resp.status_code == 200
            resolved_ids.append(apv_id)

    async with c.stream("GET", f"/api/tasks/{task_id}/events") as stream:
        events = await _consume_sse(stream, on_event=_on_event)

    assert resolved_ids, "应产生审批请求并被批准"
    assert any(e["type"] == "approval:resolved" and e["data"]["approved"] for e in events)
    assert any(e["type"] == "tool:before_execute"
               and e["data"]["toolName"] == "execute_command" for e in events)
    assert events[-1]["type"] == "task:done"

    # 已 resolve 的审批再次提交应 404
    r = await c.post("/api/approve", json={"approval_id": resolved_ids[0], "approved": True})
    assert r.status_code == 404


async def test_session_agents_endpoint(live_client):
    """/api/sessions/{session_id}/agents 返回子 Agent 状态（含 running/completed/errored）。"""
    c, app, _ = live_client
    r = await c.post("/api/sessions", json={"name": "agent测试"})
    sid = r.json()["session_id"]
    from litework.orchestration.agent_manager import AgentRecord, SessionAgentManager

    # 直接往 manager 注入两条记录模拟已派生的子 Agent
    mgr = app.agent_manager(sid)
    mgr.agents["sa_test1"] = AgentRecord(
        agent_id="sa_test1", nickname="test-1", role="explorer",
        task="调研报告", status="completed", summary="完成",
    )
    mgr.agents["sa_test2"] = AgentRecord(
        agent_id="sa_test2", nickname="test-2", role="explorer",
        task="代码审查", status="running",
    )
    r = await c.get(f"/api/sessions/{sid}/agents")
    assert r.status_code == 200
    data = r.json()
    assert len(data["agents"]) == 2
    ids = {a["agent_id"] for a in data["agents"]}
    assert ids == {"sa_test1", "sa_test2"}


async def test_security_hot_reload(live_client):
    c, app, _ = live_client
    r = await c.get("/api/security")
    assert r.status_code == 200
    rules = r.json()
    assert "high_risk_patterns" in rules

    r = await c.post("/api/security", json={"rules": {
        **rules,
        "high_risk_patterns": [r"\bcustom-block\b"],
    }})
    assert r.status_code == 200
    assert app.guard.check_shell_command("custom-block xyz").level.value == "HIGH"
