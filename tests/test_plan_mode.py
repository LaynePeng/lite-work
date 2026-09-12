# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""Plan 模式只读回归测试：全链路（TaskManager → Kernel 装配 → AgentLoop）不得泄漏写工具。"""
from __future__ import annotations

import os

from litework.app import AgentApp
from litework.core.system_prompt import FINAL_REPORT_REQUIREMENT, SystemPromptBuilder
from litework.server.tasks import TaskManager
from tests.conftest import MockLLMAdapter, tool_call


class RecAdapter(MockLLMAdapter):
    """记录每次发给 LLM 的工具 schema。"""

    def __init__(self, responses):
        super().__init__(responses)
        self.seen_tools: list = []
        self.seen_systems: list = []

    async def chat_stream(self, messages, tools, events=None):
        self.seen_tools.append([t.name for t in tools])
        if messages and messages[0].role == "system":
            self.seen_systems.append(messages[0].content)
        return await super().chat_stream(messages, tools, events)


WRITE_TOOLS = {"write_file", "apply_search_replace", "apply_unified_diff",
               "execute_command", "git_commit", "spawn_sub_agent"}


def test_all_agent_prompts_require_final_report(tmp_path):
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    tools = app.build_registry().get_tools()
    assert FINAL_REPORT_REQUIREMENT in SystemPromptBuilder.build(str(tmp_path), tools)


def test_plan_agent_prompt_injects_role_into_system_prompt(tmp_path):
    """Plan 的角色提示必须进入发给 LLM 的 System Prompt（模型需知道自己的工具边界）。"""
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    registry = app.create_agent_registry("plan")
    plan_prompt = SystemPromptBuilder.build(
        str(tmp_path), registry.get_tools(), agent_prompt=app.get_agent("plan").system_prompt
    )
    build_prompt = SystemPromptBuilder.build(
        str(tmp_path), app.build_registry().get_tools()
    )

    # 1. Plan 角色人格注入，且只出现一次（FINAL_REPORT 由 builder 统一前置，不重复）
    assert "技术规划师" in plan_prompt
    assert "不直接实施" in plan_prompt
    assert plan_prompt.count(FINAL_REPORT_REQUIREMENT) == 1
    # 2. 共享头：交付要求在最前，两个 Agent 的 prompt 均以其开头（缓存友好）
    assert plan_prompt.startswith(FINAL_REPORT_REQUIREMENT)
    assert build_prompt.startswith(FINAL_REPORT_REQUIREMENT)
    # 3. build 有专属提示时与通用版不同（各自独立、缓存前缀按 agent 稳定）
    build_with_prompt = SystemPromptBuilder.build(
        str(tmp_path), app.build_registry().get_tools(),
        agent_prompt=app.get_agent("build").system_prompt
    )
    assert build_with_prompt != build_prompt
    assert "资深软件工程师" in build_with_prompt
    # 4. Plan 的工具清单不含写工具（角色声明与实际工具集一致）；
    #    提示词正文允许提到 write_file 作为"你没有的"示例，但工具清单格式不能出现
    assert "- **write_file**" not in plan_prompt
    assert "- **execute_command**" not in plan_prompt   # 工具清单无此工具（规则文本中的提及不计）


def test_project_instruction_files_are_included(tmp_path):
    (tmp_path / "AGENTS.md").write_text("先运行单元测试。", encoding="utf-8")
    (tmp_path / "Claude.md").write_text("使用项目既有命名规范。", encoding="utf-8")
    prompt = SystemPromptBuilder.build(str(tmp_path), [])
    assert "### 项目指令 (Project Instructions)" in prompt
    assert "### AGENTS.md" in prompt
    assert "先运行单元测试。" in prompt
    assert "### Claude.md" in prompt
    assert "使用项目既有命名规范。" in prompt


async def test_plan_mode_never_leaks_write_tools(tmp_path):
    """plan 任务：发给 LLM 的 schema 只含只读工具，尝试写文件也不会执行。"""
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    app._mock_adapter = RecAdapter([
        ("", [tool_call("write_file", '{"filePath":"x.txt","content":"hi"}', cid="c1")]),
        ("（plan 完成）", []),
    ])
    tm = TaskManager(app)
    handle = tm.start("plan-session", "帮我规划一下", agent_id="plan")
    await handle.task

    # 1. 工具 schema 无任何写工具
    schema = handle.registry.names()
    assert WRITE_TOOLS.isdisjoint(schema), f"plan 模式泄漏写工具: {sorted(WRITE_TOOLS & set(schema))}"
    # 1.5 全链路：Plan 角色人格确实进入发给 LLM 的 System Prompt
    assert app._mock_adapter.seen_systems, "未捕获 system prompt"
    assert "技术规划师" in app._mock_adapter.seen_systems[0]
    assert "不直接实施" in app._mock_adapter.seen_systems[0]
    # 2. 写文件未发生
    assert not os.path.exists(tmp_path / "x.txt")
    # 3. 注册表执行写工具返回未注册错误（防 LLM 越权调用）
    result = await handle.registry.execute("write_file", {"filePath": "x.txt", "content": "hi"})
    assert "未注册" in result


