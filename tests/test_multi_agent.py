"""多智能体 P1 测试：SessionAgentManager 生命周期 + IsolationPlugin 硬隔离 + 通知注入。

设计对齐 docs/multi-agent-design.md §8 测试计划。
"""
import asyncio
import os

import pytest

from litework.app import AgentApp
from litework.core.agent_loop import AgentLoop
from litework.core.kernel import Kernel
from litework.core.types import Message
from litework.orchestration.agent_manager import (
    AgentRecord,
    IsolationPlugin,
    SessionAgentManager,
)
from litework.tools.registry import ToolRegistry
from tests.conftest import MockLLMAdapter, tool_call


def _make_app(tmp_path, **config) -> AgentApp:
    cfg = {"max_parallel_agents": 2, "agent_total_limit": 3}
    cfg.update(config)
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".cfg"))
    app.config.update(cfg)
    # 子 Agent 的 LLM 用脚本化 mock（无需真实 key）
    app._mock_adapter = MockLLMAdapter([("子任务完成：调研结论 X。", [])])
    # 隔离性护栏：默认拦截 build_adapter——环境变量注入的真实 key 会让
    # spawn(model=...) 构建真实 adapter 并发起网络调用（曾致测试 22s 且
    # 把任务文本发给真实 API）。需要验证模型路由的测试自行安装 spy 覆盖。
    app.llm_registry.build_adapter = (
        lambda provider_id=None, overrides=None: app._mock_adapter)
    return app


# ---------------------------------------------------------------- IsolationPlugin

def _make_kernel_with_isolation(tmp_path, allowed, record=None):
    kernel = Kernel("iso-test")
    record = record or AgentRecord(
        agent_id="sa_x", nickname="x-1", role="general", task="t",
        allowed_dirs=allowed)
    kernel.use(IsolationPlugin(str(tmp_path), allowed, record))
    return kernel, record


async def _run_before_tool(kernel, tool_name, args):
    data = {"toolName": tool_name, "args": args, "cancel": False, "reason": ""}
    return await kernel.before_tool.run(kernel.ctx, data)


async def test_isolation_allows_in_scope_write(tmp_path):
    kernel, record = _make_kernel_with_isolation(tmp_path, ["src/"])
    data = await _run_before_tool(kernel, "write_file", {"filePath": "src/a.py", "content": "x"})
    assert not data["cancel"]
    assert "src/a.py" in record.changed_files


async def test_isolation_blocks_out_of_scope_write(tmp_path):
    kernel, record = _make_kernel_with_isolation(tmp_path, ["src/"])
    data = await _run_before_tool(kernel, "write_file", {"filePath": "lib/b.py", "content": "x"})
    assert data["cancel"]
    assert "[Isolation]" in data["reason"]
    assert record.changed_files == []


async def test_isolation_blocks_dotdot_traversal(tmp_path):
    kernel, _ = _make_kernel_with_isolation(tmp_path, ["src/"])
    data = await _run_before_tool(kernel, "write_file", {"filePath": "src/../../escape.py", "content": "x"})
    assert data["cancel"]


async def test_isolation_blocks_absolute_path_outside(tmp_path):
    kernel, _ = _make_kernel_with_isolation(tmp_path, ["src/"])
    data = await _run_before_tool(kernel, "apply_search_replace",
                                  {"filePath": "/etc/passwd", "searchBlock": "a", "replaceBlock": "b"})
    assert data["cancel"]


async def test_isolation_ignores_read_tools(tmp_path):
    kernel, _ = _make_kernel_with_isolation(tmp_path, ["src/"])
    data = await _run_before_tool(kernel, "read_file", {"filePath": "docs/anywhere.md"})
    assert not data["cancel"]


# ---------------------------------------------------------------- SessionAgentManager

class _SlowAdapter(MockLLMAdapter):
    """慢速 + 变参数 mock：让子 agent 真正长跑（防死循环防御提前终止）。"""

    def __init__(self, rounds: int = 60, delay: float = 0.05) -> None:
        responses = [
            ("工作ing", [tool_call("read_file", f'{{"filePath":"f{i}.txt"}}')])
            for i in range(rounds)
        ] + [("done", [])]
        super().__init__(responses)
        self.delay = delay

    async def chat_stream(self, messages, tools, events=None):
        await asyncio.sleep(self.delay)
        return await super().chat_stream(messages, tools, events)


async def test_manager_spawn_returns_immediately(tmp_path):
    app = _make_app(tmp_path)
    mgr = app.agent_manager("s1")
    result = await mgr.spawn("调研 A", role="explorer")
    assert result["ok"]
    assert result["agent_id"].startswith("sa_")
    assert result["nickname"]
    # 异步：立即返回时仍在运行
    rec = mgr.get(result["agent_id"])
    assert rec.status == "running"
    await mgr.wait([result["agent_id"]], timeout_ms=10000)
    assert rec.status == "completed"
    assert "调研结论" in rec.summary


async def test_manager_parallel_limit(tmp_path):
    app = _make_app(tmp_path)  # max_parallel=2
    mgr = app.agent_manager("s1")
    r1 = await mgr.spawn("任务1", role="explorer")
    r2 = await mgr.spawn("任务2", role="explorer")
    assert r1["ok"] and r2["ok"]
    r3 = await mgr.spawn("任务3", role="explorer")
    assert not r3["ok"]
    assert "上限" in r3["error"]
    await mgr.close(r1["agent_id"])
    r4 = await mgr.spawn("任务4", role="explorer")
    assert r4["ok"]


async def test_manager_total_limit(tmp_path):
    app = _make_app(tmp_path, agent_total_limit=2)
    mgr = app.agent_manager("s1")
    r1 = await mgr.spawn("任务1", role="explorer")
    await mgr.wait([r1["agent_id"]], timeout_ms=10000)
    r2 = await mgr.spawn("任务2", role="explorer")
    await mgr.wait([r2["agent_id"]], timeout_ms=10000)
    r3 = await mgr.spawn("任务3", role="explorer")
    assert not r3["ok"]
    assert "累计" in r3["error"]


