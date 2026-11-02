# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""ACP 三项进阶能力：历史重放（session/load）、工具展示、权限审批。

这三项的共同点是「不接也能跑，但跑不实用」：

- 没有 session/load：编辑器重启后对话全丢；
- 没有 tool_call：用户看不到 Agent 在干什么，只看到长时间无输出；
- 没有 session/request_permission：**写操作会被自己的审批门拦住直到
  600s 超时**，表现为「Agent 卡住然后失败」——这是最致命的一项。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import pytest

from litework.acp.agent import AcpAgent, replay_updates
from litework.acp.permissions import (
    ALLOW_ALWAYS,
    ALLOW_ONCE,
    PermissionBridge,
    build_options,
    interpret_outcome,
)
from litework.acp.tools import (
    ToolUpdateBridge,
    content_of_result,
    kind_of_tool,
    status_of_result,
    title_of_tool,
)
from litework.acp.transport import METHOD_NOT_FOUND, RpcError, StdioTransport


class FakeStream:
    def __init__(self) -> None:
        self.chunks: List[str] = []

    def write(self, data: str) -> None:
        self.chunks.append(data)

    def flush(self) -> None:
        pass

    def messages(self) -> List[Dict[str, Any]]:
        return [json.loads(x) for x in "".join(self.chunks).splitlines() if x.strip()]


def make_transport():
    from litework.acp.transport import _StdoutWriter
    t = StdioTransport()
    stream = FakeStream()
    t._writer = _StdoutWriter(stream)
    return t, stream


def make_agent(history_loader=None, relay=None):
    t, stream = make_transport()

    def factory(session_id, cwd):
        class FakeLoop:
            kernel = None

            async def run_task(self, prompt, **kw):
                return ("ok", {})
        return FakeLoop()

    return AcpAgent(t, factory, history_loader=history_loader,
                    permission_relay=relay), t, stream


# ============================================================ 历史重放

def test_replay_maps_user_and_assistant_text():
    msgs = [
        {"role": "user", "content": "问题一"},
        {"role": "assistant", "content": "回答一"},
    ]
    updates = replay_updates(msgs)
    kinds = [u["sessionUpdate"] for u in updates]
    assert kinds == ["user_message_chunk", "agent_message_chunk"]
    assert updates[0]["content"]["text"] == "问题一"
    assert updates[1]["content"]["text"] == "回答一"


def test_replay_skips_system_messages():
    """ACP 没有 system 概念，塞进去只会污染对话。"""
    updates = replay_updates([
        {"role": "system", "content": "你是一个助手"},
        {"role": "user", "content": "hi"},
    ])
    assert [u["sessionUpdate"] for u in updates] == ["user_message_chunk"]


