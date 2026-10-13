# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""盯梢 / 定时（watch & schedule）。

三个模块：

- `schedule_store`：计划持久化 + 跨进程触发租约（多窗口各一个 Core，必须防重复触发）；
- `registry`：`WatchSource` 数据源插件扩展点 + 内置 command/file/url 源；
- `scheduler`：调度循环（interval/daily/cron + catch-up）+ 探针→指纹→按需唤 LLM。

对外入口是 `WatchScheduler`，由 `AgentApp` 持有并在服务 lifespan 里启停。
"""
from .notify import WatchNotificationBus
from .registry import ProbeContext, ProbeResult, WatchSource, WatchSourceRegistry
from .schedule_store import RunRecord, Schedule, ScheduleStore, TriggerLease
from .scheduler import WatchScheduler, compute_next_run

__all__ = [
    "ProbeContext",
    "ProbeResult",
    "WatchSource",
    "WatchSourceRegistry",
    "RunRecord",
    "Schedule",
    "ScheduleStore",
    "TriggerLease",
    "WatchScheduler",
    "WatchNotificationBus",
    "compute_next_run",
]
