# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""工具渐进式披露（按需工具组）回归测试，设计见 docs/deferred-tools-design.md。

锁定的语义：
1. 骨架插件（文件/编辑/命令/git/搜索…）永远常驻；
2. 领域插件（office/email/astock…）默认不发 schema；
3. `load_tools(group)` 装载后，该组工具才进请求；
4. 装载是本任务单调的，且**不改 registry 语义**（执行/存在性查询仍全量）；
5. 配置 `deferred_tools: false` 一键退回全体常驻。
"""
from __future__ import annotations

import asyncio

from litework.core.types import ToolDefinition
from litework.tools.loadout import (
    CORE_PLUGINS,
    ToolLoadout,
    group_name_for_plugin,
)


def _tool(name: str) -> ToolDefinition:
    return ToolDefinition(name=name, description=f"{name} 的说明", parameters={"type": "object"})


PLUGIN_TOOLS = {
    "filesystem-plugin": ["read_file", "write_file"],
    "shell-plugin": ["execute_command"],
    "office-plugin": ["docx_create", "xlsx_create", "docx_read"],
    "email-plugin": ["email_send", "email_list"],
}


# ---------------------------------------------------------------- 分组

def test_group_name_strips_plugin_suffix():
    assert group_name_for_plugin("office-plugin") == "office"
    assert group_name_for_plugin("astock-plugin") == "astock"
    assert group_name_for_plugin("plain") == "plain"


def test_core_plugins_are_never_deferred():
    """骨架工具不进任何按需组 —— 否则读写/命令都要往返一次装载。"""
    loadout = ToolLoadout.from_plugin_tools(PLUGIN_TOOLS)
    assert "read_file" not in loadout.tool_group
    assert "execute_command" not in loadout.tool_group
    assert loadout.is_deferred("read_file") is False


def test_domain_plugins_become_deferred_groups():
    loadout = ToolLoadout.from_plugin_tools(PLUGIN_TOOLS)
    assert set(loadout.groups) == {"office", "email"}
    assert loadout.is_deferred("docx_create") is True
    assert loadout.is_deferred("email_send") is True
    assert loadout.group_of("docx_create") == "office"


# ---------------------------------------------------------------- select

def test_select_drops_deferred_tools_before_load():
    loadout = ToolLoadout.from_plugin_tools(PLUGIN_TOOLS)
    tools = [_tool(n) for n in
             ("read_file", "docx_create", "execute_command", "email_send")]
    chosen = [t.name for t in loadout.select(tools)]
    assert chosen == ["read_file", "execute_command"]


def test_select_restores_group_after_load():
    loadout = ToolLoadout.from_plugin_tools(PLUGIN_TOOLS)
    tools = [_tool(n) for n in ("read_file", "docx_create", "email_send")]
    loadout.load("office")
    chosen = [t.name for t in loadout.select(tools)]
    assert "docx_create" in chosen
    assert "email_send" not in chosen      # 只装了 office


def test_select_preserves_order():
    """顺序稳定是缓存前缀的前提：select 不能重排。"""
    loadout = ToolLoadout.from_plugin_tools(PLUGIN_TOOLS)
    names = ["read_file", "write_file", "execute_command"]
    tools = [_tool(n) for n in names]
    assert [t.name for t in loadout.select(tools)] == names


# ---------------------------------------------------------------- 装载

def test_load_is_idempotent():
    loadout = ToolLoadout.from_plugin_tools(PLUGIN_TOOLS)
    assert loadout.load("office") is not None
    before = set(loadout.loaded)
    assert loadout.load("office") is not None
    assert loadout.loaded == before


def test_load_accepts_plugin_name():
    loadout = ToolLoadout.from_plugin_tools(PLUGIN_TOOLS)
    assert loadout.load("office-plugin").name == "office"


def test_load_unknown_group_returns_none():
    loadout = ToolLoadout.from_plugin_tools(PLUGIN_TOOLS)
    assert loadout.load("not-a-group") is None
    assert loadout.loaded == set()


def test_unavailable_hint_mentions_group_and_tool():
    loadout = ToolLoadout.from_plugin_tools(PLUGIN_TOOLS)
    hint = loadout.unavailable_hint("docx_create")
    assert hint is not None
    assert "office" in hint and "docx_create" in hint
    assert "load_tools" in hint


def test_unavailable_hint_none_for_core_or_loaded():
    loadout = ToolLoadout.from_plugin_tools(PLUGIN_TOOLS)
    assert loadout.unavailable_hint("read_file") is None
    loadout.load("office")
    assert loadout.unavailable_hint("docx_create") is None


# ---------------------------------------------------------------- 总开关

def test_disabled_loadout_keeps_everything():
    """deferred_tools=false 等价旧行为：所有工具常驻。"""
    loadout = ToolLoadout.from_plugin_tools(PLUGIN_TOOLS, enabled=False)
    tools = [_tool("read_file"), _tool("docx_create")]
    assert len(loadout.select(tools)) == 2
    assert loadout.is_deferred("docx_create") is False
    assert loadout.pending_groups() == []


# ---------------------------------------------------------------- 接真实装配

def test_core_plugins_cover_the_baseline_tools():
    """骨架组必须覆盖「任何任务都可能用」的插件，别把它们漏进按需。"""
    for name in ("filesystem-plugin", "editor-plugin", "shell-plugin",
                 "git-plugin", "codebase-plugin", "todo-plugin", "question-plugin"):
        assert name in CORE_PLUGINS


def test_app_registry_exposes_loadout(tmp_path):
    """AgentApp 装配出的 registry 带可用的 loadout，且领域工具默认不发。"""
    from litework.app import AgentApp

    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite"))
    registry = app.build_registry(workspace=str(tmp_path))
    loadout = registry.loadout
    assert loadout is not None and loadout.enabled

    all_tools = registry.get_tools()
    chosen = {t.name for t in loadout.select(all_tools)}
    # 骨架常驻
    assert "read_file" in chosen and "execute_command" in chosen
    # 领域按需：没装载则不出现
    assert "docx_create" not in chosen
    # registry 本身仍全量（执行/存在性不受影响）
    assert registry.has("docx_create")

    loadout.load("office")
    chosen_after = {t.name for t in loadout.select(all_tools)}
    assert "docx_create" in chosen_after


def test_registry_registers_load_tools(tmp_path):
    """装配阶段就注册 load_tools（/api/tools 与真实请求都从 build_registry 取）。"""
    from litework.app import AgentApp

    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite"))
    registry = app.build_registry(workspace=str(tmp_path))
    assert registry.loadout is not None and registry.loadout.groups
    # 关键：不构造 AgentLoop 也必须已注册，否则接口/请求看不到它
    assert registry.has("load_tools")
    assert not registry.loadout.is_deferred("load_tools")

    # load_tools 的 enum 只列真实组名
    load_tool = next(t for t in registry.get_tools() if t.name == "load_tools")
    enum = load_tool.parameters["properties"]["group"]["enum"]
    assert set(enum) == set(registry.loadout.groups)

    # 执行装载：返回该组工具清单（名称 + 一行摘要）
    out = asyncio.run(registry.execute("load_tools", {"group": "office"}))
    assert "office" in out and "docx_create" in out
    assert registry.loadout.is_deferred("docx_create") is False


def test_skips_load_tools_when_disabled(tmp_path):
    """deferred_tools=false：不注册 load_tools（不白占 schema）。"""
    from litework.app import AgentApp

    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite"))
    app.save_config({"deferred_tools": False})
    registry = app.build_registry(workspace=str(tmp_path))
    assert registry.loadout is not None and not registry.loadout.enabled
    assert not registry.has("load_tools")
    # 全部工具常驻
    chosen = {t.name for t in registry.loadout.select(registry.get_tools())}
    assert "docx_create" in chosen


def test_prompt_lists_deferred_tools_only_once(tmp_path):
    """按需工具不在「可用工具」段重复出现 —— 只在「按需工具组」段按组说明。"""
    from litework.app import AgentApp
    from litework.core.system_prompt import SystemPromptBuilder

    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite"))
    registry = app.build_registry(workspace=str(tmp_path))
    prompt = SystemPromptBuilder.build(
        str(tmp_path), registry.get_tools(), loadout=registry.loadout)

    avail = prompt[prompt.index("### 可用工具"):prompt.index("### 按需工具组")]
    groups = prompt[prompt.index("### 按需工具组"):]
    # 骨架工具逐条列；领域工具只在组段出现（按组说明，不逐条列 schema）
    assert "**read_file**" in avail
    assert "**docx_create**" not in avail
    assert "docx_create" in groups
    assert "**office**" in groups