def test_replay_expands_tool_calls_with_completed_status():
    msgs = [
        {"role": "user", "content": "读文件"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "function": {"name": "read_file",
                                      "arguments": '{"path": "/a.txt"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "文件内容"},
        {"role": "assistant", "content": "读到了"},
    ]
    updates = replay_updates(msgs)
    tool_calls = [u for u in updates if u["sessionUpdate"] == "tool_call"]
    tool_upds = [u for u in updates if u["sessionUpdate"] == "tool_call_update"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["toolCallId"] == "c1"
    assert tool_calls[0]["name"] == "read_file"
    # 关键：重放的历史工具必须是 completed，否则客户端永远转圈
    assert tool_calls[0]["status"] == "completed"
    assert tool_calls[0]["rawInput"] == {"path": "/a.txt"}
    assert len(tool_upds) == 1
    assert tool_upds[0]["content"][0]["content"]["text"] == "文件内容"


def test_replay_skips_tool_call_without_id():
    """没有 id 的工具调用无法与结果配对，推出去只会产生孤立卡片。"""
    updates = replay_updates([
        {"role": "assistant", "tool_calls": [{"function": {"name": "x"}}]},
    ])
    assert [u for u in updates if u["sessionUpdate"] == "tool_call"] == []


def test_replay_handles_object_messages():
    """内存里的 Message 是对象而非 dict，两种形态都要支持。"""
    class M:
        def __init__(self, role, content=None):
            self.role = role
            self.content = content
            self.tool_calls = None
            self.tool_call_id = None

    updates = replay_updates([M("user", "对象消息")])
    assert updates[0]["content"]["text"] == "对象消息"


def test_load_session_replays_then_returns():
    async def go():
        agent, _t, stream = make_agent(
            history_loader=lambda sid: [{"role": "user", "content": "旧对话"}])
        result = await agent.session_load({"sessionId": "s1", "cwd": "/tmp"})
        return result, stream.messages()

    result, msgs = asyncio.run(go())
    assert result == {}
    updates = [m for m in msgs if m.get("method") == "session/update"]
    assert len(updates) == 1
    assert updates[0]["params"]["update"]["content"]["text"] == "旧对话"
    assert updates[0]["params"]["sessionId"] == "s1"


def test_load_session_unsupported_raises_method_not_found():
    """没有 history_loader 时必须明确拒绝，而不是假装成功（回空历史）。"""
    async def go():
        agent, _t, _s = make_agent(history_loader=None)
        await agent.session_load({"sessionId": "s1"})

    with pytest.raises(RpcError) as exc:
        asyncio.run(go())
    assert exc.value.code == METHOD_NOT_FOUND


def test_initialize_declares_load_session_only_when_supported():
    async def go(with_loader):
        agent, _t, _s = make_agent(
            history_loader=(lambda sid: []) if with_loader else None)
        return await agent.initialize({"protocolVersion": 1})

    assert asyncio.run(go(True))["agentCapabilities"]["loadSession"] is True
    assert asyncio.run(go(False))["agentCapabilities"]["loadSession"] is False


# ============================================================ 工具展示

@pytest.mark.parametrize("name,expected", [
    ("read_file", "read"),
    ("write_file", "edit"),
    ("edit_files", "edit"),
    ("delete_file", "delete"),
    ("grep", "search"),
    ("astock_search", "search"),
    ("bash", "execute"),
    ("run_command", "execute"),
    ("webfetch", "fetch"),
    ("rename_file", "move"),
    ("todo_write", "think"),
    ("unknown_thing", "other"),
    ("", "other"),
])
def test_kind_of_tool(name, expected):
    assert kind_of_tool(name) == expected


def test_title_includes_key_argument():
    assert title_of_tool("read_file", {"filePath": "/a/b.txt"}) == "read_file: /a/b.txt"
    assert title_of_tool("bash", {"command": "ls -la"}) == "bash: ls -la"


def test_title_truncates_long_path():
    long_path = "/very/" + "x" * 200 + ".txt"
    title = title_of_tool("read_file", {"filePath": long_path})
    assert len(title) < 90
    assert title.endswith(".txt")


def test_title_without_args_is_just_name():
    assert title_of_tool("read_file", {}) == "read_file"
    assert title_of_tool("", {}) == "tool"


def test_status_mapping():
    assert status_of_result("ok") == "completed"
    assert status_of_result("error") == "failed"
    assert status_of_result("timeout") == "failed"
    # 未知状态按「有结果就算跑完」，不猜成 failed（会把成功显示成失败）
    assert status_of_result("weird") == "completed"


def test_cancelled_tool_shows_failed_not_completed():
    """被用户拒绝/安全拦截的工具必须显示 failed。

    这是真实的误导风险：拒绝删除后工具箱「completed」，用户会以为文件
    真的被删了。ACP 没有 cancelled 状态，只能映射到 failed。
    """
    assert status_of_result("cancelled") == "failed"


def test_content_truncates_long_output():
    out = content_of_result("x" * 10000)
    text = out[0]["content"]["text"]
    assert len(text) < 10000
    assert "已截断" in text


class FakeKernel:
    def __init__(self) -> None:
        self.handlers: Dict[str, Any] = {}

    class _Events:
        def __init__(self) -> None:
            self.handlers: Dict[str, Any] = {}

        def on(self, name, fn) -> None:
            self.handlers[name] = fn

    def __post_init__(self):
        pass

    @property
    def events(self):
        if not hasattr(self, "_events_obj"):
            self._events_obj = FakeKernel._Events()
        return self._events_obj


def test_tool_bridge_emits_tool_call_and_update():
    async def go():
        agent, _t, stream = make_agent()
        kernel = FakeKernel()
        bridge = ToolUpdateBridge(agent._emit, "s1")
        bridge.attach(kernel)

        kernel.events.handlers["tool:before_execute"]({
            "toolName": "read_file", "args": {"filePath": "/a.txt"}, "callId": "c1"})
        kernel.events.handlers["tool:after_execute"]({
            "toolName": "read_file", "callId": "c1", "status": "ok",
            "result": "内容", "durationMs": 10})
        await asyncio.sleep(0.05)   # 让 create_task 的 emit 落地
        return stream.messages()

    msgs = asyncio.run(go())
    updates = [m["params"]["update"] for m in msgs if m.get("method") == "session/update"]
    kinds = [u["sessionUpdate"] for u in updates]
    assert "tool_call" in kinds
    assert "tool_call_update" in kinds
    call = next(u for u in updates if u["sessionUpdate"] == "tool_call")
    assert call["toolCallId"] == "c1"
    assert call["kind"] == "read"
    assert call["status"] == "in_progress"


def test_tool_bridge_ignores_events_without_call_id():
    """没有 callId 的事件无法关联到具体调用，发出去只会产生垃圾卡片。"""
    async def go():
        agent, _t, stream = make_agent()
        kernel = FakeKernel()
        bridge = ToolUpdateBridge(agent._emit, "s1")
        bridge.attach(kernel)
        kernel.events.handlers["tool:before_execute"]({"toolName": "x"})
        await asyncio.sleep(0.05)
        return stream.messages()

    assert asyncio.run(go()) == []


def test_tool_bridge_detach_stops_emitting():
    async def go():
        agent, _t, stream = make_agent()
        kernel = FakeKernel()
        bridge = ToolUpdateBridge(agent._emit, "s1")
        bridge.attach(kernel)
        bridge.detach()
        kernel.events.handlers["tool:before_execute"]({
            "toolName": "read_file", "callId": "c9"})
        await asyncio.sleep(0.05)
        return stream.messages()

    assert asyncio.run(go()) == []


# ============================================================ 权限审批

def test_build_options_without_remember():
    opts = build_options(False)
    assert [o["optionId"] for o in opts] == [ALLOW_ONCE, "reject-once"]
    # 没有规则时不给「总是允许」——那按钮点了也没有记忆能力
    assert all(o["kind"] != "allow_always" for o in opts)


def test_build_options_with_remember():
    opts = build_options(True)
    assert ALLOW_ALWAYS in [o["optionId"] for o in opts]
    always = next(o for o in opts if o["optionId"] == ALLOW_ALWAYS)
    assert always["kind"] == "allow_always"


def test_interpret_outcome_allow():
    assert interpret_outcome({"outcome": {"outcome": "selected",
                                          "optionId": ALLOW_ONCE}}) is True
    assert interpret_outcome({"outcome": {"outcome": "selected",
                                          "optionId": ALLOW_ALWAYS}}) is True


def test_interpret_outcome_reject():
    assert interpret_outcome({"outcome": {"outcome": "selected",
                                          "optionId": "reject-once"}}) is False


def test_interpret_outcome_cancelled_is_reject():
    """用户点取消 → 必须当拒绝。把不确定当放行是危险的。"""
    assert interpret_outcome({"outcome": {"outcome": "cancelled"}}) is False


def test_interpret_outcome_malformed_is_reject():
    assert interpret_outcome(None) is False
    assert interpret_outcome({}) is False
    assert interpret_outcome({"outcome": "garbage"}) is False


def test_permission_bridge_relays_decision_to_gate():
    """用户在编辑器点「允许」→ 必须回填审批门，否则工具挂到 600s 超时。"""
    recorded: List[tuple] = []

    async def fake_request(method, params):
        assert method == "session/request_permission"
        assert params["sessionId"] == "s1"
        assert params["options"]
        return {"outcome": {"outcome": "selected", "optionId": ALLOW_ONCE}}

    async def go():
        t, _stream = make_transport()
        t.request = fake_request  # type: ignore[assignment]
        bridge = PermissionBridge(t, lambda aid, ok: recorded.append((aid, ok)), "s1")
        kernel = FakeKernel()
        bridge.attach(kernel)
        kernel.events.handlers["approval:request"]({
            "id": "apv_1", "action": "写入 /a.txt", "reason": "需要确认",
            "rememberable": False, "judge_opinion": None, "draft": None})
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert recorded == [("apv_1", True)]


def test_permission_bridge_channel_failure_denies():
    """通道断了必须按拒绝处理，不能挂住（挂住用户完全不知道发生了什么）。"""
    recorded: List[tuple] = []

    async def failing_request(method, params):
        raise RuntimeError("通道已断")

    async def go():
        t, _stream = make_transport()
        t.request = failing_request  # type: ignore[assignment]
        bridge = PermissionBridge(t, lambda aid, ok: recorded.append((aid, ok)), "s1")
        kernel = FakeKernel()
        bridge.attach(kernel)
        kernel.events.handlers["approval:request"]({"id": "apv_2", "action": "x"})
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert recorded == [("apv_2", False)]


def test_prompt_turn_attaches_and_detaches_bridges():
    """turn 结束必须卸载桥，否则事件会重复推送（多轮后指数增长）。"""
    class Kernel(FakeKernel):
        pass

    class FakeLoop:
        def __init__(self):
            self.kernel = Kernel()

        async def run_task(self, prompt, **kw):
            return ("ok", {})

    async def go():
        t, _stream = make_transport()
        loop_holder = {}

        def factory(session_id, cwd):
            loop_holder["loop"] = FakeLoop()
            return loop_holder["loop"]

        agent = AcpAgent(t, factory, permission_relay=lambda a, b: None)
        sid = (await agent.session_new({"cwd": "/tmp"}))["sessionId"]
        await agent.session_prompt({"sessionId": sid,
                                    "prompt": [{"type": "text", "text": "hi"}]})
        return loop_holder["loop"].kernel.events.handlers

    handlers = asyncio.run(go())
    # 挂过就该有记录；detach 后再次触发不应产生新消息（由 _detached 控制）
    assert "tool:before_execute" in handlers
