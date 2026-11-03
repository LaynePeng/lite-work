# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""会话自唤醒（schedule_wakeup）与一次性计划（mode=once）。

覆盖三类真实风险：

1. **唤醒必须回到原会话**：原会话被删时宁可失败，也不能新建一个空会话去跑
   prompt——那样上下文全丢，"复活"名存实亡。
2. **once 只能跑一次**：跑完自动停用；错过的必须补跑（否则应用关着就永久失效）。
3. **多 Core 不互相覆盖计划**：两个 Core 各持一份内存副本，整份写盘会抹掉
   对方新建的计划（实测过）。
"""
import asyncio
import json
import os
import tempfile
import time

import pytest

from litework.tools.wakeup import (
    MAX_PER_SESSION,
    MIN_DELAY_SECONDS,
    ORIGIN,
    make_wakeup_handler,
)
from litework.watch.schedule_store import RunRecord, Schedule, ScheduleStore
from litework.watch.scheduler import WatchScheduler, compute_next_run
from litework.watch.registry import WatchSourceRegistry


# ---------------------------------------------------------------- 存储：多 Core 安全


def test_two_cores_do_not_overwrite_each_other(tmp_path):
    """两个 Core 各建一条计划：落盘后两条都要在。

    回归：此前 save() 整份覆盖，后写的 Core 会静默抹掉对方新建的计划——
    实测落盘只剩最后一条，用户以为"盯梢计划自己消失了"。
    """
    path = str(tmp_path / "schedules.json")
    a, b = ScheduleStore(path), ScheduleStore(path)
    a.load()
    b.load()
    a.upsert(Schedule(id="sch_a", name="A 建的"))
    b.upsert(Schedule(id="sch_b", name="B 建的"))

    on_disk = {s["id"] for s in json.load(open(path, encoding="utf-8"))["schedules"]}
    assert on_disk == {"sch_a", "sch_b"}


def test_delete_not_resurrected_by_merge(tmp_path):
    """删除不能被"合并磁盘"复活（墓碑）。

    save() 会先 _merge_from_disk()，不记墓碑就会把刚删的那条又并回来。
    """
    path = str(tmp_path / "schedules.json")
    a = ScheduleStore(path)
    a.load()
    a.upsert(Schedule(id="sch_x", name="要删的"))
    assert a.delete("sch_x") is True

    on_disk = [s["id"] for s in json.load(open(path, encoding="utf-8"))["schedules"]]
    assert on_disk == []


def test_record_run_survives_other_core_write(tmp_path):
    """A 记的运行历史不能被 B 的写盘抹掉（历史/指纹基线是排错依据）。"""
    path = str(tmp_path / "schedules.json")
    a, b = ScheduleStore(path), ScheduleStore(path)
    a.load()
    a.upsert(Schedule(id="sch_h", name="历史"))
    b.load()
    b.upsert(Schedule(id="sch_h2", name="B 的"))
    a.record_run("sch_h", RunRecord(status="changed", detail="变了"))

    rows = {s["id"]: s for s in json.load(open(path, encoding="utf-8"))["schedules"]}
    assert rows["sch_h"]["last_status"] == "changed"
    assert len(rows["sch_h"]["runs"]) == 1


def test_once_fields_roundtrip():
    s = Schedule(id="sch_o", schedule_mode="once", run_at=1_700_000_000.0,
                 origin=ORIGIN)
    back = Schedule.from_dict(s.to_dict())
    assert back.schedule_mode == "once"
    assert back.run_at == 1_700_000_000.0
    assert back.origin == ORIGIN


# ---------------------------------------------------------------- 调度：once


def test_once_due_time_is_run_at():
    s = Schedule(schedule_mode="once", run_at=1_700_000_000.0)
    assert compute_next_run(s, now=1_600_000_000.0) == 1_700_000_000.0


def test_once_without_run_at_never_fires():
    """没给 run_at 的 once 永不调度（路由层建计划时就拒，这里兜底）。"""
    assert compute_next_run(Schedule(schedule_mode="once")) is None


def _scheduler(tmp_path, *, sessions=None):
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.load()
    fired = []

    def start_task(sched, prompt):
        fired.append({"id": sched.id, "prompt": prompt})
        return "t1"

    sch = WatchScheduler(store, WatchSourceRegistry(), start_task=start_task)
    return sch, store, fired


def test_once_fires_then_removed(tmp_path):
    """一次性计划跑完就**删除**，不在列表里留垃圾。"""
    sch, store, fired = _scheduler(tmp_path)
    store.upsert(Schedule(id="sch_1", enabled=True, schedule_mode="once",
                          run_at=time.time() - 1, prompt="接着干"))
    rec = asyncio.run(sch.fire(store.get("sch_1")))
    assert rec.status == "ok"
    assert len(fired) == 1
    # 计划本体已从 store 与磁盘一起消失
    assert store.get("sch_1") is None
    on_disk = json.load(open(tmp_path / "schedules.json", encoding="utf-8"))
    assert [s["id"] for s in on_disk["schedules"]] == []
    # 但本次运行结果仍随通知送达（删除不影响"告诉我跑完了"）
    assert rec.detail


def test_once_removed_plan_not_resurrected_by_other_core(tmp_path):
    """A 跑完删掉的一次性计划，不能被 B 的内存副本写回去。"""
    path = str(tmp_path / "schedules.json")
    a = ScheduleStore(path)
    a.load()
    b = ScheduleStore(path)
    a.upsert(Schedule(id="sch_r1", schedule_mode="once",
                      run_at=time.time() - 1, prompt="x"))
    b.load()                       # B 也读到了这条
    assert b.get("sch_r1") is not None

    sch = WatchScheduler(a, WatchSourceRegistry(), start_task=lambda s, p: "t1")
    asyncio.run(sch.fire(a.get("sch_r1")))
    assert a.get("sch_r1") is None

    # B 之后任何写盘都不得让它复活
    b.upsert(Schedule(id="sch_r2", name="B 新计划"))
    on_disk = [s["id"] for s in json.load(open(path, encoding="utf-8"))["schedules"]]
    assert "sch_r1" not in on_disk
    assert "sch_r2" in on_disk


def test_once_advance_returns_none(tmp_path):
    """once 跑过就没有"下一次"——不能返回一个远期时间戳假装还在排期。"""
    sch, store, _ = _scheduler(tmp_path)
    s = Schedule(id="sch_2", schedule_mode="once", run_at=time.time())
    assert sch._advance(s, time.time()) is None


def test_once_catch_up_runs_missed_wakeup(tmp_path):
    """应用在到点时关着 → 启动后补跑一次（否则唤醒永久失效）。"""
    sch, store, fired = _scheduler(tmp_path)
    store.upsert(Schedule(id="sch_cu", enabled=True, schedule_mode="once",
                          run_at=time.time() - 600, last_run=None,
                          prompt="补跑我"))
    asyncio.run(sch.catch_up_all())
    assert len(fired) == 1


def test_once_catch_up_skips_when_policy_skip(tmp_path):
    """catch_up=skip 时不补，并且把这条已过期的一次性计划删掉。"""
    sch, store, fired = _scheduler(tmp_path)
    store.upsert(Schedule(id="sch_cs", enabled=True, schedule_mode="once",
                          run_at=time.time() - 600, catch_up="skip", prompt="x"))
    recs = asyncio.run(sch.catch_up_all())
    assert fired == []
    assert any(r.status == "skipped" for r in recs)
    # 过期且明确不补：留着只是死计划，删掉
    assert store.get("sch_cs") is None


def test_once_catch_up_does_not_rerun_already_fired(tmp_path):
    """已经跑过这个时间点的计划不得在重启时再跑一次。"""
    sch, store, fired = _scheduler(tmp_path)
    at = time.time() - 600
    store.upsert(Schedule(id="sch_done", enabled=True, schedule_mode="once",
                          run_at=at, last_run=at + 5, prompt="x"))
    asyncio.run(sch.catch_up_all())
    assert fired == []


def test_wakeup_prompt_states_it_is_a_self_wakeup(tmp_path):
    """唤醒 prompt 必须说明「你被自己叫醒、别重做已完成的部分」。

    否则模型会把一段孤立的 prompt 当成新任务，从头再来一遍。
    """
    sch, store, fired = _scheduler(tmp_path)
    store.upsert(Schedule(id="sch_wk", enabled=True, schedule_mode="once",
                          run_at=time.time() - 1, origin=ORIGIN,
                          prompt="继续等 CI 结果"))
    asyncio.run(sch.fire(store.get("sch_wk")))
    assert "定时唤醒" in fired[0]["prompt"]
    assert "不要重做" in fired[0]["prompt"]
    assert "继续等 CI 结果" in fired[0]["prompt"]


# ---------------------------------------------------------------- 启动失败 vs 跳过


def test_workspace_busy_recorded_as_skipped_not_error(tmp_path):
    """撞同项目互斥是**正常让位**，不能记成 error（用户会以为盯梢坏了）。"""
    store = ScheduleStore(str(tmp_path / "s.json"))
    store.load()
    store.upsert(Schedule(id="sch_busy", enabled=True, prompt="x"))
    sch = WatchScheduler(store, WatchSourceRegistry())

    def busy(_sched, _prompt):
        sch.note_start_failure("sch_busy", kind="skip",
                               detail="跳过本轮：该项目已有任务在跑")
        return None

    sch.start_task = busy
    rec = asyncio.run(sch.fire(store.get("sch_busy")))
    assert rec.status == "skipped"
    assert "该项目已有任务在跑" in rec.detail


def test_start_failure_carries_real_reason(tmp_path):
    """真故障要把原因写进运行记录，而不是笼统的「任务启动失败」。"""
    store = ScheduleStore(str(tmp_path / "s.json"))
    store.load()
    store.upsert(Schedule(id="sch_err", enabled=True, prompt="x"))
    sch = WatchScheduler(store, WatchSourceRegistry())

    def boom(_sched, _prompt):
        sch.note_start_failure("sch_err", kind="error", detail="准备会话失败：会话已被删除")
        return None

    sch.start_task = boom
    rec = asyncio.run(sch.fire(store.get("sch_err")))
    assert rec.status == "error"
    assert "会话已被删除" in rec.detail


def test_stale_reason_not_attached_to_next_run(tmp_path):
    """上一轮的失败原因不得挂到下一轮成功触发上（清残留）。"""
    store = ScheduleStore(str(tmp_path / "s.json"))
    store.load()
    store.upsert(Schedule(id="sch_s", enabled=True, prompt="x"))
    sch = WatchScheduler(store, WatchSourceRegistry())
    sch.note_start_failure("sch_s", kind="error", detail="上一轮的旧原因")
    sch.start_task = lambda s, p: "t_ok"
    rec = asyncio.run(sch.fire(store.get("sch_s")))
    assert rec.status == "ok"


# ---------------------------------------------------------------- 工具层


class _FakeApp:
    def __init__(self, workspace: str) -> None:
        self.workspace = workspace
        self.schedule_store = ScheduleStore(os.path.join(workspace, "schedules.json"))
        self.schedule_store.load()
        self.watch_scheduler = None


@pytest.fixture
def app_and_handler(tmp_path):
    app = _FakeApp(str(tmp_path))
    return app, make_wakeup_handler(app, "session_abc")


def test_wakeup_set_creates_once_plan_bound_to_session(app_and_handler):
    app, handler = app_and_handler
    out = asyncio.run(handler({"action": "set", "delay_seconds": 600,
                               "prompt": "等 CI 跑完检查结果"}))
    assert "[OK]" in out
    plans = [s for s in app.schedule_store.all() if s.origin == ORIGIN]
    assert len(plans) == 1
    p = plans[0]
    assert p.schedule_mode == "once"
    assert p.session_id == "session_abc"
    # 主会话也指向自己：唤醒结果回到原对话
    assert p.main_session_id == "session_abc"
    assert p.enabled is True
    assert 500 < (p.run_at or 0) - time.time() < 700
    # 工作区必须钉死，否则多窗口下会落到别的项目
    assert p.workspace == app.workspace


def test_wakeup_rejects_too_short_delay(app_and_handler):
    _, handler = app_and_handler
    out = asyncio.run(handler({"action": "set", "delay_seconds": MIN_DELAY_SECONDS - 5,
                               "prompt": "x"}))
    assert "[Error]" in out
    assert "至少" in out


def test_wakeup_rejects_far_future(app_and_handler):
    _, handler = app_and_handler
    out = asyncio.run(handler({"action": "set", "delay_seconds": 30 * 86400,
                               "prompt": "x"}))
    assert "[Error]" in out


def test_wakeup_requires_prompt(app_and_handler):
    _, handler = app_and_handler
    out = asyncio.run(handler({"action": "set", "delay_seconds": 600}))
    assert "[Error]" in out and "prompt" in out


def test_wakeup_accepts_absolute_time(app_and_handler):
    app, handler = app_and_handler
    target = time.strftime("%Y-%m-%d %H:%M",
                           time.localtime(time.time() + 3600))
    out = asyncio.run(handler({"action": "set", "at": target, "prompt": "x"}))
    assert "[OK]" in out
    plans = [s for s in app.schedule_store.all() if s.origin == ORIGIN]
    assert len(plans) == 1


def test_wakeup_rejects_unparseable_time(app_and_handler):
    _, handler = app_and_handler
    out = asyncio.run(handler({"action": "set", "at": "明天早上", "prompt": "x"}))
    assert "[Error]" in out
    assert "无法解析" in out


def test_wakeup_list_and_cancel(app_and_handler):
    app, handler = app_and_handler
    asyncio.run(handler({"action": "set", "delay_seconds": 600, "prompt": "A"}))
    asyncio.run(handler({"action": "set", "delay_seconds": 900, "prompt": "B",
                         "name": "第二次"}))
    listed = asyncio.run(handler({"action": "list"}))
    assert "A" in listed and "第二次" in listed

    target = [s for s in app.schedule_store.all() if s.name == "第二次"][0]
    out = asyncio.run(handler({"action": "cancel", "schedule_id": target.id}))
    assert "已取消" in out
    # 取消 = 删除（不留一条停用的僵尸计划在列表里）
    assert app.schedule_store.get(target.id) is None
    # 取消后 list 里不再出现，且另一个唤醒未受影响
    assert "第二次" not in asyncio.run(handler({"action": "list"}))
    assert len([s for s in app.schedule_store.all() if s.origin == ORIGIN]) == 1


def test_wakeup_cancel_requires_id(app_and_handler):
    _, handler = app_and_handler
    out = asyncio.run(handler({"action": "cancel"}))
    assert "[Error]" in out
    assert "schedule_id" in out


def test_wakeup_cannot_cancel_other_sessions_plan(tmp_path):
    """只能取消自己设的唤醒——不得误删别人（或用户建的盯梢计划）。"""
    app = _FakeApp(str(tmp_path))
    app.schedule_store.upsert(Schedule(id="sch_user", name="用户建的盯梢"))
    handler = make_wakeup_handler(app, "session_abc")
    out = asyncio.run(handler({"action": "cancel", "schedule_id": "sch_user"}))
    assert "[Error]" in out
    assert app.schedule_store.get("sch_user").enabled is True


def test_wakeup_per_session_limit(app_and_handler):
    app, handler = app_and_handler
    for i in range(MAX_PER_SESSION):
        out = asyncio.run(handler({"action": "set", "delay_seconds": 600 + i,
                                   "prompt": f"任务 {i}"}))
        assert "[OK]" in out
    out = asyncio.run(handler({"action": "set", "delay_seconds": 999,
                               "prompt": "超额"}))
    assert "[Error]" in out
    assert "上限" in out


def test_wakeup_rejects_unknown_action(app_and_handler):
    _, handler = app_and_handler
    out = asyncio.run(handler({"action": "explode"}))
    assert "[Error]" in out


# ---------------------------------------------------------------- REST 层


def _client(tmp_path, *, workspace=True):
    from fastapi.testclient import TestClient

    from litework.app import AgentApp
    from litework.server.app import create_app

    app = AgentApp(config_dir=str(tmp_path / "cfg"))
    if workspace:
        app.workspace = str(tmp_path / "ws")
        os.makedirs(app.workspace, exist_ok=True)
    app.schedule_store.load()
    return app, TestClient(create_app(app, token=None))


def test_route_creates_once_plan(tmp_path):
    _, client = _client(tmp_path)
    with client:
        r = client.post("/api/schedules", json={
            "name": "一次", "schedule_mode": "once",
            "run_at": time.time() + 600, "prompt": "x",
        })
        assert r.status_code == 200, r.text
        body = r.json()["schedule"]
        assert body["schedule_mode"] == "once"
        assert body["run_at"] is not None
        assert body["next_run"] is not None


def test_route_rejects_once_without_run_at(tmp_path):
    """没给 run_at 的 once 会永远不触发——建的时候就拒，别建一条死的。"""
    _, client = _client(tmp_path)
    with client:
        r = client.post("/api/schedules", json={
            "name": "一次", "schedule_mode": "once", "prompt": "x",
        })
        assert r.status_code == 400
        assert "run_at" in r.json()["detail"]


def test_patch_once_time_reenables(tmp_path):
    """跑过一次后（enabled=False）改触发时间 → 自动重新启用。

    否则用户在界面上改时间，看到的是一条「启用中」都不算的计划，永远不响。
    """
    app, client = _client(tmp_path)
    with client:
        sched = Schedule(id="sch_p1", schedule_mode="once", enabled=False,
                         run_at=time.time() - 100, prompt="x")
        app.schedule_store.upsert(sched)
        r = client.patch("/api/schedules/sch_p1",
                         json={"run_at": time.time() + 900})
        assert r.status_code == 200, r.text
        assert r.json()["schedule"]["enabled"] is True


def test_schedules_visible_without_open_project(tmp_path):
    """没打开项目的 Core 也能读计划（多 Core 下最常见的"盯梢报错"来源）。

    回归：此前 /api/schedules 走 require_workspace → 409「请先打开项目」，
    右栏「盯梢」页直接报错，用户以为盯梢功能坏了。
    """
    app, client = _client(tmp_path, workspace=False)
    with client:
        app.schedule_store.upsert(Schedule(id="sch_l", name="全局计划", prompt="x"))
        r = client.get("/api/schedules")
        assert r.status_code == 200, r.text
        assert [s["id"] for s in r.json()["schedules"]] == ["sch_l"]
        # 单条读取、通知、数据源列表同样不该被工作区挡住
        assert client.get("/api/schedules/sch_l").status_code == 200
        assert client.get("/api/watch/sources").status_code == 200
        assert client.get("/api/watch/notifications").status_code == 200


def test_creating_schedule_without_project_still_refused(tmp_path):
    """新建计划仍需要工作区：计划要记"在哪个项目里工作"，不能凭空猜。"""
    _, client = _client(tmp_path, workspace=False)
    with client:
        r = client.post("/api/schedules", json={"name": "x", "prompt": "x"})
        assert r.status_code == 409


def test_advance_keeps_interval_and_daily_behaviour(tmp_path):
    """_advance 对 interval / daily / cron 仍要正确推进。

    回归：给 once 加分支时曾把 interval/daily/cron 三个分支整段删掉，
    当时**所有测试都还是绿的**——没有用例直接锁 _advance。补上。
    """
    sch, _store, _fired = _scheduler(tmp_path)
    now = time.time()

    every = sch._advance(Schedule(schedule_mode="interval", interval_seconds=300),
                         now)
    assert every is not None and every > now
    assert abs(every - (now + 300)) < 2

    daily = sch._advance(Schedule(schedule_mode="daily", time_of_day="09:00"), now)
    assert daily is not None and daily > now

    cron = sch._advance(Schedule(schedule_mode="cron", cron="0 9 * * *"), now)
    assert cron is not None and cron > now

    # 未知模式退回 1 小时后（不能让 tick 死循环重试同一个窗口）
    unknown = sch._advance(Schedule(schedule_mode="weekly"), now)
    assert unknown is not None and abs(unknown - (now + 3600)) < 2


def test_tick_drops_once_from_schedule_table(tmp_path):
    """once 触发后必须从到期表摘掉，否则每 tick 都会重复命中它。"""
    sch, store, fired = _scheduler(tmp_path)
    store.upsert(Schedule(id="sch_t1", enabled=True, schedule_mode="once",
                          run_at=time.time() - 1, prompt="x"))
    sch._next["sch_t1"] = time.time() - 1
    asyncio.run(sch.tick())
    assert len(fired) == 1
    assert "sch_t1" not in sch._next


def test_delete_reports_success_when_already_gone_on_disk(tmp_path):
    """两个 Core 各删一次：第二个按磁盘为准也算成功（不能报失败却不做事）。"""
    path = str(tmp_path / "schedules.json")
    a = ScheduleStore(path)
    a.load()
    a.upsert(Schedule(id="sch_dd", name="删两次"))
    b = ScheduleStore(path)
    b.load()
    assert a.delete("sch_dd") is True      # A 先删
    # B 内存里还有 → 它再删一次，磁盘上已经没了，仍应返回 True
    assert b.delete("sch_dd") is True
    assert [s["id"] for s in json.load(open(path, encoding="utf-8"))["schedules"]] == []


def test_interval_plan_never_deleted(tmp_path):
    """常驻计划（interval）跑完不得被删——只有 once 才删。"""
    sch, store, fired = _scheduler(tmp_path)
    store.upsert(Schedule(id="sch_iv", enabled=True, schedule_mode="interval",
                          interval_seconds=300, prompt="常驻"))
    asyncio.run(sch.fire(store.get("sch_iv")))
    assert len(fired) == 1
    survivor = store.get("sch_iv")
    assert survivor is not None and survivor.enabled is True


# ---------------------------------------------------------------- 悬空会话（真实报障）

def _bootstrapped_client(tmp_path):
    """真实 create_app 启动一次，拿到 lifespan 里装的 _start_watch_task。"""
    from fastapi.testclient import TestClient

    from litework.app import AgentApp
    from litework.server.app import create_app

    app = AgentApp(config_dir=str(tmp_path / "cfg"))
    app.workspace = str(tmp_path / "ws")
    os.makedirs(app.workspace, exist_ok=True)
    app.schedule_store.load()
    return app, TestClient(create_app(app, token=None))


def test_failed_start_does_not_leave_dangling_session(tmp_path, monkeypatch):
    """启动失败时，计划上不得留下指向空会话的 session_id。

    真实报障：盯盘通知弹出来，点「打开会话」却是一个**空对话**。
    根因链：

    1. 计划没绑会话 → 触发时补建一个会话并立刻回写计划；
    2. 紧接着任务启动失败（撞同项目互斥 / 模型未配置）；
    3. 计划上留下了那个 session_id，但它是个空会话，之后还会被清理掉；
    4. 用户点通知「打开会话」→ 拿到死链 → getSession 404 → 空白对话。

    修法：**只有任务真的起来了**才回写计划；失败则丢弃补建的会话。
    """
    app, client = _bootstrapped_client(tmp_path)
    with client:
        sched = Schedule(id="sch_dangling", enabled=True,
                         schedule_mode="interval", interval_seconds=300,
                         workspace=app.workspace, prompt="x",
                         session_id=None, main_session_id=None)
        app.schedule_store.upsert(sched)

        # 让启动必然失败（模拟撞同项目互斥这类"任务起不来"）。
        # 必须打在 TaskManager.start 这个类方法上：lifespan 内部 new 了自己的
        # TaskManager 实例，往 app 上塞一个替身根本不会被用到。
        from litework.server import tasks as tasks_mod

        def _boom(*_a, **_kw):
            raise tasks_mod.WorkspaceBusyError("该项目已有任务运行中")

        monkeypatch.setattr(tasks_mod.TaskManager, "start", _boom)
        rec = asyncio.run(app.watch_scheduler.fire(app.schedule_store.get("sch_dangling")))

        assert rec.status == "skipped", rec.detail
        after = app.schedule_store.get("sch_dangling")
        # 关键断言：计划上不能留悬空会话 id
        assert not after.session_id, f"计划残留了指向空会话的 id: {after.session_id}"
        # 补建的空会话也不该留在磁盘上
        orphans = [s for s in app.session_store.list()
                   if (s.get("metadata") or {}).get("watch_schedule_id") == "sch_dangling"]
        assert orphans == [], f"残留了空会话: {orphans}"


def test_successful_start_writes_session_back(tmp_path, monkeypatch):
    """任务真起来了才回写会话——否则点通知仍是空对话的另一半。"""
    app, client = _bootstrapped_client(tmp_path)
    with client:
        app.schedule_store.upsert(Schedule(
            id="sch_back", enabled=True, schedule_mode="interval",
            interval_seconds=300, workspace=app.workspace, prompt="x",
            session_id=None))

        started = {}
        from litework.server import tasks as tasks_mod

        class _Handle:
            task_id = "task_ok"

        def _ok(_self, session_id, *_a, **_kw):
            started["sid"] = session_id
            return _Handle()

        monkeypatch.setattr(tasks_mod.TaskManager, "start", _ok)
        rec = asyncio.run(app.watch_scheduler.fire(app.schedule_store.get("sch_back")))
        assert rec.status == "ok", rec.detail
        after = app.schedule_store.get("sch_back")
        assert after.session_id == started["sid"]
        assert app.session_store.load(after.session_id) is not None
