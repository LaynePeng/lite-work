# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""多客户端视图分离（单用户多端共享一个 Core）的回归测试。

架构约定：
- 每端一个 X-Client-Id；切项目（POST /api/workspace）只登记该端视图，
  不再改全局 app.workspace——其他端不受影响；
- 任务启动时钉死**会话所属项目**（metadata.workspace），不读全局值；
- 同项目互斥（方案 A）：同项目已有任务在跑 → WorkspaceBusyError（429）；
  跨项目并行不受限；worktree 隔离任务不参与互斥。
"""
from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.testclient import TestClient

from litework.app import AgentApp
from litework.server.app import create_app
from litework.server.tasks import TaskManager, TaskQueued, WorkspaceBusyError


AUTH = {"Authorization": "Bearer t"}


@pytest.fixture
def two_projects(tmp_path):
    a = tmp_path / "proj-a"
    b = tmp_path / "proj-b"
    a.mkdir()
    b.mkdir()
    return str(a), str(b)


@pytest.fixture
def core(tmp_path, two_projects):
    """一个 Core + TestClient（全局 workspace 初始指向 proj-a）。"""
    a, _b = two_projects
    app = AgentApp(workspace=a, config_dir=str(tmp_path / "cfg"))
    fast = create_app(app, token="t")
    with TestClient(fast) as client:
        yield app, client


def _mk_session(app: AgentApp, ws: str, name: str = "s") -> str:
    sid = f"session_{name}"
    app.session_store.save(sid, [], {"workspace": ws})
    return sid


# ---------------------------------------------------------------- 视图分离

def test_switch_workspace_only_affects_that_client(core, two_projects):
    """带 X-Client-Id 的端切项目：只登记自己视图，全局与其他端不变。"""
    app, client = core
    a, b = two_projects
    assert app.workspace == a

    r = client.post("/api/workspace", json={"path": b},
                    headers={**AUTH, "X-Client-Id": "phone"})
    assert r.status_code == 200
    # 手机端看到 b
    assert client.get("/api/status", headers={**AUTH, "X-Client-Id": "phone"}
                      ).json()["workspace"] == b
    # 全局值不动（未登记端/旧客户端的兜底仍是 a）
    assert app.workspace == a
    assert client.get("/api/status", headers=AUTH).json()["workspace"] == a
    # 另一个端也看 a
    assert client.get("/api/status", headers={**AUTH, "X-Client-Id": "desktop"}
                      ).json()["workspace"] == a


def test_legacy_client_without_id_switches_globally(core, two_projects):
    """无 client-id 的旧客户端：退回全局切换语义（单端兼容）。"""
    app, client = core
    _a, b = two_projects
    r = client.post("/api/workspace", json={"path": b}, headers=AUTH)
    assert r.status_code == 200
    assert app.workspace == b


def test_status_workspace_resolves_per_client(core, two_projects):
    """require_workspace 系（如 /api/tools）按客户端视图解析。"""
    app, client = core
    a, b = two_projects
    client.post("/api/workspace", json={"path": b},
                headers={**AUTH, "X-Client-Id": "phone"})
    # 手机的会话树在 b 下能看到内容
    os.makedirs(os.path.join(b, "phone-only"), exist_ok=True)
    tree = client.get("/api/workspace/tree-json",
                      headers={**AUTH, "X-Client-Id": "phone"}).json()
    assert tree["workspace"] == b
    assert any(e["name"] == "phone-only" for e in tree["entries"])


def test_session_created_in_client_view(core, two_projects):
    """端上新建的会话归属该端当前视图（metadata.workspace）。"""
    app, client = core
    _a, b = two_projects
    client.post("/api/workspace", json={"path": b},
                headers={**AUTH, "X-Client-Id": "phone"})
    r = client.post("/api/sessions", json={},
                    headers={**AUTH, "X-Client-Id": "phone"})
    assert r.status_code == 200
    sid = r.json()["session_id"]
    snap = app.session_store.load(sid)
    assert snap is not None
    assert snap.metadata.get("workspace") == b


# ---------------------------------------------------------------- 任务钉工作区

def test_task_pins_session_workspace_not_global(core, two_projects):
    """任务的工作区取会话所属项目，不读全局值——任一端切项目不影响别的
    会话正在跑的任务。"""
    app, client = core
    a, b = two_projects
    sid = _mk_session(app, b, "in-b")
    # 全局（兜底）仍指向 a；任务应钉 b
    assert app.workspace == a
    tm = TaskManager(app)
    captured = {}

    def fake_registry(agent_id, workspace=None, unattended_profile=None, session_id=None):
        captured["workspace"] = workspace
        raise _Abort()

    orig_create = app.create_agent_registry
    app.create_agent_registry = fake_registry  # type: ignore[method-assign]
    try:
        tm.start(sid, "hi")
    except _Abort:
        pass
    finally:
        app.create_agent_registry = orig_create  # type: ignore[method-assign]
    assert captured["workspace"] == b
    assert captured["workspace"] != a


class _Abort(Exception):
    pass


def _fake_running_task(sid: str, ws: str):
    """一个「正在运行」的 TaskHandle 替身（只带互斥/排队判定需要的字段）。"""
    from litework.server.tasks import TaskHandle

    h = TaskHandle.__new__(TaskHandle)
    h.running = True
    h.stopping = False
    h.done = False
    h.released = True
    h.finished_at = None
    h._subscribers = []
    h.workspace = ws
    h.session_workspace = ws
    h.worktree_name = None
    h.kernel = type("K", (), {"session_id": sid})()
    return h


# ---------------------------------------------------------------- 同项目互斥

def test_same_project_rejected_while_running(core, two_projects):
    """同项目已有任务在跑 → WorkspaceBusyError；跨项目放行。"""
    app, client = core
    a, b = two_projects
    tm = app.__dict__.get("_test_tm") or TaskManager(app)

    from litework.server.tasks import TaskHandle

    def _fake_running_task(sid: str, ws: str) -> TaskHandle:
        h = TaskHandle.__new__(TaskHandle)
        h.running = True
        h.stopping = False
        h.workspace = ws
        h.session_workspace = ws
        h.worktree_name = None
        h.kernel = type("K", (), {"session_id": sid})()
        return h

    tm.tasks["t1"] = _fake_running_task("session_busy", a)
    sid_a = _mk_session(app, a, "want-a")
    sid_b = _mk_session(app, b, "want-b")

    # 同项目 + **跨会话**：排队（C 项，不再 429 让用户反复重试）
    with pytest.raises(TaskQueued) as ei:
        _start_until_registry(tm, app, sid_a)
    assert ei.value.queued.position == 1
    assert tm.queued_for(sid_a)
    # 跨项目：过（走到 registry 构建为止）
    captured = {}
    orig = app.create_agent_registry

    def fake_registry(agent_id, workspace=None, unattended_profile=None, session_id=None):
        captured["ok"] = True
        raise _Abort()

    app.create_agent_registry = fake_registry  # type: ignore[method-assign]
    try:
        with pytest.raises(_Abort):
            tm.start(sid_b, "hi")
        assert captured.get("ok")
    finally:
        app.create_agent_registry = orig  # type: ignore[method-assign]


def test_same_session_duplicate_start_still_rejected(core, two_projects):
    """同会话重复启动仍是直接拒绝（误操作，排队只会掩盖它）。"""
    app, client = core
    a, b = two_projects
    tm = TaskManager(app)

    h = _fake_running_task("busy", a)
    sid_same = "session_same"
    h.kernel = type("K", (), {"session_id": sid_same})()
    tm.tasks["t1"] = h

    # 会话必须真实存在且钉在同一项目上（否则 base_ws 为空 → 不判冲突）
    app.session_store.save(sid_same, [], {"workspace": a})
    # 用同一个 session_id 再起 → 拒（不是排队）
    with pytest.raises(WorkspaceBusyError):
        _start_until_registry(tm, app, sid_same)
    assert not tm.queued_for(sid_same)


def test_queue_if_busy_false_still_raises(core, two_projects):
    """无人值守路径传 queue_if_busy=False → 保持旧的拒绝语义（撞车即跳过）。"""
    app, client = core
    a, b = two_projects
    tm = TaskManager(app)

    h = _fake_running_task("busy", a)
    h.kernel = type("K", (), {"session_id": "watch"})()
    tm.tasks["t1"] = h

    sid = _mk_session(app, a, "want-a")
    with pytest.raises(WorkspaceBusyError):
        tm.start(sid, "hi", queue_if_busy=False)
    assert not tm.queued_for(sid)


def test_queue_order_is_fifo(core, two_projects):
    """同项目多个会话排队：先到先跑，position 递增。"""
    app, client = core
    a, b = two_projects
    tm = TaskManager(app)

    h = _fake_running_task("busy", a)
    h.kernel = type("K", (), {"session_id": "busy"})()
    tm.tasks["t1"] = h

    sids = [_mk_session(app, a, f"want-{i}") for i in range(3)]
    positions = []
    for sid in sids:
        with pytest.raises(TaskQueued) as ei:
            _start_until_registry(tm, app, sid)
        positions.append(ei.value.queued.position)
    assert positions == [1, 2, 3]
    # 队列里顺序与会话入队顺序一致
    key = tm._ws_key(a)
    assert [q.session_id for q in tm.pending[key]] == sids


def test_cancel_queued_removes_only_target_session(core, two_projects):
    app, client = core
    a, b = two_projects
    tm = TaskManager(app)

    h = _fake_running_task("busy", a)
    h.kernel = type("K", (), {"session_id": "busy"})()
    tm.tasks["t1"] = h

    s1 = _mk_session(app, a, "want-1")
    s2 = _mk_session(app, a, "want-2")
    for sid in (s1, s2):
        with pytest.raises(TaskQueued):
            _start_until_registry(tm, app, sid)
    assert tm.cancel_queued(s1) == 1
    assert not tm.queued_for(s1)
    assert tm.queued_for(s2)  # 另一个不受影响
    # position 重新编号
    assert tm.queued_for(s2)[0].position == 1


def test_drain_starts_next_when_project_free(core, two_projects):
    """项目空出来后 drain_queues 启动队首任务。"""
    app, client = core
    a, b = two_projects
    tm = TaskManager(app)

    h = _fake_running_task("busy", a)
    h.kernel = type("K", (), {"session_id": "busy"})()
    tm.tasks["t1"] = h

    sid = _mk_session(app, a, "want-a")
    with pytest.raises(TaskQueued):
        _start_until_registry(tm, app, sid)

    # 项目仍被占用：不启动
    assert tm.drain_queues() == 0
    assert tm.queued_for(sid)

    # 占用者结束 → 队首被启动
    h.running = False
    started = []
    orig = app.create_agent_registry

    def fake_registry(agent_id, workspace=None, unattended_profile=None, session_id=None):
        started.append(agent_id)
        raise _Abort()

    app.create_agent_registry = fake_registry  # type: ignore[method-assign]
    try:
        try:
            tm.drain_queues()
        except _Abort:
            pass
        assert started  # 队列被消费（跑到了 registry 构建）
    finally:
        app.create_agent_registry = orig  # type: ignore[method-assign]
        # drain 内部吞掉异常，队列已被 pop（启动失败也不重排，避免死循环）


def _start_until_registry(tm: TaskManager, app: AgentApp, sid: str):
    """start 到 registry 构建即止（不真正起任务）。"""
    orig = app.create_agent_registry

    def fake_registry(agent_id, workspace=None, unattended_profile=None, session_id=None):
        raise _Abort()

    app.create_agent_registry = fake_registry  # type: ignore[method-assign]
    try:
        tm.start(sid, "hi")
    finally:
        app.create_agent_registry = orig  # type: ignore[method-assign]


def test_worktree_task_not_mutually_exclusive(core, two_projects):
    """worktree 隔离任务在独立副本执行，不参与同项目互斥。"""
    app, client = core
    a, _b = two_projects
    tm = TaskManager(app)

    from litework.server.tasks import TaskHandle

    def _fake_running_task(sid: str) -> TaskHandle:
        h = TaskHandle.__new__(TaskHandle)
        h.running = True
        h.stopping = False
        h.workspace = None  # worktree 任务的 workspace 是副本路径，这里 None 简化
        h.session_workspace = a
        h.worktree_name = sid  # 非空 = worktree 任务
        h.kernel = type("K", (), {"session_id": sid})()
        return h

    tm.tasks["wt1"] = _fake_running_task("session_wt")
    # 新任务也开 worktree：两侧都在隔离副本，互不冲突
    sid = f"session_wt2"
    app.session_store.save(sid, [], {"workspace": a, "worktree": True})

    # worktree 创建会真实跑 git——换成捕获 manager.create 之后的路径
    orig_create = app.worktree_manager
    class _FakeMgr:
        def create(self, session_id):
            return {"ok": True, "path": "/fake/wt", "branch": "b", "restored": False}
    app.worktree_manager = lambda: _FakeMgr()  # type: ignore[method-assign]
    captured = {}
    orig_reg = app.create_agent_registry

    def fake_registry(agent_id, workspace=None, unattended_profile=None, session_id=None):
        captured["workspace"] = workspace
        raise _Abort()

    app.create_agent_registry = fake_registry  # type: ignore[method-assign]
    try:
        with pytest.raises(_Abort):
            tm.start(sid, "hi")
        # 没有因互斥被拒，且工作区是 worktree 副本
        assert captured["workspace"] == "/fake/wt"
    finally:
        app.create_agent_registry = orig_reg  # type: ignore[method-assign]
        app.worktree_manager = orig_create  # type: ignore[method-assign]


def test_chat_returns_429_on_busy_workspace(core, two_projects):
    """/api/chat 对 WorkspaceBusyError 返回 429（前端可提示重试）。"""
    app, client = core
    a, _b = two_projects
    sid = _mk_session(app, a, "busy")
    # 占坑：让 TaskManager 已有一个"运行中"的同项目任务
    ctx_router_state = None
    # 通过 monkeypatch TaskManager.start 抛 WorkspaceBusyError 验证 HTTP 层
    from litework.server.tasks import TaskManager as TM

    orig_start = TM.start

    def busy_start(self, *a_, **kw):
        raise WorkspaceBusyError("该项目已有任务运行中")

    TM.start = busy_start  # type: ignore[method-assign]
    try:
        r = client.post("/api/chat", json={"session_id": sid, "prompt": "hi"}, headers=AUTH)
        assert r.status_code == 429
        assert "已有任务运行" in r.json()["detail"]
    finally:
        TM.start = orig_start  # type: ignore[method-assign]


def test_chat_returns_queue_info_on_cross_session_conflict(core, two_projects):
    """/api/chat 撞同项目（跨会话）→ 200 + queued + position（不再 429）。"""
    app, client = core
    a, _b = two_projects
    sid = _mk_session(app, a, "queued")
    from litework.server.tasks import QueuedTask, TaskManager as TM

    orig_start = TM.start

    def queue_start(self, session_id, prompt, **kw):
        raise TaskQueued(QueuedTask(task_id="qid", session_id=session_id,
                                    prompt=prompt, position=2))

    TM.start = queue_start  # type: ignore[method-assign]
    try:
        r = client.post("/api/chat", json={"session_id": sid, "prompt": "hi"}, headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["queued"] is True
        assert body["position"] == 2
        assert body["task_id"] == "qid"
        assert "排队" in body["message"]
    finally:
        TM.start = orig_start  # type: ignore[method-assign]


def test_all_queued_reports_position_and_waiting(core, two_projects):
    """all_queued()：面板数据源——带序号、前面还有几个、已等待秒数。"""
    app, client = core
    a, _b = two_projects
    tm = TaskManager(app)

    h = _fake_running_task("busy", a)
    tm.tasks["t1"] = h

    s1 = _mk_session(app, a, "q1")
    s2 = _mk_session(app, a, "q2")
    for sid in (s1, s2):
        with pytest.raises(TaskQueued):
            _start_until_registry(tm, app, sid)

    rows = tm.all_queued()
    assert [r["session_id"] for r in rows] == [s1, s2]
    assert [r["position"] for r in rows] == [1, 2]
    # 队首前面只有「正在跑的那个」，第二个前面还有跑着的 + 队首
    assert [r["waiting_behind"] for r in rows] == [1, 2]
    assert all(r["waiting"] >= 0 for r in rows)
    assert all(r["project"] for r in rows)


def test_api_queued_list_and_cancel(core, two_projects):
    """GET /api/tasks/queued 与 DELETE /api/tasks/queued/{id}。"""
    app, client = core
    a, _b = two_projects
    tm = app._task_manager

    h = _fake_running_task("busy", a)
    tm.tasks["t1"] = h

    sid = _mk_session(app, a, "api-q")
    with pytest.raises(TaskQueued) as ei:
        _start_until_registry(tm, app, sid)
    qid = ei.value.queued.task_id

    r = client.get("/api/tasks/queued", headers=AUTH)
    assert r.status_code == 200
    rows = r.json()["tasks"]
    assert any(x["task_id"] == qid for x in rows)

    r = client.delete(f"/api/tasks/queued/{qid}", headers=AUTH)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert not any(x["task_id"] == qid for x in client.get(
        "/api/tasks/queued", headers=AUTH).json()["tasks"])

    # 再删一次：已不在队列，返回 False（不是 500）
    r = client.delete(f"/api/tasks/queued/{qid}", headers=AUTH)
    assert r.status_code == 200 and r.json()["ok"] is False