async def test_build_mode_keeps_full_tools(tmp_path):
    """对照：build 模式仍拥有全部工具，写文件正常执行。"""
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    app._mock_adapter = RecAdapter([
        ("", [tool_call("write_file", '{"filePath":"y.txt","content":"hi"}', cid="c2")]),
        ("（build 完成）", []),
    ])
    tm = TaskManager(app)
    handle = tm.start("build-session", "帮我写文件", agent_id="build")
    await handle.task

    assert "write_file" in handle.registry.names()
    assert (tmp_path / "y.txt").exists()


async def test_create_kernel_without_registry_installs_full_tools(tmp_path):
    """create_kernel 不传 registry 时：默认全量工具内核仍可用。"""
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    kernel = app.create_kernel("bare")
    from litework.tools.registry import ToolRegistry

    registry = kernel.get_service("tools")
    assert isinstance(registry, ToolRegistry)
    assert registry.has("write_file")
    assert registry.has("webfetch")


# ---------------------------------------------------------------- Agent 角色切换检测

from litework.core.types import Message  # noqa: E402


async def test_agent_switch_build_to_plan_injects_handover(tmp_path):
    """build 历史会话切到 plan：注入角色切换提示（历史是 build 做的 + 只读边界）。"""
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    app._mock_adapter = RecAdapter([("（plan 完成）", [])])
    # 预置 build 历史快照（assistant 消息带 agent="build" 标记）
    app.session_store.save("sw-session", [
        Message(role="user", content="帮我写个文件"),
        Message(role="assistant", content="已写入 x.txt", agent="build"),
    ])
    tm = TaskManager(app)
    handle = tm.start("sw-session", "现在帮我做规划", agent_id="plan")
    await handle.task

    system = app._mock_adapter.seen_systems[0]
    assert "角色切换提示" in system
    assert "agent_id=build" in system
    assert "不是你的操作" in system
    assert "只读能力" in system          # plan 是只读型 → 只读措辞
    assert "不得执行、继续或撤销" in system


async def test_agent_switch_plan_to_build_injects_handover(tmp_path):
    """plan 历史会话切回 build：注入提示且为可写型措辞。"""
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    app._mock_adapter = RecAdapter([("（build 完成）", [])])
    app.session_store.save("sw-plan-session", [
        Message(role="user", content="帮我规划"),
        Message(role="assistant", content="计划如下…", agent="plan"),
    ])
    tm = TaskManager(app)
    handle = tm.start("sw-plan-session", "按计划执行", agent_id="build")
    await handle.task

    system = app._mock_adapter.seen_systems[0]
    assert "角色切换提示" in system
    assert "agent_id=plan" in system
    assert "可以直接落地执行" in system   # build 是可写型 → 可写措辞


async def test_agent_switch_to_office_and_custom_agent(tmp_path):
    """泛化：build→office 与 切换到自定义 agent 均触发，展示名取 profile 描述。"""
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    app._mock_adapter = RecAdapter([("（office 完成）", [])])
    app.session_store.save("sw-office-session", [
        Message(role="assistant", content="已生成周报.docx", agent="build"),
    ])
    tm = TaskManager(app)
    handle = tm.start("sw-office-session", "做个周报", agent_id="office")
    await handle.task

    system = app._mock_adapter.seen_systems[0]
    assert "角色切换提示" in system
    assert "agent_id=build" in system
    # office 是可写型（白名单含写类工具）
    assert "可以直接落地执行" in system


async def test_agent_switch_metadata_fallback(tmp_path):
    """消息无 agent 标记（旧快照）时回退 metadata.last_agent_id。"""
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    app._mock_adapter = RecAdapter([("（plan 完成）", [])])
    app.session_store.save("sw-legacy-session", [
        Message(role="assistant", content="旧版本的任务输出"),  # 无 agent 字段
    ], metadata={"last_agent_id": "build"})
    tm = TaskManager(app)
    handle = tm.start("sw-legacy-session", "做规划", agent_id="plan")
    await handle.task

    assert "角色切换提示" in app._mock_adapter.seen_systems[0]


