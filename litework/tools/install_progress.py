# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""安装进度上报：跨调用栈的轻量 contextvar 通道。

为什么用 contextvar 而不是层层传参：安装链路很深（router → app →
plugin_loader/skills → gh_fetch），而且安装跑在**独立的后台线程**里。
在后台线程入口 `set_progress(...)` 后，同线程内任意深度的函数都能
`report(...)` 上报进度，无需改动每个函数签名。

注意：contextvar 不跨线程传播。gh_fetch 内部用线程池并发下载，需在
`fetch_subpath` 里 `_current.get()` 取出回调后再传给 worker（见 gh_fetch）。
"""
from __future__ import annotations

import contextvars
import threading
from typing import Any, Callable, Dict, Optional

ProgressEmit = Callable[[Dict[str, Any]], None]

_current: contextvars.ContextVar[Optional[ProgressEmit]] = contextvars.ContextVar(
    "litework_install_progress", default=None
)
_control: contextvars.ContextVar[Optional["InstallControl"]] = contextvars.ContextVar(
    "litework_install_control", default=None
)


class InstallCancelled(Exception):
    """用户取消安装（取消时下载缓存会被清理）。"""


class InstallControl:
    """安装任务的协作式控制（暂停 / 取消）。

    - checkpoint()：在下载 / 安装循环中周期调用。已请求取消 → 抛
      InstallCancelled；已请求暂停 → 阻塞直到恢复或取消。
    - 事件由 InstallJobRegistry 创建并持有，run_job 在 worker 线程入口
      set_install_control 后，同线程任意深度的代码都能 checkpoint()。
    """

    __slots__ = ("cancel_event", "pause_event")

    def __init__(self) -> None:
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()

    def checkpoint(self) -> None:
        if self.cancel_event.is_set():
            raise InstallCancelled()
        while self.pause_event.is_set():
            # 0.2s 轮询取消，暂停中也能即时取消
            if self.cancel_event.wait(timeout=0.2):
                raise InstallCancelled()


# 安装步骤（与前端进度条步骤标签一一对应）——阶段索引
STAGE_CONNECT = 0    # 连接仓库 / 解析来源
STAGE_LIST = 1       # 列出文件
STAGE_DOWNLOAD = 2   # 下载文件（带字节进度）
STAGE_INSTALL = 3    # 安装 / 解依赖

PLUGIN_STEPS = ["连接仓库", "列出文件", "下载文件", "安装插件", "完成"]
SKILL_STEPS = ["连接仓库", "列出文件", "下载文件", "安装技能", "完成"]
LOCAL_STEPS = ["读取来源", "安装", "完成"]


def set_progress(emit: ProgressEmit):
    """在后台线程入口设置进度回调，返回可用于 reset 的 token。"""
    return _current.set(emit)


def reset_progress(token: Any) -> None:
    try:
        _current.reset(token)
    except (ValueError, LookupError):
        pass


def get_progress() -> Optional[ProgressEmit]:
    """取出当前进度回调（供需跨线程传递的场景显式捕获）。"""
    return _current.get()


def report(**fields: Any) -> None:
    """上报进度（无回调时静默忽略；回调异常不影响安装主流程）。"""
    emit = _current.get()
    if emit is None:
        return
    try:
        emit(dict(fields))
    except Exception:
        pass


def set_install_control(control: Optional[InstallControl]) -> Any:
    """在后台线程入口设置安装控制句柄，返回可用于 reset 的 token。"""
    return _control.set(control)


def reset_install_control(token: Any) -> None:
    try:
        _control.reset(token)
    except (ValueError, LookupError):
        pass


def get_install_control() -> Optional[InstallControl]:
    """取出当前安装控制句柄（contextvar 不跨线程，线程池 worker 需显式传）。"""
    return _control.get()


def checkpoint() -> None:
    """便捷入口：有控制句柄时执行协作检查点（无则 no-op）。"""
    ctrl = _control.get()
    if ctrl is not None:
        ctrl.checkpoint()
