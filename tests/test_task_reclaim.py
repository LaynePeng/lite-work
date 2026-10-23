# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""已完成任务的内存回收回归（TaskHandle.release / TaskManager.reap）。

事故背景：TaskHandle 原先只在 SSE 流正常收尾时被 `TaskManager.cleanup` 摘掉。
「界面已关 / 客户端断网 / 盯梢定时任务」这类没有活跃订阅者的路径永远走不到
那里，handle 连同整个会话上下文（kernel.ctx.messages 就是整份历史）永久驻留
`TaskManager.tasks`——实测每次任务残留 ~55MB，跑十几次任务后端内存吃满。

这些用例锁住两条不变量：
1. 没有订阅者时，reap 立刻摘掉重对象（不依赖 SSE 收尾）；
2. 迟到订阅者拿到的仍是「任务已结束」语义（[DONE]），而不是 404。
"""
from __future__ import annotations

from litework.app import AgentApp
from litework.server.tasks import TaskManager
from tests.conftest import MockLLMAdapter


def _make_app(tmp_path) -> AgentApp:
    return AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))


async def _run_task(tm: TaskManager, session_id: str = "reclaim-session"):
    handle = tm.start(session_id, "hi")
    await handle.task
    return handle


async def test_reap_releases_finished_handle_without_subscribers(tmp_path):
    """无 SSE 订阅者的任务结束后，reap 释放 kernel/registry/loop 三大对象图。"""
    app = _make_app(tmp_path)
    app._mock_adapter = MockLLMAdapter([("完成", [])])
    tm = TaskManager(app)

    handle = await _run_task(tm)
    assert handle.done and not handle.released
    # 任务跑完时确实持有整个会话上下文（泄漏的就是这份）
    assert handle.kernel is not None

    released = tm.reap()

    assert released >= 1
    assert handle.released is True
    assert handle.kernel is None, "kernel 未释放：会话历史仍被句柄钉住"
    assert handle.registry is None
    assert handle.loop is None


async def test_reaped_handle_still_answers_late_subscriber(tmp_path):
    """回收后 handle 仍可订阅：迟到客户端立刻收到结束哨兵，而不是 404。"""
    app = _make_app(tmp_path)
    app._mock_adapter = MockLLMAdapter([("完成", [])])
    tm = TaskManager(app)

    handle = await _run_task(tm)
    tm.reap()
    assert handle.released is True

    assert tm.get(handle.task_id) is not None, "回收应保留 shell 供迟到订阅"
    queue = handle.subscribe()
    assert queue.get_nowait() is None, "迟到的订阅者应立刻拿到 [DONE]"


async def test_reap_removes_expired_handle_from_table(tmp_path, monkeypatch):
    """超过 TTL 的已完成 handle 从 tasks 表摘除（含队列永不排空的僵尸场景）。"""
    app = _make_app(tmp_path)
    app._mock_adapter = MockLLMAdapter([("完成", [])])
    tm = TaskManager(app)

    handle = await _run_task(tm)
    # 伪造「很久以前结束」
    handle.finished_at -= TaskManager.FINISHED_TTL_SECONDS + 1
    tm.reap()

    assert tm.get(handle.task_id) is None


async def test_reap_keeps_running_task_untouched(tmp_path):
    """运行中的任务绝不被回收（否则 SSE 流与中断能力全部断掉）。"""
    app = _make_app(tmp_path)
    app._mock_adapter = MockLLMAdapter([("完成", [])])
    tm = TaskManager(app)

    handle = tm.start("running-session", "hi")
    await handle.task  # 先让它结束，再手工伪装成运行中
    handle.done = False
    handle.running = True

    tm.reap()

    assert handle.released is False
    assert handle.kernel is not None


async def test_reap_caps_finished_handles(tmp_path):
    """已完成 handle 数超上限时摘除旧的，防止长时间运行下表无界增长。

    注意：`TaskManager.start()` 内部会顺手 reap 一次，因此连续起任务时旧 handle
    会先被释放（released=True）再按上限摘除——这里验证的正是这条链路。
    """
    app = _make_app(tmp_path)
    app._mock_adapter = MockLLMAdapter([("完成", [])])
    tm = TaskManager(app)

    handles = []
    total = TaskManager.FINISHED_KEEP_MAX + 3
    for i in range(total):
        handles.append(await _run_task(tm, f"s-{i}"))
    # start() 的顺手 reap 已把旧 handle 释放掉（重对象不再驻留）
    assert all(h.released for h in handles[:-1]), "旧 handle 应已被释放"
    # 上限判定发生在「新任务插入之后」，因此允许恰好超出 1 条
    assert len(tm.tasks) <= TaskManager.FINISHED_KEEP_MAX + 1

    tm.reap()

    assert len(tm.tasks) <= TaskManager.FINISHED_KEEP_MAX
    # 最新那条仍可查询（界面还在用它收尾）
    assert tm.get(handles[-1].task_id) is not None
