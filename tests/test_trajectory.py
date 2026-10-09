# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""Agent 轨迹测试（W7）。

覆盖：
1. 写入器：header/step/tool_call/outcome 的事件结构与 step_id 递增；
2. 读取：分页 / 文件缺失 / 损坏行跳过；
3. 会话列表：列出所有轨迹任务；
4. 分析（级联漏斗第一步）：重试风暴 / 成本异常 / 上下文膨胀 / 正常无异常；
5. 集成：trajectory_enabled=true 时 loop 写轨迹，false 时不写。
"""
from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from litework.app import AgentApp
from litework.core.agent_loop import AgentLoop
from litework.core.kernel import Kernel
from litework.core.trajectory import (
    TrajectoryWriter,
    analyze_trajectory,
    list_session_trajectories,
    read_trajectory,
)
from litework.core.session_store import SessionStore
from litework.tools.registry import ToolRegistry
from litework.server.app import create_app
from tests.conftest import MockLLMAdapter, tool_call


# ---------------------------------------------------------------- 写入器

def test_writer_creates_events(tmp_path):
    w = TrajectoryWriter(str(tmp_path), "sess-1", "task-1", workspace="/ws", agent_id="build")
    w.write_header()
    w.step(turn=1)
    w.tool_call(turn=1, tool="read_file", outcome="ok", duration_ms=12.5)
    w.tool_call(turn=1, tool="write_file", outcome="error")
    w.outcome(verdict="unknown", cost_estimate=0.05, turns=1)

    events = read_trajectory(str(tmp_path), "sess-1", "task-1")
    assert len(events) == 5
    assert events[0]["type"] == "header"
    assert events[0]["session_id"] == "sess-1"
    assert events[0]["trace_id"]  # trace_id 存在
    assert events[1]["type"] == "step"
    assert events[2]["type"] == "tool_call" and events[2]["tool"] == "read_file"
    assert events[3]["outcome"] == "error"
    assert events[4]["type"] == "outcome" and events[4]["verdict"] == "unknown"
    # step_id 递增
    ids = [e["step_id"] for e in events[1:]]
    assert ids == sorted(ids)


def test_writer_disabled(tmp_path):
    w = TrajectoryWriter(str(tmp_path), "s", "t")
    w.enabled = False
    w.write_header()
    w.step(turn=1)
    assert read_trajectory(str(tmp_path), "s", "t") == []


def test_writer_redacts_args(tmp_path):
    """args_hint 经 redact_evidence 脱敏（凭证不进轨迹）。"""
    w = TrajectoryWriter(str(tmp_path), "s", "t")
    w.tool_call(turn=1, tool="execute_command", args_hint="token sk-abcdef0123456789abcdef")
    events = read_trajectory(str(tmp_path), "s", "t")
    assert "sk-abcdef" not in json.dumps(events)


# ---------------------------------------------------------------- 读取

def test_read_missing_file(tmp_path):
    assert read_trajectory(str(tmp_path), "no", "no") == []


def test_read_pagination(tmp_path):
    w = TrajectoryWriter(str(tmp_path), "s", "t")
    for i in range(10):
        w.step(turn=i)
    # 分页
    page1 = read_trajectory(str(tmp_path), "s", "t", offset=0, limit=5)
    page2 = read_trajectory(str(tmp_path), "s", "t", offset=5, limit=5)
    assert len(page1) == 5 and len(page2) == 5
    assert page1[0]["turn"] == 0 and page2[0]["turn"] == 5


def test_read_skips_bad_lines(tmp_path):
    path = tmp_path / "trajectories" / "s" / "t.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"type":"step","step_id":1}\nnot json\n{"type":"step","step_id":2}\n', encoding="utf-8")
    events = read_trajectory(str(tmp_path), "s", "t")
    assert len(events) == 2  # 坏行跳过


def test_list_session_trajectories(tmp_path):
    TrajectoryWriter(str(tmp_path), "sess-A", "task-1").write_header()
    TrajectoryWriter(str(tmp_path), "sess-A", "task-2").write_header()
    result = list_session_trajectories(str(tmp_path), "sess-A")
    assert len(result) == 2
    assert {r["task_id"] for r in result} == {"task-1", "task-2"}
    assert list_session_trajectories(str(tmp_path), "no-such") == []


# ---------------------------------------------------------------- 分析（级联漏斗第一步）

def test_analyze_retry_storm():
    events = [
        {"type": "tool_call", "tool": "execute_command", "outcome": "error"},
        {"type": "tool_call", "tool": "execute_command", "outcome": "error"},
        {"type": "tool_call", "tool": "execute_command", "outcome": "error"},
    ]
    findings = analyze_trajectory(events)
    assert any(f["rule"] == "retry_storm" and f["tool"] == "execute_command" for f in findings)


def test_analyze_no_storm_when_interleaved():
    """失败-成功-失败-成功 → 不算连续风暴。"""
    events = [
        {"type": "tool_call", "tool": "t", "outcome": "error"},
        {"type": "tool_call", "tool": "t", "outcome": "ok"},
        {"type": "tool_call", "tool": "t", "outcome": "error"},
        {"type": "tool_call", "tool": "t", "outcome": "ok"},
    ]
    assert analyze_trajectory(events) == []


def test_analyze_cost_outlier():
    events = [{"type": "outcome", "cost_estimate": 10.0}]
    findings = analyze_trajectory(events)
    assert any(f["rule"] == "cost_outlier" for f in findings)


def test_analyze_context_bloat():
    events = [{"type": "state_change", "change": "compact:session"} for _ in range(7)]
    findings = analyze_trajectory(events)
    assert any(f["rule"] == "context_bloat" and f["compactions"] == 7 for f in findings)


def test_analyze_clean_trajectory():
    events = [
        {"type": "tool_call", "tool": "read_file", "outcome": "ok"},
        {"type": "tool_call", "tool": "write_file", "outcome": "ok"},
        {"type": "outcome", "cost_estimate": 0.05},
    ]
    assert analyze_trajectory(events) == []


# ---------------------------------------------------------------- 集成：loop 写轨迹

async def test_loop_writes_trajectory(tmp_path):
    """trajectory 传入时 loop 写工具调用与 outcome。"""
    registry = ToolRegistry()

    async def read_file(args):
        return "[Command OK] 内容"

    registry.register("read_file", "读文件", {"type": "object"}, read_file)

    adapter = MockLLMAdapter([
        ("", [tool_call("read_file", '{"filePath":"a.txt"}')]),
        ("完成。", []),
    ])
    trajectory = TrajectoryWriter(str(tmp_path), "traj-sess", "traj-task")
    kernel = Kernel("traj-sess")
    loop = AgentLoop(kernel=kernel, adapter=adapter, registry=registry,
                     session_store=SessionStore(str(tmp_path / "sessions")),
                     max_steps=5, trajectory=trajectory)
    loop.workspace = str(tmp_path)
    loop.completion_gate = "off"

    await loop.run_task("读个文件", system_prompt="测试")

    events = read_trajectory(str(tmp_path), "traj-sess", "traj-task")
    types = [e["type"] for e in events]
    assert "tool_call" in types
    assert "outcome" in types
    # 工具事件带 outcome
    tc = next(e for e in events if e["type"] == "tool_call")
    assert tc["tool"] == "read_file" and tc["outcome"] == "ok"


async def test_loop_without_trajectory_is_noop(tmp_path):
    """trajectory=None 时零开销、不产生文件。"""
    registry = ToolRegistry()

    async def read_file(args):
        return "ok"

    registry.register("read_file", "读文件", {"type": "object"}, read_file)
    adapter = MockLLMAdapter([
        ("", [tool_call("read_file", '{"filePath":"a"}')]),
        ("完成。", []),
    ])
    kernel = Kernel("no-traj")
    loop = AgentLoop(kernel=kernel, adapter=adapter, registry=registry, max_steps=5)
    loop.workspace = str(tmp_path)
    loop.completion_gate = "off"
    result, _ = await loop.run_task("读文件", system_prompt="测试")
    assert result == "完成。"
    assert list_session_trajectories(str(tmp_path), "no-traj") == []


# ---------------------------------------------------------------- API

def test_trajectory_api(tmp_path):
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    # 写一条轨迹
    w = TrajectoryWriter(app.config_dir, "api-sess", "api-task")
    w.write_header()
    w.tool_call(turn=1, tool="read_file", outcome="ok")

    fast = create_app(app, token=None)
    with TestClient(fast) as client:
        # 列表
        r1 = client.get("/api/trajectories/api-sess")
        assert r1.status_code == 200
        assert r1.json()["enabled"] is False  # 默认关
        assert len(r1.json()["trajectories"]) == 1

        # 事件 + findings
        r2 = client.get("/api/trajectories/api-sess", params={"task_id": "api-task"})
        assert r2.status_code == 200
        body = r2.json()
        assert body["count"] == 2  # header + tool_call
        assert "findings" in body

        # 不存在的 session
        r3 = client.get("/api/trajectories/no-such")
        assert r3.status_code == 200 and r3.json()["trajectories"] == []
