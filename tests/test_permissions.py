# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""职责域权限模型测试：换算、旧配置迁移、各 Agent 工具面。"""
from __future__ import annotations

import os
import tempfile

from litework.app import AgentApp
from litework.core.agent_profile import AgentProfile, AgentRegistry
from litework.core.permissions import (
    DEFAULT_DOMAIN_ACTIONS,
    TOOL_DOMAIN,
    domain_of,
    domains_to_allowed_and_permissions,
    resolved_domains,
)


# ---------------------------------------------------------------- 换算函数

def test_domain_of_known_and_unknown():
    assert domain_of("write_file") == "edit"
    assert domain_of("execute_command") == "execute"
    assert domain_of("plan_save") == "plan"
    assert domain_of("no_such_tool_xyz") == "misc"  # 未知工具归 misc


def test_resolved_domains_merges_defaults():
    r = resolved_domains({"edit": "allow"})
    assert r["edit"] == "allow"
    assert r["read"] == DEFAULT_DOMAIN_ACTIONS["read"] == "allow"
    assert r["execute"] == "deny"  # 未声明域跟随默认（偏安全）
    # 非法值忽略
    r2 = resolved_domains({"edit": "maybe", "execute": "allow"})
    assert r2["edit"] == "deny"
    assert r2["execute"] == "allow"


def test_domains_to_allowed_and_permissions():
    all_tools = ["read_file", "write_file", "execute_command", "plan_save", "webfetch", "mcp_x"]
    allowed, perms = domains_to_allowed_and_permissions(
        {"read": "allow", "edit": "deny", "execute": "ask", "plan": "allow", "web": "allow"},
        all_tools,
    )
    assert "read_file" in allowed
    assert "plan_save" in allowed
    assert "execute_command" in allowed       # ask 也进白名单
    assert perms["execute_command"] == "ask"  # ask 同时进 permissions
    assert perms["write_file"] == "deny"      # deny 双保险
    assert "write_file" not in allowed
    # misc 默认 ask
    assert perms["mcp_x"] == "ask"
    assert "mcp_x" in allowed


def test_extra_tools_appended():
    allowed, _ = domains_to_allowed_and_permissions(
        {"read": "allow"}, ["read_file", "write_file"], extra_tools=["write_file"],
    )
    assert "write_file" in allowed


# ---------------------------------------------------------------- 旧配置迁移

def test_legacy_tools_permissions_infer_domains():
    """旧 tools+permissions 配置 → 职责域反推（deny 优先）。"""
    reg = AgentRegistry()
    # plan 历史覆盖：只读 tools + 写工具 deny
    legacy = AgentProfile(
        id="legacy-plan",
        mode="primary",
        tools=["read_file", "search_code", "webfetch"],
        permissions={"write_file": "deny", "apply_search_replace": "deny"},
    )
    reg.register(legacy)
    prof = reg.get("legacy-plan")
    assert prof.domains.get("read") == "allow"
    assert prof.domains.get("edit") == "deny"
    assert prof.domains.get("web") == "allow"
    # ask 迁移
    legacy2 = AgentProfile(
        id="legacy-office",
        mode="primary",
        tools=["docx_create", "read_file", "execute_command"],
        permissions={"execute_command": "ask"},
    )
    reg.register(legacy2)
    assert reg.get("legacy-office").domains.get("office") == "allow"
    assert reg.get("legacy-office").domains.get("execute") == "ask"


# ---------------------------------------------------------------- 各 Agent 工具面

WRITE_TOOLS = {"write_file", "apply_search_replace", "apply_unified_diff", "edit_files",
               "execute_command", "git_commit"}


def _make_app():
    # 临时目录用系统临时目录：此前 `TEMP or "."` 在 macOS/Linux（无 TEMP）
    # 会把 lite-work-test-perms/ 落到仓库根目录，污染工作区（未跟踪噪音）
    return AgentApp(workspace=os.getcwd(), config_dir=os.path.join(
        tempfile.gettempdir(), "lite-work-test-perms"))


