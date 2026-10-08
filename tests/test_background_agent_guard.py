# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""MCP 热重载与工作区切换的后台 Agent 守卫测试。

后台子 Agent 生命周期超出主任务（spawn_agent 异步派生）：
- MCP reload 会 close 它们 registry 捕获的旧 MCPClient → 调用即断管道；
- 工作区切换会移走其 worktree/路径基准。
两处守卫此前只数主任务（tasks.active_count），此处覆盖后台 Agent 场景。
"""
from __future__ import annotations

import os
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


# --------------------------------------------- 工作区切换 × Shell 实例缓存
# 设计不变式（方案 1）：
#   1) ShellPlugin 按 workspace 为键缓存（AgentApp._shell_plugins）——缓存键就是目录，
#      切换工作区必然拿到新目录的实例，不可能复用旧目录实例（历史 bug 的根因）；
#   2) 后台命令注册表 app 级共享（AgentApp._bg_registry）——任务归属与工作区解耦，
#      切换后旧 task_id 仍可在任意工作区查询/终止。
# 两者合起来：execute_command 的 cwd 不可能陈旧，且切换不会让后台任务失联。


def _shell_for(app, workspace):
    """取指定工作区对应的 ShellPlugin（顺带触发一次装配）。"""
    app.workspace = str(workspace)
    app.tool_plugins()
    return app._shell_plugins[os.path.abspath(str(workspace))]


def test_shell_plugin_is_cached_per_workspace(client_and_app, tmp_path):
    """键即目录：A/B 各一份实例并存，切回 A 复用 A 的实例（不重建、不串目录）。"""
    client, app = client_and_app
    dir_a, dir_b = tmp_path / "projA", tmp_path / "projB"
    dir_a.mkdir()
    dir_b.mkdir()

    shell_a = _shell_for(app, dir_a)
    assert shell_a._tools.workspace == os.path.abspath(str(dir_a))

    shell_b = _shell_for(app, dir_b)
    assert shell_b is not shell_a
    assert shell_b._tools.workspace == os.path.abspath(str(dir_b))
    assert set(app._shell_plugins) == {os.path.abspath(str(dir_a)),
                                      os.path.abspath(str(dir_b))}
    # 切回 A：命中同一实例，其 cwd 仍是 A（缓存键=目录，不可能错位）
    assert _shell_for(app, dir_a) is shell_a


def test_workspace_switch_binds_new_directory(client_and_app, tmp_path):
    """HTTP 切换工作区后，shell 实例绑定新目录（execute_command 的 cwd 来源）。"""
    client, app = client_and_app
    dir_a, dir_b = tmp_path / "projA", tmp_path / "projB"
    dir_a.mkdir()
    dir_b.mkdir()
    app.workspace = str(dir_a)
    app.tool_plugins()

    r = client.post("/api/workspace", json={"path": str(dir_b)})
    assert r.status_code == 200
    app.tool_plugins()
    shell_b = app._shell_plugins[os.path.abspath(str(dir_b))]
    assert shell_b._tools.workspace == os.path.abspath(str(dir_b))


def test_recent_project_open_binds_new_directory(client_and_app, tmp_path):
    """POST /api/projects/recent（UI「打开项目」主路径）同样绑定新目录。"""
    client, app = client_and_app
    dir_a, dir_b = tmp_path / "projA", tmp_path / "projB"
    dir_a.mkdir()
    dir_b.mkdir()
    app.workspace = str(dir_a)
    app.tool_plugins()

    r = client.post("/api/projects/recent", json={"path": str(dir_b)})
    assert r.status_code == 200
    app.tool_plugins()
    shell_b = app._shell_plugins[os.path.abspath(str(dir_b))]
    assert shell_b._tools.workspace == os.path.abspath(str(dir_b))


def test_background_command_survives_workspace_switch(client_and_app, tmp_path):
    """切换工作区不再拦截，且旧工作区的后台任务仍可列出 + check_command 到。"""
    from litework.tools.shell import _BackgroundTask

    client, app = client_and_app
    dir_a, dir_b = tmp_path / "projA", tmp_path / "projB"
    dir_a.mkdir()
    dir_b.mkdir()
    _shell_for(app, dir_a)
    app._bg_registry.add("t-1", _BackgroundTask("sleep 100"))

    # 旧行为是 409 拦截（丢弃实例会让 task_id 失联）；现在注册表共享，放行
    r = client.post("/api/workspace", json={"path": str(dir_b)})
    assert r.status_code == 200
    assert app.workspace == os.path.abspath(str(dir_b))

    # 任务面板仍看得到（与当前工作区无关）
    tasks = app.background_tasks()
    assert [t["task_id"] for t in tasks] == ["t-1"]
    assert tasks[0]["running"] is True

    # 新工作区的 shell 工具也能查到它（check_command 不失联）
    out = _shell_for(app, dir_b)._tools._check_command({"task_id": "t-1"})
    assert "未找到" not in out
    assert "[running]: True" in out


def test_kill_background_task_reaches_other_workspace(client_and_app, tmp_path, monkeypatch):
    """kill 走共享注册表：在 B 工作区可终止 A 工作区启动的任务。"""
    from types import SimpleNamespace

    from litework.tools import shell as shell_mod
    from litework.tools.shell import _BackgroundTask

    killed = []
    monkeypatch.setattr(shell_mod, "kill_process_tree", lambda pid: killed.append(pid))

    client, app = client_and_app
    dir_a, dir_b = tmp_path / "projA", tmp_path / "projB"
    dir_a.mkdir()
    dir_b.mkdir()
    _shell_for(app, dir_a)
    task = _BackgroundTask("sleep 100")
    task.proc = SimpleNamespace(pid=4242)  # 假进程：kill_process_tree 已被替换
    app._bg_registry.add("t-2", task)

    client.post("/api/workspace", json={"path": str(dir_b)})  # 切到 B
    assert app.kill_background_task("t-2") is True
    assert killed == [4242]


async def test_execute_command_cwd_follows_workspace_switch(client_and_app, tmp_path):
    """端到端回归（原 bug 现象）：切换工作区后 execute_command 实际 pwd 必须跟随。

    此前缓存的 ShellPlugin 绑定旧目录 → 切项目后 pwd 仍是旧项目。
    """
    def pwd_of(output: str) -> str:
        for line in output.splitlines():
            if line.startswith("/"):
                return line.strip()
        raise AssertionError(f"未从输出解析出路径: {output!r}")

    client, app = client_and_app
    dir_a, dir_b = tmp_path / "projA", tmp_path / "projB"
    dir_a.mkdir()
    dir_b.mkdir()

    shell_a = _shell_for(app, dir_a)
    out_a = await shell_a._tools.execute("execute_command", {"command": "pwd"})
    assert os.path.realpath(pwd_of(out_a)) == os.path.realpath(str(dir_a))

    # 经 UI 主路径切换项目
    assert client.post("/api/projects/recent", json={"path": str(dir_b)}).status_code == 200

    shell_b = _shell_for(app, dir_b)
    out_b = await shell_b._tools.execute("execute_command", {"command": "pwd"})
    assert os.path.realpath(pwd_of(out_b)) == os.path.realpath(str(dir_b))
    assert os.path.realpath(pwd_of(out_b)) != os.path.realpath(str(dir_a))