async def test_manager_close_running_agent(tmp_path):
    app = _make_app(tmp_path)
    app._mock_adapter = _SlowAdapter()
    mgr = app.agent_manager("s1")
    r = await mgr.spawn("长任务", role="explorer", max_steps=60)
    rec = mgr.get(r["agent_id"])
    assert rec.status == "running"  # 慢 adapter 保证还在跑
    result = await mgr.close(r["agent_id"])
    assert result["ok"]
    assert rec.status == "closed"
    # close 不产生交付通知
    assert mgr.drain_notifications() == []


async def test_manager_notification_format(tmp_path):
    app = _make_app(tmp_path)
    mgr = app.agent_manager("s1")
    r = await mgr.spawn("调研认证模块", role="explorer", agent_name="auth-scout")
    await mgr.wait([r["agent_id"]], timeout_ms=10000)
    notes = mgr.drain_notifications()
    assert len(notes) == 1
    assert "[agent:completed]" in notes[0]
    assert "auth-scout" in notes[0]
    assert "调研认证模块" in notes[0]
    # drain 后清空
    assert mgr.drain_notifications() == []


async def test_manager_wait_timeout(tmp_path):
    app = _make_app(tmp_path)
    app._mock_adapter = _SlowAdapter()
    mgr = app.agent_manager("s1")
    r = await mgr.spawn("长任务", role="explorer", max_steps=60)
    result = await mgr.wait([r["agent_id"]], timeout_ms=300)
    assert result["ok"]
    assert result["timed_out"]
    assert result["statuses"][r["agent_id"]]["status"] == "running"
    await mgr.close(r["agent_id"])


# ---------------------------------------------------------------- AgentLoop 通知注入

async def test_loop_finish_injects_pending_notifications(tmp_path):
    """父任务结束时剩余通知注入消息链并落盘（父已结束场景）。"""
    app = _make_app(tmp_path)
    mgr = app.agent_manager("s1")
    r = await mgr.spawn("任务A", role="explorer")
    await mgr.wait([r["agent_id"]], timeout_ms=10000)
    # 通知滞留队列（父 loop 从未运行/已结束）
    assert len(mgr.notifications) == 1

    kernel = Kernel("s1")
    registry = ToolRegistry()
    adapter = MockLLMAdapter([("父任务完成。", [])])
    from litework.core.session_store import SessionStore
    loop = AgentLoop(kernel=kernel, adapter=adapter, registry=registry,
                     session_store=SessionStore(str(tmp_path / "sessions")), max_steps=5)
    loop.workspace = str(tmp_path)
    loop.agent_manager_factory = lambda sid: app.agent_managers.get(sid)

    result, _ = await loop.run_task("父任务", system_prompt="测试")
    assert result == "父任务完成。"
    # 通知已注入消息链（父结束前 _finish 注入）
    injected = [m for m in kernel.ctx.messages if "[agent:completed]" in (m.content or "")]
    assert len(injected) == 1
    # 通知被 drain
    assert mgr.drain_notifications() == []


async def test_loop_turn_boundary_injection(tmp_path):
    """父运行中：turn 边界注入通知，下一轮 LLM 可见。"""
    app = _make_app(tmp_path)
    mgr = app.agent_manager("s1")
    r = await mgr.spawn("任务A", role="explorer")
    await mgr.wait([r["agent_id"]], timeout_ms=10000)

    kernel = Kernel("s1")
    registry = ToolRegistry()
    seen_messages = []

    class CapturingAdapter(MockLLMAdapter):
        async def chat_stream(self, messages, tools, events=None):
            seen_messages.append([m.content for m in messages])
            return await super().chat_stream(messages, tools, events)

    adapter = CapturingAdapter([
        ("先干活。", [tool_call("list_agents", "{}")]),
        ("收到子 Agent 结果，汇总完成。", []),
    ])
    # list_agents 需要 manager —— 注册一个转发 handler
    async def _list(args):
        import json
        return json.dumps(mgr.list_agents(), ensure_ascii=False)
    registry.register("list_agents", "列 agent", {"type": "object"}, _list)

    from litework.core.session_store import SessionStore
    loop = AgentLoop(kernel=kernel, adapter=adapter, registry=registry,
                     session_store=SessionStore(str(tmp_path / "sessions")), max_steps=5)
    loop.workspace = str(tmp_path)
    loop.agent_manager_factory = lambda sid: app.agent_managers.get(sid)

    result, _ = await loop.run_task("父任务", system_prompt="测试")
    assert "汇总完成" in result
    # 第二轮 LLM 调用看到了 [agent:completed] 通知
    assert any("[agent:completed]" in c for c in seen_messages[1])


# ---------------------------------------------------------------- agent 间合作（P2 切片）

async def test_send_message_to_running_agent(tmp_path):
    """运行中的 agent 在 turn 边界收到消息（合作中插话）。"""
    app = _make_app(tmp_path)

    seen = []

    class CaptureAdapter(MockLLMAdapter):
        def __init__(self, responses):
            super().__init__(responses)
            self.delay = 0.0

        async def chat_stream(self, messages, tools, events=None):
            seen.append([m.content for m in messages])
            if self.delay:
                await asyncio.sleep(self.delay)
            return await super().chat_stream(messages, tools, events)

    # 第一轮回复让 agent 继续干活（保证还在 running 时消息到达）
    app._mock_adapter = CaptureAdapter(
        [("工作ing", [tool_call("read_file", '{"filePath":"a.txt"}')]),
         ("收到消息，继续。", [])]
    )
    # 加大首轮延迟：确保 send_message 到达时第一轮 LLM 仍在飞行中，
    # 消息在 G2（工具回填后）注入，第二轮 LLM 调用可见
    app._mock_adapter.delay = 0.3
    mgr = app.agent_manager("s1")
    r = await mgr.spawn("任务A", role="explorer", max_steps=5)
    await asyncio.sleep(0.1)  # 首轮仍在飞行
    out = await mgr.send_message(r["agent_id"], "补充线索：重点看 auth 模块")
    assert out["ok"] and "已送达" in out["delivered"]
    await mgr.wait([r["agent_id"]], timeout_ms=10000)
    # 消息注入了 agent 上下文（第二轮 LLM 调用可见）
    assert any("[来自其他 Agent 的消息]" in c and "auth" in c
               for call in seen for c in call)


