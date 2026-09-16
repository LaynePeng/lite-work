# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""安装任务暂停/取消测试：状态机 + 协作检查点 + 取消清理下载缓存。"""
from __future__ import annotations

import os
import threading
import time

import pytest

from litework.server.install_jobs import InstallJobRegistry, run_job
from litework.tools.gh_fetch import fetch_subpath
from litework.tools.install_progress import (
    InstallCancelled,
    InstallControl,
    checkpoint,
    set_install_control,
)


def _wait_until(pred, timeout: float = 5.0) -> bool:
    """轮询等待条件成立（后台线程状态同步用）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------- 注册表状态机

def test_registry_pause_resume_cancel_flow():
    registry = InstallJobRegistry()
    job = registry.create("skill", ["a", "b", "c"])

    snap = registry.snapshot(job.id)
    assert snap["status"] == "running"

    # 暂停 → 恢复
    assert registry.request_pause(job.id) is True
    assert registry.snapshot(job.id)["status"] == "paused"
    assert job.control.pause_event.is_set()
    assert registry.request_resume(job.id) is True
    assert registry.snapshot(job.id)["status"] == "running"
    assert not job.control.pause_event.is_set()

    # 取消 → cancelling 终态前仍接受进度更新
    assert registry.request_cancel(job.id) is True
    assert registry.snapshot(job.id)["status"] == "cancelling"
    registry.update(job.id, message="最后一个 chunk")
    assert registry.snapshot(job.id)["message"] == "最后一个 chunk"
    assert job.control.cancel_event.is_set()

    # 取消后完成/失败不得覆盖 cancelled 状态
    registry.mark_cancelled(job.id)
    assert registry.snapshot(job.id)["status"] == "cancelled"
    registry.finish(job.id, "x")
    registry.fail(job.id, "y")
    assert registry.snapshot(job.id)["status"] == "cancelled"


def test_registry_cancel_on_paused_job():
    registry = InstallJobRegistry()
    job = registry.create("plugin", ["a"])
    registry.request_pause(job.id)
    assert registry.request_cancel(job.id) is True
    assert registry.snapshot(job.id)["status"] == "cancelling"
    # 暂停中的事件已清除，checkpoint 轮询能即时看到取消
    assert not job.control.pause_event.is_set()


# ---------------------------------------------------------------- run_job 取消收口

def test_run_job_cancelled_not_error():
    """worker 抛 InstallCancelled → job 状态为 cancelled 而非 error。"""
    registry = InstallJobRegistry()

    def work():
        while True:
            time.sleep(0.02)
            checkpoint()

    job = run_job(registry, "skill", ["a"], work)
    assert _wait_until(lambda: registry.snapshot(job.id)["status"] == "running")
    registry.request_cancel(job.id)
    assert _wait_until(lambda: registry.snapshot(job.id)["status"] == "cancelled")
    snap = registry.snapshot(job.id)
    assert snap["status"] == "cancelled"
    assert "缓存" in snap["message"]


def test_run_job_paused_blocks_until_resume():
    """暂停时 worker 阻塞在检查点，恢复后继续执行到完成（事件门保证确定性）。"""
    registry = InstallJobRegistry()
    steps_done: list[str] = []
    ready = threading.Event()   # worker 完成 step0 后置位
    release = threading.Event() # 测试确认已进入暂停后放行

    def work():
        checkpoint()
        steps_done.append("0")
        ready.set()
        release.wait(timeout=5)  # 等测试确认暂停已生效
        for i in range(1, 3):
            checkpoint()  # pause 已置位 → 阻塞在此，直到 resume
            steps_done.append(str(i))
        return "ok"

    job = run_job(registry, "skill", ["a"], work)
    assert _wait_until(lambda: len(steps_done) >= 1 and ready.is_set())
    registry.request_pause(job.id)
    assert _wait_until(lambda: registry.snapshot(job.id)["status"] == "paused")
    release.set()
    time.sleep(0.15)
    paused_len = len(steps_done)
    assert paused_len == 1, "暂停中 worker 应停在检查点"
    time.sleep(0.15)
    assert len(steps_done) == paused_len
    registry.request_resume(job.id)
    assert _wait_until(lambda: registry.snapshot(job.id)["status"] == "done", timeout=5)
    assert len(steps_done) == 3


# ---------------------------------------------------------------- fetch_subpath 取消清理

def _patch_tree(monkeypatch, count: int = 4):
    import httpx

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_api_get(client, url, token):
        if "/commits/" in url:
            return {"sha": "cafef00d1234567890"}
        return {
            "truncated": False,
            "tree": [
                {"type": "blob", "path": f"skills/x/f{i}.txt", "size": 16, "mode": "100644"}
                for i in range(count)
            ],
        }

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    monkeypatch.setattr("litework.tools.gh_fetch._api_get", fake_api_get)


def test_fetch_subpath_cancel_deletes_cache(monkeypatch, tmp_path):
    """取消下载 → 抛 InstallCancelled 且该 repo+commit 的缓存目录被删除。"""
    _patch_tree(monkeypatch)
    control = InstallControl()
    barrier = threading.Barrier(2)  # 每个文件 worker 到齐后再统一触发取消

    def fake_download(owner, repo, commit, path, dest, expected_size, retries,
                      on_bytes=None, control=None):
        assert control is not None, "worker 应显式收到 control"
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest + ".part", "wb") as f:
            f.write(b"partial")
        barrier.wait(timeout=5)  # 两个 worker 都写过 .part 后再取消
        control.cancel_event.set()
        control.checkpoint()  # 应抛 InstallCancelled
        raise AssertionError("unreachable")

    monkeypatch.setattr("litework.tools.gh_fetch._download_one", fake_download)

    cache_root = tmp_path / "cache"
    with pytest.raises(InstallCancelled):
        fetch_subpath(str(cache_root), "o", "r", "main", "skills/x",
                      max_workers=2, control=control)
    base = cache_root / "o__r__cafef00d1234"
    assert not base.exists(), "取消后应删除该 repo+commit 的下载缓存"


def test_fetch_subpath_pause_resume(monkeypatch, tmp_path):
    """暂停期间 worker 阻塞，恢复后继续下载到完成。"""
    _patch_tree(monkeypatch, count=2)

    control = InstallControl()
    downloaded: list[str] = []

    def fake_download(owner, repo, commit, path, dest, expected_size, retries,
                      on_bytes=None, control=None):
        control.checkpoint()  # 每个文件一个检查点：暂停时阻塞在此
        downloaded.append(path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(b"0" * 16)

    monkeypatch.setattr("litework.tools.gh_fetch._download_one", fake_download)

    worker = threading.Thread(
        target=lambda: fetch_subpath(str(tmp_path / "cache"), "o", "r", "main",
                                     "skills/x", max_workers=2, control=control),
        daemon=True,
    )
    worker.start()
    assert _wait_until(lambda: len(downloaded) >= 1)
    control.pause_event.set()
    time.sleep(0.15)
    paused = len(downloaded)
    time.sleep(0.1)
    assert len(downloaded) == paused  # 暂停中不推进
    control.pause_event.clear()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert sorted(downloaded) == ["skills/x/f0.txt", "skills/x/f1.txt"]
