# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""安装任务注册表：后台安装 + 进度快照（供前端轮询进度条）。

为什么需要它：插件/技能的社区安装是「下载 + 解依赖」的慢操作（可续传下载
可达数十秒）。同步接口会让 UI 干等、无法展示「步骤 + 下载进度」。这里把安装
丢到后台线程执行，前端用 `/api/install/jobs/{id}` 轮询快照，实时展示步骤与
下载百分比。
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..tools.install_progress import (
    InstallCancelled,
    InstallControl,
    reset_install_control,
    reset_progress,
    set_install_control,
    set_progress,
)


@dataclass
class InstallJob:
    id: str
    kind: str                       # "plugin" | "skill"
    steps: List[str]
    status: str = "running"         # running | paused | cancelling | cancelled | done | error
    step: int = 0
    message: str = "准备中…"
    files_done: int = 0
    files_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    error: str = ""
    result: Any = None
    created_at: float = field(default_factory=time.time)
    # 协作式控制事件（不进快照）：run_job 里桥接到 worker 线程
    control: InstallControl = field(default_factory=InstallControl, repr=False, compare=False)

    def snapshot(self) -> Dict[str, Any]:
        # 下载百分比：优先按字节；无字节总量时按文件数；未完成封顶 99%
        if self.bytes_total > 0:
            percent = int(self.bytes_done * 100 / self.bytes_total)
        elif self.files_total > 0:
            percent = int(self.files_done * 100 / self.files_total)
        else:
            percent = 0
        if self.status == "done":
            percent = 100
        elif self.status in ("running", "cancelling"):
            percent = min(percent, 99)
        elif self.status == "paused":
            percent = min(percent, 99)
        return {
            "id": self.id,
            "kind": self.kind,
            "steps": self.steps,
            "status": self.status,
            "step": self.step,
            "message": self.message,
            "files_done": self.files_done,
            "files_total": self.files_total,
            "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
            "percent": max(0, percent),
            "error": self.error,
            "result": self.result,
        }


class InstallJobRegistry:
    """线程安全的安装任务表（进程内，保留最近 N 条）。"""

    def __init__(self, keep: int = 50) -> None:
        self._jobs: Dict[str, InstallJob] = {}
        self._order: List[str] = []
        self._lock = threading.Lock()
        self._keep = keep

    def create(self, kind: str, steps: List[str]) -> InstallJob:
        job = InstallJob(id=uuid.uuid4().hex[:12], kind=kind, steps=list(steps))
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            while len(self._order) > self._keep:
                old = self._order.pop(0)
                self._jobs.pop(old, None)
        return job

    def update(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            # 终态（cancelled/done/error）后冻结；cancelling 仍接受进度更新
            # （取消生效前的最后一次下载字节也要体现在快照里）
            if job.status in ("cancelled", "done", "error"):
                return
            for key, value in fields.items():
                if value is None or not hasattr(job, key):
                    continue
                setattr(job, key, value)

    def finish(self, job_id: str, result: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status == "cancelled":
                return
            job.status = "done"
            # step 越过最后一项：所有步骤（含「完成」）都显示 ✓ 绿点。
            # 旧实现定格在 len-1，最后一步永远显示 ● 活动态，看起来没装完。
            job.step = len(job.steps)
            job.message = "完成"
            job.result = result

    def fail(self, job_id: str, error: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status == "cancelled":
                return
            job.status = "error"
            job.error = error or "安装失败"

    def mark_cancelled(self, job_id: str, message: str = "已取消（下载缓存已清理）") -> None:
        """worker 捕获 InstallCancelled 后收口为 cancelled 终态。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status in ("done", "error", "cancelled"):
                return
            job.status = "cancelled"
            job.message = message
            job.error = ""

    # ---------------- 暂停 / 取消控制（API 线程调用） ----------------

    def request_pause(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != "running":
                return False
            job.status = "paused"
            job.control.pause_event.set()
            job.message = "已暂停（下载断点保留）"
            return True

    def request_resume(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != "paused":
                return False
            job.status = "running"
            job.control.pause_event.clear()
            job.message = "继续安装…"
            return True

    def request_cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status in ("done", "error", "cancelled"):
                return False
            job.status = "cancelling"
            job.message = "正在取消…（将清理下载缓存）"
            job.control.cancel_event.set()
            # 暂停中的 worker 阻塞在 checkpoint 轮询 cancel_event，能即时退出
            job.control.pause_event.clear()
            return True

    def snapshot(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.snapshot() if job is not None else None


def run_job(registry: InstallJobRegistry, kind: str, steps: List[str],
            work: Callable[[], Any]) -> InstallJob:
    """在后台线程执行 work，并把 install_progress 上报桥接到 job 快照。

    同时桥接协作式控制：worker 线程内通过 install_progress.checkpoint()
    响应暂停/取消；InstallCancelled 收口为 cancelled 终态（而非 error）。
    """
    job = registry.create(kind, steps)

    def _bridge(fields: Dict[str, Any]) -> None:
        registry.update(job.id, **fields)

    def _run() -> None:
        progress_token = set_progress(_bridge)
        control_token = set_install_control(job.control)
        try:
            result = work()
            registry.finish(job.id, result)
        except InstallCancelled:
            registry.mark_cancelled(job.id)
        except Exception as exc:  # noqa: BLE001 - 安装失败要回传给前端
            registry.fail(job.id, str(exc))
        finally:
            reset_progress(progress_token)
            reset_install_control(control_token)

    threading.Thread(target=_run, name=f"install-{job.id}", daemon=True).start()
    return job
