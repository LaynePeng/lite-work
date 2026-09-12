# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors
#
"""delete_file 工具：不可逆删除走审批 + 会话删除时停止后台子 Agent。"""
import asyncio
import os

from litework.app import AgentApp
from litework.core.agent_loop import AgentLoop
from litework.core.types import ToolCall


class FakeGate:
    def __init__(self):
        self.pending = {}
        self._n = 0

    def request_approval(self, action, reason, **kwargs):
        self._n += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[f"ap{self._n}"] = fut
        return fut

    def current_id(self, fut):
        for aid, f in self.pending.items():
            if f is fut:
                return aid
        return ""


class DeleteMock:
    def __init__(self):
        self.calls = 0

    async def chat_stream(self, messages, tools, events=None):
        self.calls += 1
        if self.calls == 1:
            return "", [ToolCall(id="d1", name="delete_file",
                                 arguments='{"filePath": "a.md"}')], None
        return "已删除", [], None


def _make_app(tmp_path, gate):
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    app.refresh_model_meta = lambda: False
    app.approval_gate = gate
    return app


async def _run_delete(app, session_id):
    app._mock_adapter = DeleteMock()
    kernel = app.create_kernel(session_id)
    received = []
    kernel.events.on("approval:request", lambda p: received.append(p))
    loop = AgentLoop(kernel=kernel, adapter=app._mock_adapter,
                     registry=app.build_registry(), auto_approve=False)
    loop.workspace = app.workspace
    task = asyncio.get_running_loop().create_task(
        loop.run_task("删除", store_snapshot=False))
    for _ in range(100):
        if received:
            break
        await asyncio.sleep(0.05)
    assert received, "delete_file 未触发审批"
    return task, received[0]


async def test_delete_file_requires_approval(tmp_path):
    (tmp_path / "a.md").write_text("x")
    gate = FakeGate()
    app = _make_app(tmp_path, gate)
    task, payload = await _run_delete(app, "s1")
    assert "删除" in payload["action"]
    gate.pending[payload["id"]].set_result(True)
    await asyncio.wait_for(task, timeout=10)
    assert not (tmp_path / "a.md").exists()


async def test_delete_file_rejected_keeps_file(tmp_path):
    (tmp_path / "a.md").write_text("x")
    gate = FakeGate()
    app = _make_app(tmp_path, gate)
    task, payload = await _run_delete(app, "s1")
    gate.pending[payload["id"]].set_result(False)
    await asyncio.wait_for(task, timeout=10)
    assert (tmp_path / "a.md").exists()


def test_delete_file_in_write_scope_tools():
    from litework.orchestration.sub_agent import WRITE_SCOPE_TOOLS
    assert "delete_file" in WRITE_SCOPE_TOOLS


def test_build_agent_has_delete_file():
    from litework.core.agent_profile import default_build_agent
    from litework.core.permissions import domain_of

    # 职责域模型：build 的 edit 域 allow → delete_file 可用
    assert default_build_agent().domains.get("edit") == "allow"
    assert domain_of("delete_file") == "edit"


async def test_delete_session_stops_background_agents(tmp_path):
    from litework.server.routers.sessions import _shutdown_session_agents

    gate = FakeGate()
    app = _make_app(tmp_path, gate)
    app._mock_adapter = DeleteMock()
    mgr = app.agent_manager("s-leak")
    r = await mgr.spawn("长任务", role="general")
    aid = r["agent_id"]
    assert mgr.agents[aid].status == "running"

    await _shutdown_session_agents(app, "s-leak")
    assert mgr.agents[aid].status == "closed"
    assert "s-leak" not in app.agent_managers
