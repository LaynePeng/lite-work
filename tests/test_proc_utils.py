# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""proc_utils（进程树击杀）单测：跨平台分支 mock 验证调用参数与吞错行为。"""
from __future__ import annotations

import sys
from unittest import mock

import pytest

from litework.tools.proc_utils import kill_process_tree, posix_spawn_kwargs


def test_spawn_kwargs_windows_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 无进程组概念：启动参数为空（不传 start_new_session）。"""
    monkeypatch.setattr(sys, "platform", "win32")
    assert posix_spawn_kwargs() == {}


def test_spawn_kwargs_posix_new_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """POSIX：start_new_session=True（自成组长是 killpg 整树击杀的前提）。"""
    monkeypatch.setattr(sys, "platform", "linux")
    assert posix_spawn_kwargs() == {"start_new_session": True}


def test_kill_windows_taskkill_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows：taskkill /PID <pid> /T /F（/T 整树 /F 强制）。"""
    monkeypatch.setattr(sys, "platform", "win32")
    with mock.patch("subprocess.run") as run:
        kill_process_tree(1234)
    run.assert_called_once()
    args, kwargs = run.call_args
    assert args[0] == ["taskkill", "/PID", "1234", "/T", "/F"]
    assert kwargs.get("capture_output") is True


def test_kill_posix_group_leader_killpg(monkeypatch: pytest.MonkeyPatch) -> None:
    """POSIX 自成组长：killpg 整组击杀。"""
    monkeypatch.setattr(sys, "platform", "linux")
    with mock.patch("os.getpgid", return_value=1234, create=True), \
         mock.patch("os.killpg", create=True) as killpg:
        kill_process_tree(1234)
    killpg.assert_called_once_with(1234, mock.ANY)


def test_kill_posix_not_leader_fallback_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    """POSIX 未自成组长：不能 killpg（可能误杀自身进程组），退化为只杀直接子进程。"""
    monkeypatch.setattr(sys, "platform", "linux")
    with mock.patch("os.getpgid", return_value=1, create=True), \
         mock.patch("os.kill", create=True) as kill:
        kill_process_tree(1234)
    kill.assert_called_once_with(1234, mock.ANY)


def test_kill_swallows_process_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """进程已死（ProcessLookupError）静默不抛——兜底路径不允许抛错。"""
    monkeypatch.setattr(sys, "platform", "linux")
    with mock.patch("os.getpgid", side_effect=ProcessLookupError(), create=True):
        kill_process_tree(999)  # 不应抛异常


def test_kill_windows_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """taskkill 失败（超时/OS 错误）静默不抛。"""
    monkeypatch.setattr(sys, "platform", "win32")
    with mock.patch("subprocess.run", side_effect=OSError("boom")):
        kill_process_tree(999)  # 不应抛异常


def test_kill_invalid_pid_noop() -> None:
    """非正 pid 直接忽略（不发起任何系统调用）。"""
    with mock.patch("subprocess.run") as run:
        kill_process_tree(0)
        kill_process_tree(-1)
    run.assert_not_called()
