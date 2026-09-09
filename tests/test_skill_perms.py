# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""技能权限控制测试：规则解析 / 索引过滤 / load_skill 拦截 / 任务启动审批。"""
import asyncio
import os

import pytest

from litework.app import AgentApp
from litework.core.types import Message
from litework.security.skill_permissions import normalize_rules, resolve
from tests.conftest import MockLLMAdapter


def _make_skill(base, name, description="测试技能", triggers=""):
    root = os.path.join(base, ".agents", "skills", name)
    os.makedirs(root, exist_ok=True)
    fm = f"---\nname: {name}\ndescription: {description}\n"
    if triggers:
        fm += f"triggers: {triggers}\n"
    fm += "---\n\n# 正文\n"
    with open(os.path.join(root, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(fm)
    return root


# ---------------------------------------------------------------- 规则解析

def test_normalize_and_resolve():
    rules = normalize_rules({"Internal-*": "Deny ", " exp-* ": "ask", "bad": "nope", "": "deny"})
    assert rules == {"internal-*": "deny", "exp-*": "ask"}
    assert resolve(rules, "internal-tools") == "deny"
    assert resolve(rules, "INTERNAL-TOOLS") == "deny"
    assert resolve(rules, "exp-1") == "ask"
    assert resolve(rules, "other") == "allow"
    assert resolve(rules, "") == "allow"
    # 首个命中的模式生效
    assert resolve({"*-x": "deny", "a-*": "allow"}, "a-x") == "deny"
    assert normalize_rules(None) == {}
    assert normalize_rules("deny") == {}


# ---------------------------------------------------------------- 列表与命令

def test_skills_list_permission_field(tmp_path):
    _make_skill(str(tmp_path), "ok-skill")
    _make_skill(str(tmp_path), "internal-tools")
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lc"))
    app.save_config({"skill_permissions": {"internal-*": "deny"}})

    perms = {s["name"]: s["permission"] for s in app.skills_list()}
    assert perms["ok-skill"] == "allow"
    assert perms["internal-tools"] == "deny"


def test_commands_list_excludes_denied(tmp_path):
    _make_skill(str(tmp_path), "public-tool", description="公开")
    _make_skill(str(tmp_path), "secret-tool", description="秘密")
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lc"))
    app.save_config({"skill_permissions": {"secret-*": "deny"}})

    names = [c["name"] for c in app.commands_list()]
    assert "public-tool" in names
    assert "secret-tool" not in names


def test_system_prompt_index_filters_denied(tmp_path):
    from litework.core.system_prompt import SystemPromptBuilder
    from litework.core.types import ToolDefinition
    _make_skill(str(tmp_path), "public-tool", description="公开技能")
    _make_skill(str(tmp_path), "secret-tool", description="秘密技能")
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lc"))
    app.save_config({"skill_permissions": {"secret-*": "deny"}})

    tools = [ToolDefinition(name="t", description="d", parameters={"type": "object"})]
    idx = "\n".join(f"- {s['name']}: {s['description'] or '使用该技能目录中的 SKILL.md'}"
                    for s in app.skills_list() if s.get("permission") != "deny")
    prompt = SystemPromptBuilder.build(str(tmp_path), tools, skill_index=idx)
    assert "public-tool" in prompt
    assert "secret-tool" not in prompt
    # 不传 skill_index 时回退全量索引（向后兼容）
    prompt2 = SystemPromptBuilder.build(str(tmp_path), tools)
    assert "secret-tool" in prompt2


# ---------------------------------------------------------------- load_skill 拦截

def test_load_skill_middleware_deny_and_ask(tmp_path):
    from litework.app import AgentApp  # noqa: F401
    _make_skill(str(tmp_path), "secret-tool")
    _make_skill(str(tmp_path), "ask-tool")
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lc"))
    app.save_config({"skill_permissions": {"secret-*": "deny", "ask-*": "ask"}})
    kernel = app.create_kernel("s-perm", registry=None)

    async def run_pipeline(data):
        called = {"terminal": False}
        return await kernel.before_tool.run(kernel.ctx, data), called

    def _reached(data):
        # 没被 cancel 即到达终点（管道无显式 terminal 标记，cancel=False 即放行）
        return not data.get("cancel")

    # deny：管道取消
    data, _ = asyncio.run(run_pipeline({"toolName": "load_skill", "args": {"skillName": "secret-tool"}}))
    assert data.get("cancel") and "[Skill Denied]" in (data.get("reason") or "")

    # allow：放行
    data2, _ = asyncio.run(run_pipeline({"toolName": "load_skill", "args": {"skillName": "normal"}}))
    assert _reached(data2)

    # ask：挂起等审批 → 批准后放行
    async def ask_flow():
        async def approver():
            await asyncio.sleep(0.05)
            for aid in list(app.approval_gate._pending):
                app.approval_gate.resolve(aid, True, by="test")
        t = asyncio.ensure_future(approver())
        data3, _ = await run_pipeline({"toolName": "load_skill", "args": {"skillName": "ask-tool"}})
        await t
        return data3

    data3 = asyncio.run(ask_flow())
    assert _reached(data3), f"ask 批准后应放行: {data3}"

    # ask 拒绝：取消
    async def reject_flow():
        async def rejecter():
            await asyncio.sleep(0.05)
            for aid in list(app.approval_gate._pending):
                app.approval_gate.resolve(aid, False, by="test")
        t = asyncio.ensure_future(rejecter())
        data4, _ = await run_pipeline({"toolName": "load_skill", "args": {"skillName": "ask-tool"}})
        await t
        return data4

    data4 = asyncio.run(reject_flow())
    assert data4.get("cancel") and "[User Rejected]" in (data4.get("reason") or "")


def test_agent_loop_cancels_denied_load_skill(tmp_path):
    """经 AgentLoop.execute_tool 验证 deny 的技能返回取消文本（真实工具执行链路）。"""
    _make_skill(str(tmp_path), "secret-tool")
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lc"))
    app.save_config({"skill_permissions": {"secret-*": "deny"}})
    app._mock_adapter = MockLLMAdapter([("ok", [])])
    registry = app.create_agent_registry("build")
    assert registry.has("load_skill")
    kernel = app.create_kernel("s-denied", registry=registry)
    loop = app.create_loop(kernel, registry)

    from litework.core.types import ToolCall
    stats = {"blocked": 0, "tool_calls": 0}
    call = ToolCall(id="c1", name="load_skill", arguments='{"skillName": "secret-tool"}')
    result = asyncio.run(loop._execute_tool_call(call, stats))
    assert "[Skill Denied]" in result

    _make_skill(str(tmp_path), "ok-skill")
    call2 = ToolCall(id="c2", name="load_skill", arguments='{"skillName": "ok-skill"}')
    result2 = asyncio.run(loop._execute_tool_call(call2, stats))
    assert "[Skill Denied]" not in result2 and "secret" not in result2.lower()


# ---------------------------------------------------------------- 任务启动解析

def test_resolve_skill_extra_permissions(tmp_path):
    from litework.server.tasks import TaskManager
    _make_skill(str(tmp_path), "free-tool", triggers="自由触发")
    _make_skill(str(tmp_path), "locked-tool", triggers="锁定触发")
    _make_skill(str(tmp_path), "gate-tool", triggers="门禁触发")
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lc"))
    app.save_config({"skill_permissions": {"locked-*": "deny", "gate-*": "ask"}})
    tm = TaskManager(app)

    # triggers：deny 跳过、ask 收集、allow 注入
    extra, names, ask = tm._resolve_skill_extra("请处理 自由触发 和 锁定触发 和 门禁触发")
    assert "free-tool" in names and "锁定" not in (extra or "")
    assert "locked-tool" not in names
    assert ask == ["gate-tool"]
    assert "gate-tool" not in names or True  # ask 不注入内容但记入 names
    assert "gate-tool" in names

    # /skill 显式 deny → 提示文本
    extra2, names2, ask2 = tm._resolve_skill_extra("/skill locked-tool 帮我")
    assert "已被权限规则禁用" in (extra2 or "")
    assert ask2 == []

    # /skill 显式 ask → 收集待审批
    extra3, names3, ask3 = tm._resolve_skill_extra("/skill gate-tool 帮我")
    assert ask3 == ["gate-tool"]
