# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""插件版本感知优先级：本地社区版仅在不低于内置版时才覆盖（防内置插件名存实亡）。

覆盖四象限：local 新 / local 旧 / 版本相等 / 版本缺失，以及
tool_plugins / collab_modes / plugins_builtin 三处裁决点的一致性。
"""
from __future__ import annotations

import os

from litework.tools.plugin_loader import local_shadows_builtin


# ------------------------------------------------------------ local_shadows_builtin 纯函数

def test_shadow_local_newer() -> None:
    """本地版更新 → 覆盖内置（社区更新机制不变）。"""
    assert local_shadows_builtin("1.5.0", "1.4.1") is True


def test_shadow_local_older() -> None:
    """本地版更旧 → 被内置旁路（核心修复：装新 lite-work 不必再手动更新社区插件）。"""
    assert local_shadows_builtin("1.3.0", "1.4.1") is False


def test_shadow_equal() -> None:
    """版本相等 → 本地优先（维持旧行为，等价代码无需翻动）。"""
    assert local_shadows_builtin("1.4.1", "1.4.1") is True


def test_shadow_missing_version_fallback() -> None:
    """任一侧版本缺失 → 无法比较 → 回退旧行为（本地优先，保守兼容）。"""
    assert local_shadows_builtin("", "1.4.1") is True
    assert local_shadows_builtin("1.3.0", "") is True
    assert local_shadows_builtin("", "") is True


def test_shadow_semver_aware() -> None:
    """semver 语义：1.10.0 > 1.9.0（不是字符串比较）；预发布 < 正式。"""
    assert local_shadows_builtin("1.10.0", "1.9.0") is True
    assert local_shadows_builtin("1.9.0", "1.10.0") is False
    assert local_shadows_builtin("1.4.1-rc.1", "1.4.1") is False


# ------------------------------------------------------------ tool_plugins 装配裁决

def _local_office_plugin(root: str, version: str, tool_name: str = "t_local_office") -> None:
    """写一个与内置 office-plugin 同名的本地插件（版本可调，带哨兵工具名）。"""
    body = f'''
from litework.core.types import ToolDefinition
from litework.tools.plugin import ToolPlugin


class OfficePlugin(ToolPlugin):
    name = "office-plugin"
    version = "{version}"
    description = "local shadow test"

    def __init__(self):
        pass

    def get_tools(self):
        return [ToolDefinition(name="{tool_name}", description="d", parameters={{}})]

    async def execute(self, name, args):
        return "local"
'''
    d = os.path.join(root, "office-plugin")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "plugin.py"), "w", encoding="utf-8") as f:
        f.write(body)


def _app_with_local_office(tmp_path, version: str, tool_name: str = "t_local_office"):
    from litework.app import AgentApp

    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lc"))
    _local_office_plugin(os.path.join(app.config_dir, "plugins"), version, tool_name)
    return app


def _tool_names(plugins) -> set:
    names: set = set()
    for p in plugins:
        try:
            names.update(t.name for t in p.get_tools())
        except Exception:
            pass
    return names


def test_tool_plugins_local_older_bypassed(tmp_path) -> None:
    """本地 office-plugin v1.0.0 < 内置 v1.4.1 → 内置生效，本地不进装配。"""
    app = _app_with_local_office(tmp_path, "1.0.0", tool_name="t_local_marker")
    plugins = app.tool_plugins()
    names = _tool_names(plugins)
    # 内置版工具在（docx_create 等），本地哨兵工具不在
    assert "docx_create" in names
    assert "t_local_marker" not in names


def test_tool_plugins_local_newer_wins(tmp_path) -> None:
    """本地 office-plugin v9.0.0 > 内置 v1.4.1 → 本地生效（哨兵工具在），内置被跳过。"""
    app = _app_with_local_office(tmp_path, "9.0.0", tool_name="t_local_marker")
    plugins = app.tool_plugins()
    names = _tool_names(plugins)
    assert "t_local_marker" in names
    # 同名覆盖：内置 office 的工具集不应出现（本地版顶替）
    assert "docx_create" not in names


def test_tool_plugins_no_version_fallback_local(tmp_path) -> None:
    """本地版无版本号 → 回退旧行为（本地覆盖）。"""
    app = _app_with_local_office(tmp_path, "", tool_name="t_local_marker")
    plugins = app.tool_plugins()
    names = _tool_names(plugins)
    assert "t_local_marker" in names


def test_plugins_builtin_flags(tmp_path) -> None:
    """plugins_builtin 元信息：overridden 与 stale_local 随版本关系翻转。"""
    app = _app_with_local_office(tmp_path, "1.0.0")
    by = {p["name"]: p for p in app.plugins_builtin()}
    office = by["office-plugin"]
    assert office["stale_local"] is True
    assert office["overridden"] is False
    assert office["local_version"] == "1.0.0"

    app2 = _app_with_local_office(tmp_path / "b", "9.0.0")
    by2 = {p["name"]: p for p in app2.plugins_builtin()}
    office2 = by2["office-plugin"]
    assert office2["overridden"] is True
    assert office2["stale_local"] is False


def test_collab_modes_local_older_bypassed(tmp_path) -> None:
    """协作模式同样版本感知：本地旧版被内置旁路。"""
    from litework.app import AgentApp
    from tests.test_plugin_listing import _write_plugin  # 复用插件写入助手

    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lc"))
    # 内置 collab-pipeline v1.0.0：写一个同名 v0.1.0 本地包
    body = '''
from litework.orchestration.collab_policy import CollabModePlugin


class PipelineMode(CollabModePlugin):
    name = "collab-pipeline"
    version = "0.1.0"
    description = "stale local"
    mode_name = "pipeline"
    display_name = "流水线（本地旧版）"
'''
    _write_plugin(os.path.join(app.config_dir, "plugins"), "collab-pipeline", body)
    modes = app.collab_modes()
    names = {m.name for m in modes}
    assert "collab-pipeline" in names
    # 内置版生效：本地旧版的 display_name 不应出现
    display = {getattr(m, "display_name", "") for m in modes}
    assert "流水线（本地旧版）" not in display
