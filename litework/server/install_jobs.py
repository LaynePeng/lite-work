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

from ..tools.install_progress import reset_progress, set_progress


@dataclass
class InstallJob:
    id: str
    kind: str                       # "plugin" | "skill"
    steps: List[str]
    status: str = "running"         # running | done | error
    step: int = 0
    message: str = "准备中…"
    files_done: int = 0
    files_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    error: str = ""
    result: Any = None
    created_at: float = field(default_factory=time.time)

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
        elif self.status == "running":
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
            if job is None or job.status != "running":
                return
            for key, value in fields.items():
                if value is None or not hasattr(job, key):
                    continue
                setattr(job, key, value)

    def finish(self, job_id: str, result: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = "done"
            job.step = len(job.steps) - 1
            job.message = "完成"
            job.result = result

    def fail(self, job_id: str, error: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = "error"
            job.error = error or "安装失败"

    def snapshot(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.snapshot() if job is not None else None


def run_job(registry: InstallJobRegistry, kind: str, steps: List[str],
            work: Callable[[], Any]) -> InstallJob:
    """在后台线程执行 work，并把 install_progress 上报桥接到 job 快照。"""
    job = registry.create(kind, steps)

    def _bridge(fields: Dict[str, Any]) -> None:
        registry.update(job.id, **fields)

    def _run() -> None:
        token = set_progress(_bridge)
        try:
            result = work()
            registry.finish(job.id, result)
        except Exception as exc:  # noqa: BLE001 - 安装失败要回传给前端
            registry.fail(job.id, str(exc))
        finally:
            reset_progress(token)

    threading.Thread(target=_run, name=f"install-{job.id}", daemon=True).start()
    return job
