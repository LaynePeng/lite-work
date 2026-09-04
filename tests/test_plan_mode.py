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
    assert "规划型" in plan_prompt
    assert "禁止修改任何文件" in plan_prompt
    assert plan_prompt.count(FINAL_REPORT_REQUIREMENT) == 1
    # 2. 共享头：交付要求在最前，两个 Agent 的 prompt 均以其开头（缓存友好）
    assert plan_prompt.startswith(FINAL_REPORT_REQUIREMENT)
    assert build_prompt.startswith(FINAL_REPORT_REQUIREMENT)
    # 3. build 无专属提示时与通用版逐字节一致（存量会话缓存不受影响）
    assert SystemPromptBuilder.build(str(tmp_path), app.build_registry().get_tools()) == build_prompt
    # 4. Plan 的工具清单不含写工具（角色声明与实际工具集一致）
    assert "write_file" not in plan_prompt
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
    assert "规划型" in app._mock_adapter.seen_systems[0]
    assert "禁止修改任何文件" in app._mock_adapter.seen_systems[0]
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