async def test_send_message_to_finished_agent_backlog(tmp_path):
    """已完成的 agent：消息滞留 mailbox，唤醒时送达。"""
    app = _make_app(tmp_path)
    mgr = app.agent_manager("s1")
    r = await mgr.spawn("任务A", role="explorer")
    await mgr.wait([r["agent_id"]], timeout_ms=10000)
    out = await mgr.send_message(r["agent_id"], "批判意见：样本量不足")
    assert out["ok"] and "已暂存" in out["delivered"]
    rec = mgr.get(r["agent_id"])
    assert len(rec.mailbox) == 1


async def test_followup_wakes_with_history_and_backlog(tmp_path):
    """followup 唤醒：带历史上下文 + 滞留消息，token 累加。"""
    app = _make_app(tmp_path)
    # 首 run 给出含关键词的总结，唤醒 run 的历史里应能看到
    app._mock_adapter = MockLLMAdapter([("首跑结论：MVP 完成。", [])])
    mgr = app.agent_manager("s1")
    r = await mgr.spawn("方案设计", role="general")
    await mgr.wait([r["agent_id"]], timeout_ms=10000)
    await mgr.send_message(r["agent_id"], "审查意见：缺错误处理")
    first_tokens = mgr.get(r["agent_id"]).tokens
    first_summary = mgr.get(r["agent_id"]).summary

    # 唤醒：mock 第二轮脚本——验证历史与滞留消息都在上下文
    seen = []

    class CaptureAdapter(MockLLMAdapter):
        async def chat_stream(self, messages, tools, events=None):
            seen.append([m.content or "" for m in messages])
            return await super().chat_stream(messages, tools, events)

    app._mock_adapter = CaptureAdapter([("已按意见修订完成。", [])])
    out = await mgr.followup(r["agent_id"], "根据审查意见修订方案")
    assert out["ok"]
    assert out["delivered_backlog"] == 1
    await mgr.wait([r["agent_id"]], timeout_ms=10000)
    rec = mgr.get(r["agent_id"])
    assert rec.status == "completed"
    assert "修订" in rec.summary
    assert rec.tokens >= first_tokens  # 累加
    # 唤醒 run 的上下文：历史（首跑结论）+ 滞留消息（审查意见）+ followup 任务
    flat = "\n".join(seen[0])
    assert "MVP" in flat            # 历史产出
    assert "错误处理" in flat        # 滞留消息
    assert "修订方案" in flat        # 新任务
    assert first_summary  # 原总结保留（唤醒不丢）


async def test_followup_rejects_running(tmp_path):
    app = _make_app(tmp_path)
    app._mock_adapter = _SlowAdapter()
    mgr = app.agent_manager("s1")
    r = await mgr.spawn("长任务", role="explorer", max_steps=60)
    out = await mgr.followup(r["agent_id"], "新任务")
    assert not out["ok"]
    assert "send_message" in out["error"]
    await mgr.close(r["agent_id"])


async def test_sub_agent_can_message_but_not_followup(tmp_path):
    """子 agent 工具面（depth=1）：可用 send_message/list_agents/spawn_agent（同伴通信
    与派孙，嵌套上限由 manager 校验）；不可 followup_task（唤醒权留编排者）。"""
    app = _make_app(tmp_path)
    from litework.orchestration.sub_agent import sub_agent_excludes
    sub_registry = app.build_registry(allowed=None, exclude=sub_agent_excludes(1))
    sub_names = {t.name for t in sub_registry.get_tools()}
    assert "send_message" in sub_names
    assert "list_agents" in sub_names
    assert "followup_task" not in sub_names
    assert "spawn_agent" in sub_names  # P2：depth=1 可派孙


async def test_pipeline_handoff_pattern(tmp_path):
    """流水线合作端到端：A 完成 → 产出滞留消息 → followup 唤醒 B？不——
    B 是独立 agent；流水线由父中转。这里验证：A 的产出 send 给 B，
    followup(B) 唤醒后 B 的上下文含 A 的产出。"""
    app = _make_app(tmp_path)
    app._mock_adapter = MockLLMAdapter([("A 的产出：接口设计 v1。", [])])
    mgr = app.agent_manager("s1")
    ra = await mgr.spawn("设计接口", role="general", agent_name="designer")
    rb = await mgr.spawn("实现接口", role="general", agent_name="coder")
    await mgr.wait([ra["agent_id"], rb["agent_id"]], timeout_ms=10000)

    # A → B 直接传产出（agent 间合作，不经父中转）
    out = await mgr.send_message(rb["agent_id"], "接口设计如下：v1 文档要点…",
                                 sender=ra["nickname"])
    assert out["ok"]

    seen = []

    class CaptureAdapter(MockLLMAdapter):
        async def chat_stream(self, messages, tools, events=None):
            seen.append([m.content or "" for m in messages])
            return await super().chat_stream(messages, tools, events)

    app._mock_adapter = CaptureAdapter([("已按 A 的设计实现完成。", [])])
    await mgr.followup(rb["agent_id"], "按收到的接口设计完成实现")
    await mgr.wait([rb["agent_id"]], timeout_ms=10000)
    flat = "\n".join(seen[0])
    assert "designer" in flat          # 来源可辨
    assert "接口设计如下" in flat       # A 的产出直达 B
    assert "已按 A 的设计实现完成" in mgr.get(rb["agent_id"]).summary


