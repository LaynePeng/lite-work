# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""内置 office/ocr 包装类的工作区解析（v1.7.1 / v1.2.1 修复回归）。

真实故障：桌面多端模式下 `POST /api/workspace` 只登记该端的 client-view、
**不写全局 `app.workspace`**（server/routers/workspace.py）。`_builtin_plugins`
于是在全局值为 None 时用家目录兜底：

    ws = workspace_override or self.workspace or os.path.expanduser("~")

面板等**非任务路径**构造出的插件实例就以家目录为工作区——项目内文件被
`_resolve_path` 判「路径越界」。修复方式是执行期按 header_context 重新解析
（任务钉死的工作区），而不是沿用构造时的绑定值。
"""
from __future__ import annotations

import os

from litework.core.types import header_context
from litework.tools.plugin import OfficePlugin, OcrPlugin, _runtime_workspace


def test_runtime_workspace_prefers_header_context(tmp_path):
    """任务执行期：header_context 里的工作区优先于构造时的绑定值。"""
    project = tmp_path / "proj"
    project.mkdir()
    home = os.path.expanduser("~")

    token = header_context.set({"workspace": str(project)})
    try:
        assert _runtime_workspace(home) == str(project)
        assert _runtime_workspace(str(project)) == str(project)
    finally:
        header_context.reset(token)


def test_runtime_workspace_falls_back_to_bound(tmp_path):
    """无任务上下文时保持原行为（面板/工具列表不会因修复而跳变）。"""
    token = header_context.set({})
    try:
        assert _runtime_workspace(str(tmp_path)) == str(tmp_path)
    finally:
        header_context.reset(token)


def test_runtime_workspace_rejects_relative_and_missing(tmp_path):
    """相对路径与不存在的目录一律丢弃。

    `AgentLoop` 在无项目时会把 workspace 置为 "."，若按 CWD 解析，
    等于把进程目录（打包版即 Contents/Resources）当成工作区。
    """
    home = os.path.expanduser("~")
    for bad in (".", "..", "relative/dir", str(tmp_path / "nope"), ""):
        token = header_context.set({"workspace": bad})
        try:
            assert _runtime_workspace(home) == home, f"{bad!r} 不应被采纳"
        finally:
            header_context.reset(token)


def test_wrappers_versions_bumped():
    """版本必须与社区 manifest 对齐（社区 1.7.1 / 1.2.1）。"""
    assert OfficePlugin.version == "1.7.1"
    assert OcrPlugin.version == "1.2.1"


def test_wrappers_rebind_workspace_during_task(tmp_path):
    """包装类在任务期改绑工作区；无任务时沿用绑定值（不重建，零开销）。"""
    project = tmp_path / "proj"
    project.mkdir()
    home = os.path.expanduser("~")

    for cls in (OfficePlugin, OcrPlugin):
        plugin = cls(home)
        assert plugin._bound_workspace == home

    token = header_context.set({"workspace": str(project)})
    try:
        # 执行期解析出项目目录 → 与绑定值不同 → execute 会重建 tools
        assert _runtime_workspace(home) == str(project)
        assert _runtime_workspace(home) != home
    finally:
        header_context.reset(token)
