# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""隔离工作树模式的工具边界检查（AgentLoop._worktree_isolation_violation）。"""
from __future__ import annotations

import os

from litework.core.agent_loop import AgentLoop


def _loop(isolation_root: str, main_workspace: str) -> AgentLoop:
    """最小构造：只设置边界检查用到的两个属性。"""
    loop = AgentLoop.__new__(AgentLoop)
    loop.isolation_root = isolation_root
    loop.main_workspace = main_workspace
    return loop


def test_non_isolation_always_allowed(tmp_path):
    """非隔离模式（isolation_root=None）：不拦截任何调用。"""
    loop = AgentLoop.__new__(AgentLoop)
    loop.isolation_root = None
    loop.main_workspace = None
    assert loop._worktree_isolation_violation("write_file", {"filePath": "/etc/passwd"}) is None


def test_file_path_inside_worktree_allowed(tmp_path):
    wt = str(tmp_path / "wt")
    os.makedirs(wt)
    loop = _loop(wt, str(tmp_path / "main"))
    assert loop._worktree_isolation_violation("write_file", {"filePath": "a/b.txt"}) is None
    assert loop._worktree_isolation_violation("read_file", {"filePath": os.path.join(wt, "x.py")}) is None


def test_file_path_outside_worktree_denied(tmp_path):
    wt = str(tmp_path / "wt")
    os.makedirs(wt)
    main = str(tmp_path / "main")
    os.makedirs(main)
    loop = _loop(wt, main)
    v = loop._worktree_isolation_violation(
        "write_file", {"filePath": os.path.join(main, "important.py")})
    assert v is not None and "Worktree Isolation" in v
    # 绝对路径越界（父目录穿越也算）
    v2 = loop._worktree_isolation_violation("read_file", {"filePath": "../outside.txt"})
    assert v2 is not None


def test_command_referencing_main_workspace_denied(tmp_path):
    wt = str(tmp_path / "wt")
    os.makedirs(wt)
    main = str(tmp_path / "main")
    os.makedirs(main)
    loop = _loop(wt, main)
    v = loop._worktree_isolation_violation(
        "execute_command", {"command": f"cd {main} && git status"})
    assert v is not None and "主工作区" in v


def test_command_git_redirect_denied(tmp_path):
    wt = str(tmp_path / "wt")
    os.makedirs(wt)
    loop = _loop(wt, str(tmp_path / "main"))
    assert loop._worktree_isolation_violation(
        "execute_command", {"command": "GIT_DIR=/repo/.git git status"}) is not None
    assert loop._worktree_isolation_violation(
        "execute_command", {"command": "git --git-dir=/repo/.git log"}) is not None


def test_command_inside_worktree_allowed(tmp_path):
    wt = str(tmp_path / "wt")
    os.makedirs(wt)
    loop = _loop(wt, str(tmp_path / "main"))
    assert loop._worktree_isolation_violation(
        "execute_command", {"command": "pytest tests/ -q"}) is None
    assert loop._worktree_isolation_violation(
        "execute_command", {"command": f"ls {wt}/src"}) is None


def test_cwd_outside_denied(tmp_path):
    wt = str(tmp_path / "wt")
    os.makedirs(wt)
    main = str(tmp_path / "main")
    os.makedirs(main)
    loop = _loop(wt, main)
    assert loop._worktree_isolation_violation("execute_command", {"cwd": main}) is not None