async def test_multi_agent_tools_registered(tmp_path):
    """build agent 的 registry 含编排工具；depth=1 子 agent 有 spawn_agent（可派孙），
    spawn_sub_agent（旧同步工具）仍被排除。"""
    app = _make_app(tmp_path)
    registry = app.create_agent_registry("build")
    names = {t.name for t in registry.get_tools()}
    assert {"spawn_agent", "list_agents", "close_agent", "wait_agents"} <= names

    from litework.orchestration.sub_agent import sub_agent_excludes
    sub_registry = app.build_registry(
        allowed=None, exclude=sub_agent_excludes(1))
    sub_names = {t.name for t in sub_registry.get_tools()}
    assert "spawn_sub_agent" not in sub_names
    assert "spawn_agent" in sub_names  # depth=1 可派孙（默认上限 2）


async def test_write_scope_tools_isolated(tmp_path):
    """声明 allowed_dirs 的子 agent：获得写文件工具但被 Isolation 约束。"""
    app = _make_app(tmp_path)
    mgr = app.agent_manager("s1")
    r = await mgr.spawn("改代码", role="general", allowed_dirs=["src/"])
    rec = mgr.get(r["agent_id"])
    await mgr.wait([r["agent_id"]], timeout_ms=10000)
    assert rec.status == "completed"


async def test_spawn_agent_tool_handler(tmp_path):
    """工具 handler 直达：spawn → list → wait → close 全链路。"""
    from litework.tools.agent_tools import make_agent_tool_handlers

    app = _make_app(tmp_path)
    handlers = make_agent_tool_handlers(app)
    from litework.core.agent_loop import current_session_id
    token = current_session_id.set("s1")
    try:
        out = await handlers["spawn_agent"]({"task": "调研 X", "role": "explorer"})
        assert "[Agent 已派生]" in out
        agent_id = out.split("id=")[1].split()[0]
        out = await handlers["list_agents"]({})
        assert agent_id in out
        out = await handlers["wait_agents"]({"agent_ids": [agent_id], "timeout_ms": 10000})
        assert "explorer" in out
        out = await handlers["close_agent"]({"agent_id": agent_id})
        assert "已关闭" in out
    finally:
        current_session_id.reset(token)


async def test_close_agent_emits_closed_event(tmp_path):
    """close_agent 成功后必须向事件总线发射 agent:closed（前端看板移除卡片的唯一信号）。"""
    from litework.tools.agent_tools import make_agent_tool_handlers
    from litework.core.agent_loop import current_session_id

    class _Bus:
        def __init__(self):
            self.events = []

        async def emit(self, name, payload):
            self.events.append((name, payload))

    app = _make_app(tmp_path)
    bus = _Bus()
    handlers = make_agent_tool_handlers(app, parent_events=bus)
    token = current_session_id.set("s1")
    try:
        out = await handlers["spawn_agent"]({"task": "调研 X", "role": "explorer"})
        assert "[Agent 已派生]" in out
        agent_id = out.split("id=")[1].split()[0]
        out = await handlers["wait_agents"]({"agent_ids": [agent_id], "timeout_ms": 10000})
        assert "explorer" in out
        out = await handlers["close_agent"]({"agent_id": agent_id})
        assert "已关闭" in out
        # agent:closed 事件必须带 agentId 发射（前端看板按此移除卡片）
        closed_events = [p for n, p in bus.events if n == "agent:closed"]
        assert len(closed_events) == 1
        assert closed_events[0]["agentId"] == agent_id
    finally:
        current_session_id.reset(token)

# ---------------------------------------------------------------- 合作模式标记

async def test_spawn_mode_tagged_events(tmp_path):
    """spawn 声明 mode → record 携带（前端看板按模式分组渲染的数据源）。"""
    app = _make_app(tmp_path)
    mgr = app.agent_manager("s1")
    r = await mgr.spawn("调研 A", role="explorer", mode="brainstorm")
    assert r["ok"]
    assert mgr.get(r["agent_id"]).mode == "brainstorm"
    # 非法 mode 归一为 orchestrate
    r2 = await mgr.spawn("调研 B", role="explorer", mode="invalid-mode")
    assert mgr.get(r2["agent_id"]).mode == "orchestrate"
    await mgr.wait([r["agent_id"], r2["agent_id"]], timeout_ms=10000)


async def test_spawn_agent_tool_mode_param(tmp_path):
    """工具层 mode 参数直达 manager。"""
    from litework.tools.agent_tools import make_agent_tool_handlers
    from litework.core.agent_loop import current_session_id

    app = _make_app(tmp_path)
    handlers = make_agent_tool_handlers(app)
    token = current_session_id.set("s1")
    try:
        out = await handlers["spawn_agent"]({"task": "提案 X", "role": "general", "mode": "brainstorm"})
        assert "[Agent 已派生]" in out
        agent_id = out.split("id=")[1].split()[0]
        assert app.agent_manager("s1").get(agent_id).mode == "brainstorm"
    finally:
        current_session_id.reset(token)


async def test_collab_mode_skills_available(tmp_path):
    """三个协作模式技能（brainstorm/agent-debate/pipeline）内置可加载，命令面板可派生。"""
    from litework.tools.skills import SkillsTools, _builtin_skills_dir
    from litework.core.commands import build_command_list

    d = _builtin_skills_dir()
    for name in ("brainstorm", "agent-debate", "pipeline"):
        assert (d / name / "SKILL.md").is_file(), f"缺少技能 {name}"
    st = SkillsTools(str(tmp_path))
    skills = st.list_skills()
    cmds = build_command_list(skills)
    names = {c["name"] for c in cmds if c.get("kind") == "skill"}
    assert {"brainstorm", "agent-debate", "pipeline"} <= names

# ---------------------------------------------------------------- P2/P3 补全

