# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""「关闭对话 / 关闭项目」后的内存回收回归。

事故背景（用户报障）：关掉对话页签或切走项目后，Core 里对应的内存**不回收**，
要整个应用重启才干净——越用越吃内存。

根因不是一处，而是一整层缺失：TaskHandle 级别的回收（release/reap）早就有了，
但**按会话**与**按项目**驻留的结构没有任何释放入口：

- 按会话：`app.agent_managers[sid]`（子 Agent 的完整消息链，实测 30 会话 ×
  4 agent × 40 条 ≈ 24MB，绝对大头）、`_context_session_stats`、
  `approval_rules`、TODO 看板绑定；
- 按项目：`_shell_plugins`（按目录缓存）、`server/tree._git_cache`（模块级，
  永不淘汰）。

这些用例锁住三条不变量：
1. 关闭对话（release_session）放掉该会话的内存态，但**不删会话文件**；
2. 关闭项目（release_workspace / POST /api/workspace/close）放掉该项目的缓存；
3. 有任务在跑时拒绝释放（否则任务收尾无处落账）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from litework.app import AgentApp
from litework.core.types import Message
from litework.orchestration.agent_manager import AgentRecord
from litework.server import tree as tree_mod
from litework.server.app import create_app


AUTH = {"Authorization": "Bearer t"}


