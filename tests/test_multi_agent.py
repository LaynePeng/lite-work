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
    """子 agent 工具面：可用 send_message/list_agents（同伴通信），
    不可 followup_task（唤醒权留给编排者）。"""
    app = _make_app(tmp_path)
    from litework.orchestration.sub_agent import SUB_AGENT_EXCLUDE
    sub_registry = app.build_registry(allowed=None, exclude=SUB_AGENT_EXCLUDE)
    sub_names = {t.name for t in sub_registry.get_tools()}
    assert "send_message" in sub_names
    assert "list_agents" in sub_names
    assert "followup_task" not in sub_names
    assert "spawn_agent" not in sub_names


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
    """build agent 的 registry 含四工具；explorer 子 agent 不含（禁嵌套）。"""
    app = _make_app(tmp_path)
    registry = app.create_agent_registry("build")
    names = {t.name for t in registry.get_tools()}
    assert {"spawn_agent", "list_agents", "close_agent", "wait_agents"} <= names

    from litework.orchestration.sub_agent import SUB_AGENT_EXCLUDE
    sub_registry = app.build_registry(
        allowed=None, exclude=SUB_AGENT_EXCLUDE)
    sub_names = {t.name for t in sub_registry.get_tools()}
    assert not ({"spawn_agent", "spawn_sub_agent"} & sub_names)


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