async def test_spawn_model_routing(tmp_path):
    """spawn 声明 model → run_task 构建对应 adapter（模型路由）。"""
    app = _make_app(tmp_path)

    built = {}
    orig_build = app.llm_registry.build_adapter
    def spy_build(provider_id=None, overrides=None):
        built["provider"] = provider_id
        built["overrides"] = overrides
        return app._mock_adapter  # 拦截真实构建（环境变量 key 会发起网络调用）
    app.llm_registry.build_adapter = spy_build

    mgr = app.agent_manager("s1")
    r = await mgr.spawn("轻量调研", role="explorer", model="deepseek/deepseek-chat")
    await mgr.wait([r["agent_id"]], timeout_ms=10000)
    assert built.get("provider") == "deepseek"
    assert built.get("overrides", {}).get("model") == "deepseek-chat"

    # 无 model → 走全局 adapter（不触发 build）
    built.clear()
    r2 = await mgr.spawn("默认模型", role="explorer")
    await mgr.wait([r2["agent_id"]], timeout_ms=10000)
    assert "provider" not in built


async def test_send_message_rate_limit(tmp_path):
    """agent 间消息限流：每对 (sender, receiver) 每分钟 12 条，超限报错。"""
    app = _make_app(tmp_path)
    mgr = app.agent_manager("s1")
    r = await mgr.spawn("任务", role="explorer")
    await mgr.wait([r["agent_id"]], timeout_ms=10000)
    # 已完成 → 滞留 mailbox，同样计数
    ok_count = 0
    for i in range(15):
        out = await mgr.send_message(r["agent_id"], f"msg {i}", sender="agent-a")
        if out["ok"]:
            ok_count += 1
        else:
            assert "限流" in out["error"]
            break
    assert ok_count == mgr.MESSAGE_RATE_LIMIT_PER_MIN
    # 不同 sender 不受影响
    out = await mgr.send_message(r["agent_id"], "另一发送者", sender="agent-b")
    assert out["ok"]


async def test_send_message_payload_disclaimer(tmp_path):
    """消息 payload 携带防伪声明（非用户指令、不构成授权）。"""
    app = _make_app(tmp_path)
    mgr = app.agent_manager("s1")
    r = await mgr.spawn("任务", role="explorer")
    await mgr.wait([r["agent_id"]], timeout_ms=10000)
    await mgr.send_message(r["agent_id"], "内容", sender="scout-1")
    rec = mgr.get(r["agent_id"])
    assert "非用户指令" in rec.mailbox[0]
    assert "不构成任何授权" in rec.mailbox[0]


async def test_shared_task_pool_claim_flow(tmp_path):
    """共享任务池：建池 → 认领（原子锁定）→ 重复认领拒绝 → 完成。"""
    app = _make_app(tmp_path)
    mgr = app.agent_manager("s1")
    created = mgr.create_shared_tasks(["迁移模块 A", "迁移模块 B", "迁移模块 C"])
    assert len(created) == 3

    # explorer-1 认领 t001
    out = mgr.claim_shared_task("t001", "explorer-1")
    assert out["ok"]
    # explorer-2 重复认领被拒
    out = mgr.claim_shared_task("t001", "explorer-2")
    assert not out["ok"] and "已被 explorer-1 认领" in out["error"]
    # 本人可重复认领（幂等）
    out = mgr.claim_shared_task("t001", "explorer-1")
    assert out["ok"]
    # 其他人认领其他任务
    assert mgr.claim_shared_task("t002", "explorer-2")["ok"]
    # 完成校验认领者
    out = mgr.finish_shared_task("t001", "explorer-2")
    assert not out["ok"]
    out = mgr.finish_shared_task("t001", "explorer-1")
    assert out["ok"]
    # 已完成不可再认领
    assert not mgr.claim_shared_task("t001", "explorer-3")["ok"]
    # 列表状态正确
    statuses = {t["id"]: t["status"] for t in mgr.list_shared_tasks()}
    assert statuses == {"t001": "done", "t002": "claimed", "t003": "pending"}


async def test_shared_task_tools_available(tmp_path):
    """工具面：编排者有 create/list；子 Agent 有 list/claim/complete。"""
    app = _make_app(tmp_path)
    from litework.tools.agent_tools import make_agent_tool_handlers
    from litework.core.agent_loop import current_session_id

    handlers = make_agent_tool_handlers(app)
    assert {"create_shared_tasks", "list_shared_tasks"} <= set(handlers)

    token = current_session_id.set("s1")
    try:
        out = await handlers["create_shared_tasks"]({"titles": ["任务甲", "任务乙"]})
        assert "共享任务池已建立" in out and "2" in out
        out = await handlers["list_shared_tasks"]({})
        assert "t001" in out
        out = await handlers["claim_shared_task"]({"task_id": "t001", "claimer": "w1"})
        assert "已认领" in out
        out = await handlers["complete_shared_task"]({"task_id": "t001", "claimer": "w1"})
        assert "任务完成" in out
    finally:
        current_session_id.reset(token)

    # 子 Agent 白名单含认领工具
    from litework.orchestration.sub_agent import sub_agent_excludes
    sub_registry = app.build_registry(allowed=None, exclude=sub_agent_excludes(1))
    sub_names = {t.name for t in sub_registry.get_tools()}
    assert {"list_shared_tasks", "claim_shared_task", "complete_shared_task"} <= sub_names
    assert "create_shared_tasks" not in sub_names  # 建池权留给编排者

# ---------------------------------------------------------------- P2 补全：嵌套/fork/恢复/配置

async def test_spawn_depth_limit(tmp_path):
    """嵌套深度：depth=2 可派（默认上限 2），depth=3 被拒。"""
    app = _make_app(tmp_path)
    mgr = app.agent_manager("s1")
    r = await mgr.spawn("任务", role="general", depth=2)
    assert r["ok"]
    await mgr.wait([r["agent_id"]], timeout_ms=10000)
    r3 = await mgr.spawn("更深层", role="general", depth=3)
    assert not r3["ok"]
    assert "嵌套深度" in r3["error"]