@pytest.fixture
def core(tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    app = AgentApp(workspace=str(ws), config_dir=str(tmp_path / "cfg"))
    fast = create_app(app, token="t")
    with TestClient(fast) as client:
        yield app, client, str(ws)


def _seed_session(app: AgentApp, ws: str, sid: str = "s1", *, agents: int = 2) -> None:
    """造一个「用过子 Agent 的会话」：会话文件 + manager 消息链 + 统计 + 审批规则。"""
    app.session_store.save(sid, [Message(role="user", content="hi")], {"workspace": ws})
    manager = app.agent_manager(sid)
    for i in range(agents):
        record = AgentRecord(agent_id=f"{sid}-a{i}", nickname=f"w{i}", role="general",
                             task="子任务", status="completed")
        record.messages = [Message(role="assistant", content="x" * 2000)] * 10
        manager.agents[record.agent_id] = record
    app.accumulate_context_stats(sid, {"prompt_tokens": 100, "output_tokens": 50})
    app.approval_rules.setdefault(sid, []).append(
        {"tool": "execute_command", "kind": "command_prefix", "pattern": "npm test"})


# ---------------------------------------------------------------- 会话级

def test_release_session_drops_memory_but_keeps_history(core):
    """关闭对话：内存态全放掉，会话文件原样保留（reopen 才有东西可读）。"""
    app, _client, ws = core
    _seed_session(app, ws, "s1")

    freed = app.release_session("s1")

    assert freed["agents"] is True
    assert "s1" not in app.agent_managers, "子 Agent 消息链仍驻留"
    assert "s1" not in app._context_session_stats
    assert "s1" not in app.approval_rules
    # 关键：不是删除会话
    assert app.session_store.load("s1") is not None, "release 不得删除会话文件"


def test_release_session_clears_agent_message_chains(core):
    """消息链是最大的一块：manager 被摘除时应已清空（防被其它引用钉住）。"""
    app, _client, ws = core
    _seed_session(app, ws, "s1", agents=3)
    manager = app.agent_managers["s1"]
    kept = list(manager.agents.values())

    app.release_session("s1")

    assert all(not r.messages for r in kept), "记录外层引用仍在，messages 必须清空"
    assert manager.agents == {}


def test_release_session_idempotent(core):
    """重复释放不报错（关页签 + 切项目都会触发同一条路径）。"""
    app, _client, ws = core
    _seed_session(app, ws, "s1")

    assert app.release_session("s1")["agents"] is True
    # 第二次：manager 已不在，其余键也不在 → 全是 False，但不抛
    freed = app.release_session("s1")
    assert freed["agents"] is False if "agents" in freed else True
    assert freed["context_stats"] is False


# ---------------------------------------------------------------- 项目级

def test_release_workspace_drops_shell_and_git_cache(core):
    """关闭项目：按目录缓存的 ShellPlugin 与 git 状态缓存都要放掉。"""
    app, _client, ws = core
    tree_mod.git_snapshot(ws)
    assert ws in tree_mod._git_cache
    app._shell_plugins[ws] = object()  # type: ignore[assignment]

    freed = app.release_workspace(ws)

    assert freed["git_cache"] is True
    assert freed["shell_plugin"] is True
    assert ws not in tree_mod._git_cache
    assert ws not in app._shell_plugins


def test_git_cache_is_bounded(core):
    """git 缓存不能在切过的每个项目上无限累积（模块级字典，进程级泄漏）。"""
    app, _client, ws = core
    tree_mod._git_cache.clear()
    # 伪造一批过期的旧目录条目
    for i in range(tree_mod.GIT_CACHE_MAX + 8):
        tree_mod._git_cache[f"/nonexistent/proj-{i}"] = (0.0, None, {})

    tree_mod.git_snapshot(ws)  # 写入触发淘汰

    assert len(tree_mod._git_cache) <= tree_mod.GIT_CACHE_MAX


# ---------------------------------------------------------------- 接口层

def test_release_endpoint_404_for_unknown_session(core):
    _app, client, _ws = core
    resp = client.post("/api/sessions/nope/release", headers=AUTH)
    assert resp.status_code == 404


def test_release_endpoint_409_while_task_running(core):
    """任务在跑时拒绝：收尾逻辑要往这份状态里落账，提前放掉会丢收尾信息。"""
    app, client, ws = core
    _seed_session(app, ws, "s1")

    class _Kernel:
        session_id = "s1"

    class _Handle:
        running = True
        stopping = False
        kernel = _Kernel()

    client.app.state.tasks.tasks["t1"] = _Handle()

    resp = client.post("/api/sessions/s1/release", headers=AUTH)

    assert resp.status_code == 409
    assert "运行" in resp.json()["detail"]
    # 内存态原样保留（没放掉）
    assert "s1" in app.agent_managers


def test_release_endpoint_409_while_subagent_running(core):
    """后台子 Agent 在跑也拒绝：它们不受 TaskHandle 管，释放会掐断正在干活的工作。"""
    app, client, ws = core
    _seed_session(app, ws, "s1", agents=1)
    record = next(iter(app.agent_managers["s1"].agents.values()))
    record.status = "running"

    resp = client.post("/api/sessions/s1/release", headers=AUTH)

    assert resp.status_code == 409
    assert "子 Agent" in resp.json()["detail"]


def test_close_workspace_endpoint_clears_view_and_caches(core):
    """POST /api/workspace/close：本端回到「未打开项目」+ 释放该项目资源。"""
    app, client, ws = core
    _seed_session(app, ws, "s1")
    tree_mod.git_snapshot(ws)

    resp = client.post("/api/workspace/close", headers={**AUTH, "X-Client-Id": "c1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["workspace"] == ""
    assert body["released"]["sessions"] >= 1

    # 该会话的内存态已放掉，但会话文件还在
    assert "s1" not in app.agent_managers
    assert app.session_store.load("s1") is not None
    # 该端的项目视图已清空（再取工作区应得到空串）
    status = client.get("/api/status", headers={**AUTH, "X-Client-Id": "c1"}).json()
    assert status["workspace"] == ""


def test_close_workspace_noop_without_project(core):
    """未打开项目时关闭是幂等 no-op（不该 500）。"""
    _app, client, _ws = core
    resp = client.post("/api/workspace/close",
                       headers={**AUTH, "X-Client-Id": "fresh-client"})
    assert resp.status_code == 200
    assert resp.json()["workspace"] == ""


# ---------------------------------------------------------------- 长期驻留层

def test_trim_finished_messages_keeps_record(core):
    """消息链按 TTL 裁剪，但**记录保留**（看板照常显示、followup 仍可用轻量历史）。"""
    app, _client, ws = core
    _seed_session(app, ws, "s1", agents=2)
    manager = app.agent_managers["s1"]
    for record in manager.agents.values():
        record.finished_at = 0.0  # 冒充很久以前结束

    trimmed = app.trim_finished_agent_messages()

    assert trimmed == 2
    assert len(manager.agents) == 2, "记录不能删（看板还要显示）"
    assert all(not r.messages for r in manager.agents.values())


def test_trim_keeps_running_agent_messages(core):
    """运行中的子 Agent 消息链绝不能被裁剪（续跑要用）。"""
    app, _client, ws = core
    _seed_session(app, ws, "s1", agents=1)
    record = next(iter(app.agent_managers["s1"].agents.values()))
    record.status = "running"
    record.finished_at = 0.0

    assert app.trim_finished_agent_messages() == 0
    assert record.messages, "运行中的上下文被裁掉了"
