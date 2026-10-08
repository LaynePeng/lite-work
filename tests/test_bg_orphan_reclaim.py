# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""后台命令孤儿回收测试（W1）。

场景：后端崩溃/断连后，Agent 启动的后台进程失去管理者，继续占用 CPU/端口/文件锁。
启动时按任务日志回收：只杀日志里记录过、且"存活时间与记录吻合"的 pid
（防 pid 复用误杀）；日志文件缺失/损坏不得影响启动。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from litework.app import AgentApp
from litework.server.app import create_app
from litework.tools.proc_utils import posix_spawn_kwargs
from litework.tools.shell import (
    BackgroundRegistry,
    _BackgroundTask,
    reclaim_orphaned_tasks,
)


def _alive(pid: int) -> bool:
    """进程是否仍存在（等价于 shell._pid_alive，测试侧自己实现避免自证）。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.fixture
def sleeper():
    """启动一个真实的长命子进程；测试结束兜底清理。"""
    procs = []

    def start(seconds: int = 30):
        p = subprocess.Popen(  # noqa: S603 - 测试内固定命令
            [sys.executable, "-c", f"import time; time.sleep({seconds})"],
            **posix_spawn_kwargs(),
        )
        procs.append(p)
        return p

    yield start
    for p in procs:
        if p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass


# ---------------------------------------------------------------- 任务日志

def test_registry_journal_roundtrip(tmp_path):
    journal = str(tmp_path / "bg_tasks.json")
    reg = BackgroundRegistry(journal)
    task = _BackgroundTask("sleep 100")
    task.proc = type("P", (), {"pid": 4242})()  # 假进程，仅取 pid

    reg.add("t1", task, workspace="/ws/a")
    data = json.loads(open(journal, encoding="utf-8").read())
    assert data["tasks"][0]["task_id"] == "t1"
    assert data["tasks"][0]["pid"] == 4242
    assert data["tasks"][0]["workspace"] == "/ws/a"
    assert data["tasks"][0]["started_at"] > 0

    reg.remove("t1")
    assert json.loads(open(journal, encoding="utf-8").read())["tasks"] == []


def test_registry_without_journal_path_does_not_write(tmp_path):
    """未配置日志路径时静默跳过（兼容单实例/测试构造）。"""
    reg = BackgroundRegistry()
    reg.add("t1", _BackgroundTask("x"))
    assert list(reg.tasks) == ["t1"]


# ---------------------------------------------------------------- 回收判定

def test_reclaim_kills_matching_live_process(tmp_path, sleeper):
    journal = str(tmp_path / "bg_tasks.json")
    p = sleeper(30)
    started = time.time()
    journal_data = {"tasks": [{
        "task_id": "t-orphan", "pid": p.pid, "command": "sleep 30",
        "workspace": str(tmp_path), "started_at": started,
    }]}
    open(journal, "w", encoding="utf-8").write(json.dumps(journal_data))

    result = reclaim_orphaned_tasks(journal)

    assert result["checked"] == 1
    assert result["killed"] == 1
    assert p.pid in result["killed_pids"]
    # 进程树被强杀（POSIX：对进程组发 SIGKILL）。注意：被杀的进程在父进程 wait
    # 之前是僵尸（kill(pid,0) 仍成功），所以这里用 wait 回收并看退出码。
    assert p.wait(timeout=10) != 0
    # 日志被清空，避免下次重复判定
    assert not os.path.exists(journal)


def test_reclaim_skips_pid_reuse(tmp_path, sleeper):
    """存活时间与记录不符（pid 已被复用给别的进程）→ 绝不误杀。"""
    journal = str(tmp_path / "bg_tasks.json")
    p = sleeper(30)
    journal_data = {"tasks": [{
        "task_id": "t-stale", "pid": p.pid, "command": "sleep 30",
        "workspace": str(tmp_path),
        "started_at": time.time() - 3600,  # 记录说 1 小时前启动，实际刚起
    }]}
    open(journal, "w", encoding="utf-8").write(json.dumps(journal_data))

    result = reclaim_orphaned_tasks(journal)

    assert result["killed"] == 0
    assert result["skipped"] == 1
    assert _alive(p.pid)  # 仍在运行


def test_reclaim_skips_dead_process_and_missing_journal(tmp_path):
    journal = str(tmp_path / "bg_tasks.json")
    # 不存在的 pid（用一个已结束的进程 pid）
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    open(journal, "w", encoding="utf-8").write(json.dumps({"tasks": [{
        "task_id": "t-dead", "pid": p.pid, "command": "x",
        "workspace": "", "started_at": time.time(),
    }]}))
    result = reclaim_orphaned_tasks(journal)
    assert result == {"checked": 1, "killed": 0, "killed_pids": [], "skipped": 1}

    # 日志缺失 / 损坏：返回空摘要且不抛
    assert reclaim_orphaned_tasks(str(tmp_path / "nope.json"))["checked"] == 0
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert reclaim_orphaned_tasks(str(bad))["checked"] == 0


# ---------------------------------------------------------------- 接口层

def test_status_exposes_running_background(tmp_path, sleeper):
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    fast = create_app(app, token=None)
    with TestClient(fast) as client:
        assert client.get("/api/status").json()["running_background"] == 0

        task = _BackgroundTask("sleep 30")
        task.proc = sleeper(30)
        app._bg_registry.add("t-run", task, workspace=str(tmp_path))
        assert client.get("/api/status").json()["running_background"] == 1

        task.done = True  # 完成后不计入 running
        assert client.get("/api/status").json()["running_background"] == 0