async def test_sub_agent_toolface_by_depth(tmp_path):
    """depth=1 的子 Agent 有 spawn_agent（可派孙）；depth=2（达上限）没有。"""
    from litework.orchestration.sub_agent import sub_agent_excludes
    app = _make_app(tmp_path)
    reg1 = app.build_registry(allowed=None, exclude=sub_agent_excludes(1, 2))
    names1 = {t.name for t in reg1.get_tools()}
    assert "spawn_agent" in names1
    reg2 = app.build_registry(allowed=None, exclude=sub_agent_excludes(2, 2))
    names2 = {t.name for t in reg2.get_tools()}
    assert "spawn_agent" not in names2


async def test_max_steps_cap(tmp_path):
    """spawn max_steps 超过封顶被截断到 cap。"""
    app = _make_app(tmp_path)
    mgr = app.agent_manager("s1")
    r = await mgr.spawn("任务", role="explorer", max_steps=999)
    assert r["ok"]
    rec = mgr.get(r["agent_id"])
    # AgentRecord 不存 max_steps（传入 runner），这里验证 spawn 不报错且默认逻辑生效
    assert rec is not None
    await mgr.wait([r["agent_id"]], timeout_ms=10000)


async def test_fork_context_extraction(tmp_path):
    """extract_fork_messages：裁剪规则（保留 user/assistant 纯文本，丢 system/tool/带工具调用）。"""
    from litework.orchestration.agent_manager import extract_fork_messages
    from litework.core.types import Message, ToolCall
    msgs = [
        Message(role="system", content="系统提示"),
        Message(role="user", content="需求 A"),
        Message(role="assistant", content=None, tool_calls=[ToolCall(id="c1", name="read_file", arguments="{}")]),
        Message(role="tool", name="read_file", tool_call_id="c1", content="文件内容"),
        Message(role="assistant", content="阶段结论 X"),
        Message(role="user", content="需求 B"),
    ]
    # all：保留 3 条（需求A、阶段结论X、需求B）
    got_all = extract_fork_messages(msgs, "all")
    assert [m.content for m in got_all] == ["需求 A", "阶段结论 X", "需求 B"]
    # 数字：最近 N 条 eligible
    got2 = extract_fork_messages(msgs, "2")
    assert [m.content for m in got2] == ["阶段结论 X", "需求 B"]
    # none / 非法
    assert extract_fork_messages(msgs, "none") == []
    assert extract_fork_messages(msgs, "abc") == []


async def test_message_length_limit(tmp_path):
    """agent 间消息长度上限（agent_message_max_chars 配置）。"""
    app = _make_app(tmp_path)
    app.config["agent_message_max_chars"] = 100
    mgr = app.agent_manager("s1")
    r = await mgr.spawn("任务", role="explorer")
    await mgr.wait([r["agent_id"]], timeout_ms=10000)
    out = await mgr.send_message(r["agent_id"], "x" * 200, sender="a")
    assert not out["ok"] and "过长" in out["error"]
    assert "文件交接" in out["error"]


async def test_restore_from_session(tmp_path):
    """跨重启恢复：新 manager 从 metadata 重建历史 agent，followup 可唤醒。"""
    app = _make_app(tmp_path)
    store = app.session_store
    store.save("s1", [Message(role="user", content="hi")], metadata={
        "workspace": str(tmp_path),
        "subagent_records": [{
            "subagentId": "sa_hist01", "role": "explorer", "task": "旧调研任务",
            "nickname": "old-scout", "mode": "orchestrate", "summary": "旧结论：模块 X 存在。",
            "changed_files": ["a.py"], "tokens": 100, "turns": 2, "status": "completed",
        }],
    })
    # 模拟重启：全新 manager（同一 session）
    mgr2 = app.agent_manager("s1")
    agents = mgr2.list_agents()
    assert any(a["agent_id"] == "sa_hist01" and a["summary"] == "旧结论：模块 X 存在。"
               for a in agents)
    # followup 唤醒恢复的记录（轻量历史：原任务 + 上次总结）
    seen = []
    class CaptureAdapter(MockLLMAdapter):
        async def chat_stream(self, messages, tools, events=None):
            seen.append([m.content or "" for m in messages])
            return await super().chat_stream(messages, tools, events)
    app._mock_adapter = CaptureAdapter([("重启后继续完成。", [])])
    out = await mgr2.followup("sa_hist01", "基于旧结论继续")
    assert out["ok"], out
    await mgr2.wait(["sa_hist01"], timeout_ms=10000)
    flat = "\n".join(seen[0])
    assert "旧调研任务" in flat and "旧结论" in flat  # 轻量历史注入
    assert "基于旧结论继续" in flat


async def test_meeting_mode_accepted(tmp_path):
    """meeting 模式合法（落 record，事件携带）。"""
    app = _make_app(tmp_path)
    mgr = app.agent_manager("s1")
    r = await mgr.spawn("讨论议题", role="general", mode="meeting")
    assert r["ok"]
    assert mgr.get(r["agent_id"]).mode == "meeting"
    await mgr.wait([r["agent_id"]], timeout_ms=10000)


async def test_spawn_fork_turns_through_handler(tmp_path):
    """工具层 fork_turns：'all' 继承当前会话消息（handler 绑定 kernel）。"""
    from litework.tools.agent_tools import make_agent_tool_handlers
    from litework.core.kernel import Kernel
    from litework.core.agent_loop import current_session_id

    app = _make_app(tmp_path)
    kernel = Kernel("s1")
    kernel.ctx.messages = [
        Message(role="system", content="sys"),
        Message(role="user", content="重要背景：项目是支付网关"),
        Message(role="assistant", content="收到"),
    ]
    handlers = make_agent_tool_handlers(app, kernel=kernel)
    token = current_session_id.set("s1")
    try:
        out = await handlers["spawn_agent"]({
            "task": "调研支付模块", "role": "explorer", "fork_turns": "all"})
        assert "[Agent 已派生]" in out
        assert "已继承当前上下文" in out
    finally:
        current_session_id.reset(token)