async def test_no_switch_same_agent_no_injection(tmp_path):
    """同一 agent 连续任务：不注入；全新会话（无历史无 metadata）：不注入。"""
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    app._mock_adapter = RecAdapter([("done", [])])
    app.session_store.save("sw-same-session", [
        Message(role="assistant", content="上轮输出", agent="plan"),
    ])
    tm = TaskManager(app)
    handle = tm.start("sw-same-session", "继续", agent_id="plan")
    await handle.task
    assert "角色切换提示" not in app._mock_adapter.seen_systems[0]

    handle = tm.start("sw-brand-new", "新会话", agent_id="build")
    await handle.task
    assert "角色切换提示" not in app._mock_adapter.seen_systems[-1]


async def test_task_persists_last_agent_id(tmp_path):
    """任务启动后 last_agent_id 写入 metadata（旧快照兼容的兜底依据）。"""
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    app._mock_adapter = RecAdapter([("done", [])])
    tm = TaskManager(app)
    handle = tm.start("sw-persist-session", "干活", agent_id="office")
    await handle.task

    snapshot = app.session_store.load("sw-persist-session")
    assert snapshot.metadata.get("last_agent_id") == "office"


# ---------------------------------------------------------------- 计划文件交接

async def test_plan_file_handover_execute_on_switch(tmp_path):
    """plan 留下计划文件 → 切到可写型 agent：提示读取并执行计划。"""
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    app._mock_adapter = RecAdapter([("（build 完成）", [])])
    app.session_store.save("pf-session", [
        Message(role="assistant", content="计划已写入", agent="plan"),
    ])
    plans_dir = tmp_path / ".lite-work" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "pf-session.md").write_text("# 计划\n1. 做 A\n2. 做 B", encoding="utf-8")

    tm = TaskManager(app)
    handle = tm.start("pf-session", "开始执行", agent_id="build")
    await handle.task

    system = app._mock_adapter.seen_systems[0]
    assert "计划文件" in system
    assert "执行其中定义的计划" in system
    assert "pf-session.md" in system


async def test_plan_file_no_file_no_execute_hint(tmp_path):
    """prev=plan 但没有计划文件：正常切换提示，不出现「执行计划」文案。"""
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    app._mock_adapter = RecAdapter([("（build 完成）", [])])
    app.session_store.save("pf-empty-session", [
        Message(role="assistant", content="计划在对话里", agent="plan"),
    ])
    tm = TaskManager(app)
    handle = tm.start("pf-empty-session", "执行吧", agent_id="build")
    await handle.task

    system = app._mock_adapter.seen_systems[0]
    assert "角色切换提示" in system
    assert "执行其中定义的计划" not in system


async def test_plan_agent_gets_plan_file_guidance(tmp_path):
    """plan 任务（无切换）也注入计划文件指引：无文件 → 引导写入；有文件 → 引导增量更新。"""
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    plans_dir = tmp_path / ".lite-work" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "pf-guide-session.md").write_text("# 旧计划", encoding="utf-8")
    app._mock_adapter = RecAdapter([("（plan 完成）", [])])

    # 无文件会话：引导创建
    tm = TaskManager(app)
    handle = tm.start("pf-new-session", "帮我规划", agent_id="plan")
    await handle.task
    system = app._mock_adapter.seen_systems[0]
    assert "plan_save" in system
    assert "pf-new-session.md" in system

    # 已有计划文件的会话：引导用 plan_save 更新（整体重写语义）
    handle = tm.start("pf-guide-session", "继续规划", agent_id="plan")
    await handle.task
    system = app._mock_adapter.seen_systems[-1]
    assert "已有计划文件" in system
    assert "plan_save" in system


async def test_non_plan_agent_without_switch_no_plan_hint(tmp_path):
    """build 连续任务（无切换、非 plan）：不注入计划文件文案。"""
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    app._mock_adapter = RecAdapter([("done", [])])
    app.session_store.save("pf-build-session", [
        Message(role="assistant", content="上轮输出", agent="build"),
    ])
    tm = TaskManager(app)
    handle = tm.start("pf-build-session", "继续干活", agent_id="build")
    await handle.task

    assert "计划文件" not in app._mock_adapter.seen_systems[0]