def test_builtin_agents_tool_face():
    app = _make_app()
    # plan：只读 + plan_save + 协作，无任何通用写工具
    plan = set(app.create_agent_registry("plan").names())
    assert WRITE_TOOLS.isdisjoint(plan)
    assert "plan_save" in plan
    assert "spawn_agent" in plan
    # build：全量（含写工具），无 plan_save（plan 专属）
    build = set(app.create_agent_registry("build").names())
    assert "write_file" in build
    assert "execute_command" in build
    assert "plan_save" not in build
    # office：办公 + 读写，execute 为 ask
    office = set(app.create_agent_registry("office").names())
    assert "docx_create" in office
    assert "write_file" in office
    assert "git_commit" not in office
    # research：只读 + 办公产出，无命令执行
    research = set(app.create_agent_registry("research").names())
    assert "webfetch" in research
    assert "docx_create" in research
    assert "write_file" not in research
    assert "execute_command" not in research


def test_plan_save_tool_defined_globally():
    """plan_save 工具定义已注册到插件层（PlanSavePlugin），仅 plan 域放行。"""
    from litework.tools.plugin import PlanSavePlugin
    app = _make_app()
    plugin = PlanSavePlugin(app)
    tools = plugin.get_tools()
    assert len(tools) == 1 and tools[0].name == "plan_save"
    # 域映射一致
    assert TOOL_DOMAIN["plan_save"] == "plan"

# ---------------------------------------------------------------- 插件只读工具声明


def test_declared_plugin_readonly_tools_enter_read_domain():
    """插件自述的只读工具归 read 域，无人值守 read 档可用。

    背景：插件工具默认归 misc，而 misc 在无人值守档下被 deny——结果盯盘这类
    计划在盯梢任务里调不到任何自己的工具，只能回「数据工具缺失、无法生成」。
    插件主动声明 read_only_tools 后，这些工具在 read 档下恢复可用。
    """
    tools = ["read_file", "astock_quote", "astock_watch"]
    declared = {"astock_quote"}          # 只声明读行情

    allowed, _ = domains_to_allowed_and_permissions(
        None, tools, None, "read", extra_read_only=declared,
    )
    assert "read_file" in allowed
    assert "astock_quote" in allowed, "已声明的只读插件工具应可用"
    assert "astock_watch" not in allowed, "未声明的插件工具必须仍被禁"


def test_undeclared_plugin_tools_stay_denied():
    """**安全默认不变**：没有声明的插件工具仍然归 misc，无人值守下被禁。

    这条是关键约束——只读协议不能让所有插件工具自动获得放行。
    """
    tools = ["some_plugin_tool", "another_plugin_tool"]
    allowed, perms = domains_to_allowed_and_permissions(
        None, tools, None, "read", extra_read_only=set(),
    )
    assert allowed == []
    assert perms["some_plugin_tool"] == "deny"
    assert domain_of("some_plugin_tool") == "misc"


def test_builtin_tool_domain_not_overridden_by_declaration():
    """插件声明不能改写**内置工具**的域（声明只对插件工具生效）。

    否则一个插件可以声明 `execute_command` 为只读，等于绕过权限模型。
    """
    allowed, _ = domains_to_allowed_and_permissions(
        None, ["execute_command"], None, "read",
        extra_read_only={"execute_command"},
    )
    assert "execute_command" not in allowed, "内置 execute 域不能被声明改成 read"
    assert domain_of("execute_command", {"execute_command"}) == "execute"


def test_app_collects_readonly_declarations_from_plugin_classes(tmp_path):
    """AgentApp 能从用户插件目录的**类属性**汇总声明（不实例化插件）。"""
    import textwrap

    app = AgentApp(config_dir=str(tmp_path / "cfg"))
    pdir = os.path.join(app.config_dir, "plugins", "demo")
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, "plugin.py"), "w", encoding="utf-8") as f:
        f.write(textwrap.dedent('''
            from litework.tools.plugin import ToolPlugin

            class DemoPlugin(ToolPlugin):
                name = "demo-plugin"
                # 只读声明
                read_only_tools = ["demo_quote", "demo_kline"]

                def get_tools(self):
                    return []

                async def execute(self, name, args):
                    return ""
        '''))
    assert app.plugin_read_only_tools() == {"demo_quote", "demo_kline"}