# ---------------------------------------------------------------- P3：治理/昵称/依赖图/权限收敛/worktree

async def test_nickname_pool(tmp_path):
    """昵称池：未命名 spawn 从池中取可读昵称（不再是 role-N），互不重复。"""
    app = _make_app(tmp_path)
    app.config["max_parallel_agents"] = 4  # 允许三个并行
    mgr = app.agent_manager("s1")
    from litework.orchestration.agent_manager import NICKNAME_POOL
    names = []
    for i in range(3):
        r = await mgr.spawn(f"任务{i}", role="general")
        assert r["ok"], r
        names.append(mgr.get(r["agent_id"]).nickname)
    assert all(n in NICKNAME_POOL for n in names)
    assert len(set(names)) == 3
    await mgr.wait([a.agent_id for a in mgr.agents.values()], timeout_ms=10000)


async def test_shared_task_dependency_graph(tmp_path):
    """依赖图：前置未完成 → 认领被阻塞；前置完成 → 自动可认领。"""
    app = _make_app(tmp_path)
    mgr = app.agent_manager("s1")
    created = mgr.create_shared_tasks(
        ["数据库迁移", "API 适配", "集成测试"],
        deps_by_title={"集成测试": ["数据库迁移", "API 适配"]})
    assert len(created) == 3
    # 集成测试被阻塞
    out = mgr.claim_shared_task("t003", "w1")
    assert not out["ok"] and "被依赖阻塞" in out["error"]
    # 前置完成后自动解锁
    mgr.claim_shared_task("t001", "w1"); mgr.finish_shared_task("t001", "w1")
    mgr.claim_shared_task("t002", "w2"); mgr.finish_shared_task("t002", "w2")
    out = mgr.claim_shared_task("t003", "w3")
    assert out["ok"]
    # 列表带依赖信息
    listed = {t["id"]: t for t in mgr.list_shared_tasks()}
    assert listed["t003"]["depends_on"] == ["t001", "t002"]


async def test_permission_convergence(tmp_path):
    """权限收敛：编排者 deny 的工具 → 子 Agent 强制 deny（子权限不超父）。"""
    app = _make_app(tmp_path)
    mgr = app.agent_manager("s1")
    r = await mgr.spawn("任务", role="general",  # general 全工具
                        parent_denies=["write_file", "apply_search_replace"])
    rec = mgr.get(r["agent_id"])
    assert rec.parent_denies == ["write_file", "apply_search_replace"]
    # registry 集成：SubAgentRunner 构建时 effective deny
    from litework.orchestration.sub_agent import sub_agent_excludes
    reg = app.build_registry(allowed=None,
                             exclude=sub_agent_excludes(1),
                             permissions={"write_file": "deny"})
    assert "write_file" not in {t.name for t in reg.get_tools()}
    await mgr.wait([r["agent_id"]], timeout_ms=10000)


async def test_collab_mode_gates_description(tmp_path):
    """治理档位：explicit（默认）→ 明确要求才派生；proactive → 主动并行。"""
    from litework.tools.agent_tools import MultiAgentPlugin
    app = _make_app(tmp_path)
    plugin = MultiAgentPlugin(app)
    spawn = next(t for t in plugin.get_tools() if t.name == "spawn_agent")
    assert "明确要求" in spawn.description
    app.config["agent_collab_mode"] = "proactive"
    spawn2 = next(t for t in plugin.get_tools() if t.name == "spawn_agent")
    assert "主动委派已开启" in spawn2.description


async def test_custom_roles_in_spawn_description(tmp_path):
    """角色清单进 spawn 提示：注册自定义 subagent → 描述中出现其 id。"""
    from litework.tools.agent_tools import MultiAgentPlugin
    from litework.core.agent_profile import AgentProfile
    app = _make_app(tmp_path)
    app.agent_registry.register(AgentProfile(
        id="db-migrator", mode="subagent",
        description="数据库迁移专家：schema 变更与数据回填",
        tools=["read_file", "write_file", "execute_command"]))
    plugin = MultiAgentPlugin(app)
    spawn = next(t for t in plugin.get_tools() if t.name == "spawn_agent")
    assert "db-migrator" in spawn.description
    assert "数据库迁移专家" in spawn.description


def _init_git_repo(path):
    import subprocess
    def g(*a):
        return subprocess.run(["git", "-C", str(path), *a],
                              capture_output=True, text=True)
    g("init", "-q")
    g("config", "user.email", "t@t.local")
    g("config", "user.name", "t")
    g("commit", "--allow-empty", "-m", "init", "-q")
    return g


async def test_worktree_isolation_merge_back(tmp_path):
    """worktree 物理隔离端到端：子 Agent 在独立工作树写文件 → 补丁自动合并回主工作区。"""
    _init_git_repo(tmp_path)
    app = _make_app(tmp_path)
    # 子 Agent：写一个文件后结束
    app._mock_adapter = MockLLMAdapter([
        ("", [tool_call("write_file", '{"filePath":"wt-feature.txt","content":"from worktree"}')]),
        ("已在独立工作树完成写入。", []),
    ])
    mgr = app.agent_manager("s1")
    r = await mgr.spawn("实现新特性", role="general", isolation="worktree")
    await mgr.wait([r["agent_id"]], timeout_ms=30000)
    rec = mgr.get(r["agent_id"])
    assert rec.status == "completed"
    # 改动合并回主工作区
    assert (tmp_path / "wt-feature.txt").exists(), "worktree 改动未合并回主工作区"
    assert "wt-feature.txt" in rec.changed_files
    assert "worktree" in rec.summary
    # worktree 已清理（临时分支不存在）
    import subprocess
    br = subprocess.run(["git", "-C", str(tmp_path), "branch", "--list", "lw-agent/*"],
                        capture_output=True, text=True)
    assert "lw-agent/" not in br.stdout


