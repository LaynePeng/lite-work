# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""插件装配隔离测试。

背景（真实风险）：社区插件的 `install()` 可能依赖**新核心才有的能力**——例如
新版插件挂 `kernel.before_finish`（Stop 钩子，lite-work ≥ 1.10.3）而用户还在旧核心上，
`AttributeError` 会从 `Kernel.use()` 冒出来。此前 build_registry / create_kernel 是
无保护的 for 循环，**一个插件装不上会让整次工具装配失败**（/api/tools 报错、任务起不来），
用户却只以为是"某个插件功能不能用"。

约定：
- 插件侧：自行做版本守卫（能力缺失只停用该能力）；
- 核心侧（本文件守住）：逐个隔离，失败的跳过并记日志，其余照常装配（fail-safe）。
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from litework.app import AgentApp
from litework.core.types import Plugin, ToolDefinition
from litework.server.app import create_app


class _BrokenPlugin(Plugin):
    """install 抛错（模拟依赖新核心能力的社区插件装在旧核心上）。"""

    name = "broken-plugin"
    version = "0.0.1"
    description = "install 抛 AttributeError"

    def get_tools(self):  # pragma: no cover - 不会走到
        return []

    async def execute(self, name, args):  # pragma: no cover
        return ""

    def install(self, kernel):
        raise AttributeError("'Kernel' object has no attribute 'before_finish'")


class _GoodPlugin(Plugin):
    name = "good-plugin"
    version = "0.0.1"
    description = "正常插件"

    def get_tools(self):
        return [ToolDefinition(name="good_tool", description="ok",
                               parameters={"type": "object", "properties": {}})]

    async def execute(self, name, args):
        return "ok"

    def install(self, kernel):
        registry = kernel.get_service("tools")
        for t in self.get_tools():
            registry.register(t.name, t.description, t.parameters,
                              lambda args: self.execute("good_tool", args))


@pytest.fixture
def app(tmp_path):
    return AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))


def test_build_registry_survives_broken_plugin(app):
    """坏插件被跳过；其余插件工具照常装配（不再整体失败）。"""
    original = app.tool_plugins
    app.tool_plugins = lambda ws=None: list(original(ws)) + [_GoodPlugin(), _BrokenPlugin()]  # type: ignore[assignment]

    registry = app.build_registry()          # 关键：不抛异常
    names = set(registry.names())
    assert "good_tool" in names
    # 内置工具仍在（装配没被中断）
    assert "read_file" in names
    # 坏插件的工具缺席 = fail-safe（不是多给了权限）
    assert "broken_tool" not in names


def test_install_plugins_reports_skipped(app):
    """_install_plugins 返回被跳过的插件名（供日志/面板展示）。"""
    from litework.core.kernel import Kernel

    kernel = Kernel("probe")
    kernel.register_service("tools", app.build_registry())
    skipped = app._install_plugins(kernel, [_GoodPlugin(), _BrokenPlugin()])
    assert skipped == ["broken-plugin"]


def test_create_kernel_survives_broken_plugin(app):
    """任务内核装配路径同样隔离（否则任务起不来）。"""
    original = app.tool_plugins
    app.tool_plugins = lambda ws=None: list(original(ws)) + [_BrokenPlugin()]  # type: ignore[assignment]

    kernel = app.create_kernel("s-broken")   # 不抛异常
    registry = kernel.get_service("tools")
    assert registry is not None


def test_tools_endpoint_still_works_with_broken_plugin(app):
    """/api/tools 不受坏插件影响（用户可见的直接后果）。"""
    original = app.tool_plugins
    app.tool_plugins = lambda ws=None: list(original(ws)) + [_BrokenPlugin()]  # type: ignore[assignment]

    fast = create_app(app, token=None)
    with TestClient(fast) as client:
        resp = client.get("/api/tools")
        assert resp.status_code == 200
        names = {t["name"] for t in resp.json()}
        assert "read_file" in names
