# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""进程树击杀：工具超时 / 任务取消时杀干净整个子进程树，防孤儿进程。

为什么需要「树」杀：shell 类工具经 create_subprocess_shell 启动的是 shell
（cmd / bash），真实命令是它的子进程——只 kill shell 会留下孙进程孤儿
（npm install / pyinstaller 等长命令尤其常见），继续占用 CPU、文件锁与端口。

- Windows: taskkill /PID <pid> /T /F（/T 连带整棵进程树，/F 强制）
- POSIX:   对进程组发 SIGKILL。子进程须以 start_new_session=True 启动
  （自成进程组长，pgid == pid），killpg 才能整树命中且不误伤调用方自身
  进程组；未自成组长时退化为只杀直接子进程（保守，不会杀到自己）。
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from typing import Any, Dict

logger = logging.getLogger("litework.tools.proc_utils")

# Windows 无 SIGKILL（POSIX-only 信号）；Windows 路径本就走 taskkill，
# 此常量仅供 POSIX 分支使用，缺失时回退 SIGTERM 保证模块在 Windows 可导入
_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def posix_spawn_kwargs() -> Dict[str, Any]:
    """子进程启动参数：POSIX 下自成进程组（killpg 整树击杀的前提），Windows 无此概念。"""
    if sys.platform == "win32":
        return {}
    return {"start_new_session": True}


def kill_process_tree(pid: int) -> None:
    """强杀进程树；进程已退出 / 权限不足时静默（调用方多为超时兜底路径，不抛错）。"""
    if pid is None or pid <= 0:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
            )
        else:
            try:
                pgid = os.getpgid(pid)
            except ProcessLookupError:
                return
            if pgid == pid:
                # 自成组长的进程树（配合 posix_spawn_kwargs）：整组击杀
                os.killpg(pgid, _SIGKILL)
            else:
                # 未自成组长：killpg 可能命中调用方自身进程组，退化为只杀直接子进程
                os.kill(pid, _SIGKILL)
    except (ProcessLookupError, PermissionError, OSError, subprocess.TimeoutExpired):
        # 进程已死 / 已被回收 / 权限不足 / taskkill 自身超时：兜底路径静默
        logger.debug("[ProcUtils] kill_process_tree(%s) 未命中（进程可能已退出）", pid)