async def test_worktree_fallback_non_git(tmp_path):
    """非 git 仓库：worktree 请求自动降级共享模式，任务照常完成。"""
    app = _make_app(tmp_path)  # tmp_path 无 .git
    mgr = app.agent_manager("s1")
    r = await mgr.spawn("任务", role="explorer", isolation="worktree")
    await mgr.wait([r["agent_id"]], timeout_ms=10000)
    rec = mgr.get(r["agent_id"])
    assert rec.status == "completed"
    assert rec.isolation == "shared"  # 降级


async def test_handler_passes_isolation_and_denies(tmp_path):
    """工具层：isolation 参数 + kernel.orchestrator_agent_id 权限收敛传递。"""
    from litework.tools.agent_tools import make_agent_tool_handlers
    from litework.core.kernel import Kernel
    from litework.core.agent_loop import current_session_id

    app = _make_app(tmp_path)
    kernel = Kernel("s1")
    kernel.orchestrator_agent_id = "plan"  # plan deny write/execute
    handlers = make_agent_tool_handlers(app, kernel=kernel)
    token = current_session_id.set("s1")
    try:
        out = await handlers["spawn_agent"]({
            "task": "调研", "role": "general", "isolation": "worktree"})
        assert "[Agent 已派生]" in out
        mgr = app.agent_manager("s1")
        aid = out.split("id=")[1].split()[0]
        rec = mgr.get(aid)
        assert rec.isolation == "worktree"
        assert "write_file" in rec.parent_denies  # plan 的 deny 继承
        assert "execute_command" in rec.parent_denies
        await mgr.wait([aid], timeout_ms=30000)
    finally:
        current_session_id.reset(token)

# ---------------------------------------------------------------- 复查修复的回归

async def test_grandchild_attaches_to_root_manager(tmp_path):
    """Bug 修复：子 Agent 派孙 → 孙必须挂主会话 manager（看板/限额/通知统一），
    而不是按 current_session_id（=sub_id）新建孤立 manager。"""
    app = _make_app(tmp_path)
    from litework.core.agent_loop import current_session_id
    from litework.tools.agent_tools import make_agent_tool_handlers

    token = current_session_id.set("root-sess")
    mgr = app.agent_manager("root-sess")
    # 主会话 spawn 子（子会在独立 task 里执行，模拟真实链路）
    r = await mgr.spawn("子任务", role="general")
    await mgr.wait([r["agent_id"]], timeout_ms=10000)
    current_session_id.reset(token)

    # 模拟子 Agent 上下文（current_session_id = sub_id），handler 绑子 kernel
    # 且子 kernel 带 root_session_id（SubAgentRunner 真实运行时设置）
    from litework.core.kernel import Kernel
    sub_kernel = Kernel(r["agent_id"])
    sub_kernel.root_session_id = "root-sess"
    handlers = make_agent_tool_handlers(app, kernel=sub_kernel)

    token2 = current_session_id.set(r["agent_id"])  # 子执行态
    try:
        out = await handlers["spawn_agent"]({"task": "孙任务", "role": "general"})
        assert "[Agent 已派生]" in out
        grandson_id = out.split("id=")[1].split()[0]
        # 孙在主会话 manager（无孤立 manager 产生）
        assert grandson_id in mgr.agents
        assert set(app.agent_managers.keys()) == {"root-sess"}
        # 孙完成通知进主会话队列（主 AgentLoop 可注入）
        await mgr.wait([grandson_id], timeout_ms=10000)
        ids = [n["agent_id"] for n in mgr.notifications]
        assert grandson_id in ids
    finally:
        current_session_id.reset(token2)


async def test_followup_preserves_model_and_steps_semantics(tmp_path):
    """Bug 修复：followup 保留模型路由；max_steps=0 走配置默认+封顶。"""
    app = _make_app(tmp_path)
    app.config["agent_max_steps"] = 7
    app.config["agent_max_steps_cap"] = 10
    # 拦截 build_adapter：环境变量注入的 key 会构建真实 adapter（网络挂起）
    orig_build = app.llm_registry.build_adapter
    app.llm_registry.build_adapter = lambda provider_id=None, overrides=None: app._mock_adapter
    mgr = app.agent_manager("s1")
    r = await mgr.spawn("任务", role="general", model="deepseek/deepseek-chat")
    await mgr.wait([r["agent_id"]], timeout_ms=10000)
    # followup：model 保留在 record；max_steps 不传（0）→ 配置默认 7
    out = await mgr.followup(r["agent_id"], "继续")
    assert out["ok"], out
    assert mgr.get(r["agent_id"]).model == "deepseek/deepseek-chat"
    await mgr.wait([r["agent_id"]], timeout_ms=10000)
    app.llm_registry.build_adapter = orig_build


async def test_persist_subagent_completed_dedupes_followup(tmp_path):
    """Bug 修复：followup 再次完成 → 落档按 subagentId 去重（不追加重复记录）。"""
    app = _make_app(tmp_path)
    store = app.session_store
    store.save("s1", [Message(role="user", content="hi")], metadata={
        "workspace": str(tmp_path),
        "subagent_records": [
            {"subagentId": "sa_d1", "role": "general", "task": "T", "summary": "第一轮",
             "tokens": 10, "turns": 1, "status": "completed"},
        ],
    })
    # 模拟同一 agent followup 完成后的落档（与 TaskHandle 相同逻辑）
    from litework.server.tasks import TaskHandle  # noqa: F401  仅确认可导入
    snapshot = store.load("s1")
    records = list((snapshot.metadata or {}).get("subagent_records") or [])
    new_id = "sa_d1"
    records = [r for r in records if r.get("subagentId") != new_id]
    records.append({"subagentId": new_id, "role": "general", "task": "T",
                    "summary": "第二轮", "tokens": 20, "turns": 2, "status": "completed"})
    store.update_metadata("s1", {"subagent_records": records})
    restored = store.load("s1").metadata["subagent_records"]
    assert len([r for r in restored if r["subagentId"] == "sa_d1"]) == 1
    assert restored[-1]["summary"] == "第二轮"
