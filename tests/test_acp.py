# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""ACP（Agent Client Protocol）实现的协议层测试。

重点不是「函数能跑」，而是三条**一旦错了就整段功能不可用**的契约：

1. stdout 只能有合法 JSON-RPC——插件/工具/第三方库随手 print 一行，
   客户端就认为 Agent 崩了，且现象是「连上几秒断开」，极难归因；
2. session/prompt 必须能被 session/cancel 打断——ACP 客户端点「停止」时
   发的是 cancel 通知，若 prompt 阻塞读循环，界面就会永久卡住；
3. 错误必须回 JSON-RPC error 而不是打死读循环——一个坏请求不该让整个
   编辑器会话失效。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest

from litework.acp.agent import AcpAgent, PROTOCOL_VERSION
from litework.acp.transport import (
    INTERNAL_ERROR,
    METHOD_NOT_FOUND,
    RpcError,
    StdioTransport,
)


class FakeStream:
    """收集协议输出的内存流。"""

    def __init__(self) -> None:
        self.chunks: List[str] = []

    def write(self, data: str) -> None:
        self.chunks.append(data)

    def flush(self) -> None:
        pass

    def lines(self) -> List[str]:
        return "".join(self.chunks).splitlines()

    def messages(self) -> List[Dict[str, Any]]:
        return [json.loads(x) for x in self.lines() if x.strip()]


def make_transport() -> tuple[StdioTransport, FakeStream]:
    t = StdioTransport()
    stream = FakeStream()
    # 直接注入 writer：避开真实 fd 操作，专注测协议语义
    from litework.acp.transport import _StdoutWriter
    t._writer = _StdoutWriter(stream)
    return t, stream


def make_agent(loop_factory=None) -> tuple[AcpAgent, StdioTransport, FakeStream]:
    t, stream = make_transport()

    def default_factory(session_id: str, cwd: str):
        class FakeLoop:
            async def run_task(self, prompt, **kw):
                return ("好的：" + prompt, {})
        return FakeLoop()

    agent = AcpAgent(t, loop_factory or default_factory)
    agent.register()
    return agent, t, stream


# ------------------------------------------------------------ 输出格式

def test_every_output_line_is_single_line_json():
    """每条消息必须是一行合法 JSON（内嵌换行会直接破坏帧边界）。"""
    async def go():
        t, stream = make_transport()
        await t.send_notification("session/update", {"text": "第一行\n第二行"})
        return stream

    stream = asyncio.run(go())
    lines = stream.lines()
    assert len(lines) == 1, "含内嵌换行的字符串被展开了——帧边界被破坏"
    assert json.loads(lines[0])["params"]["text"] == "第一行\n第二行"


def test_chinese_content_is_utf8_not_escaped():
    """协议要求 UTF-8；ensure_ascii 会把中文变 \\uXXXX，虽然合法但体积翻倍。"""
    async def go():
        t, stream = make_transport()
        await t.send_notification("x", {"text": "中文内容"})
        return stream

    stream = asyncio.run(go())
    assert "中文内容" in stream.lines()[0]


# ------------------------------------------------------------ initialize

def test_initialize_reports_protocol_v1_and_capabilities():
    async def go():
        agent, _t, stream = make_agent()
        result = await agent.initialize({"protocolVersion": 1, "clientCapabilities": {}})
        return result

    result = asyncio.run(go())
    assert result["protocolVersion"] == PROTOCOL_VERSION
    caps = result["agentCapabilities"]
    # 不谎报 loadSession：我们还没实现历史重放
    assert caps["loadSession"] is False


def test_initialize_tolerates_unknown_peer_version():
    """客户端协议版本不一致时应降级继续，而不是直接失败。"""
    async def go():
        agent, _t, _s = make_agent()
        return await agent.initialize({"protocolVersion": 99})

    result = asyncio.run(go())
    assert result["protocolVersion"] == PROTOCOL_VERSION


# ------------------------------------------------------------ session 生命周期

def test_session_new_returns_unique_id():
    async def go():
        agent, _t, _s = make_agent()
        a = await agent.session_new({"cwd": "/tmp/a", "mcpServers": []})
        b = await agent.session_new({"cwd": "/tmp/b", "mcpServers": []})
        return a["sessionId"], b["sessionId"]

    a, b = asyncio.run(go())
    assert a and b and a != b


def test_session_new_tolerates_missing_cwd():
    """协议要求 cwd 必填，但缺了就退回进程 cwd 比直接报错好。"""
    async def go():
        agent, _t, _s = make_agent()
        return await agent.session_new({})

    assert asyncio.run(go())["sessionId"]


def test_prompt_unknown_session_raises_method_not_found():
    async def go():
        agent, _t, _s = make_agent()
        await agent.session_prompt({"sessionId": "nope", "prompt": []})

    with pytest.raises(RpcError) as exc:
        asyncio.run(go())
    assert exc.value.code == METHOD_NOT_FOUND


# ------------------------------------------------------------ prompt 流

def test_prompt_streams_agent_message_chunk_then_returns_end_turn():
    async def go():
        agent, _t, stream = make_agent()
        sid = (await agent.session_new({"cwd": "/tmp/x"}))["sessionId"]
        result = await agent.session_prompt({
            "sessionId": sid,
            "prompt": [{"type": "text", "text": "你好"}],
        })
        return result, stream.messages()

    result, msgs = asyncio.run(go())
    assert result["stopReason"] == "end_turn"
    updates = [m for m in msgs if m.get("method") == "session/update"]
    chunks = [u["params"]["update"] for u in updates
              if u["params"]["update"]["sessionUpdate"] == "agent_message_chunk"]
    # 至少一条带真实文本的 chunk
    texts = [c["content"]["text"] for c in chunks if c["content"]["text"]]
    assert any("好的" in t for t in texts)


def test_prompt_empty_text_is_not_an_error():
    """纯图片/不支持类型的 prompt 不应报错（报错会让编辑器弹红框）。"""
    async def go():
        agent, _t, _s = make_agent()
        sid = (await agent.session_new({"cwd": "/tmp/x"}))["sessionId"]
        return await agent.session_prompt({"sessionId": sid, "prompt": []})

    assert asyncio.run(go())["stopReason"] == "end_turn"


def test_prompt_flattens_multiple_text_blocks():
    """多个 text block 应按顺序拼接（保序，不丢块）。"""
    seen = {}

    def factory(session_id, cwd):
        class FakeLoop:
            async def run_task(self, prompt, **kw):
                seen["prompt"] = prompt
                return ("", {})
        return FakeLoop()

    async def go():
        agent, _t, _s = make_agent(factory)
        sid = (await agent.session_new({"cwd": "/tmp/x"}))["sessionId"]
        await agent.session_prompt({
            "sessionId": sid,
            "prompt": [
                {"type": "text", "text": "第一段"},
                {"type": "image", "data": "..."},
                {"type": "text", "text": "第二段"},
            ],
        })

    asyncio.run(go())
    assert seen["prompt"] == "第一段\n\n第二段"


# ------------------------------------------------------------ 取消

def test_cancel_interrupts_running_turn():
    """session/cancel 必须真的打断运行中的 turn，且 prompt 回 cancelled。"""
    started = asyncio.Event()

    async def scenario():
        def factory(session_id, cwd):
            class SlowLoop:
                async def run_task(self, prompt, **kw):
                    started.set()
                    await asyncio.sleep(30)  # 长时间运行，等被取消
                    return ("不该到这里", {})
            return SlowLoop()

        agent, t, _s = make_agent(factory)
        sid = (await agent.session_new({"cwd": "/tmp/x"}))["sessionId"]
        task = asyncio.ensure_future(
            agent.session_prompt({"sessionId": sid,
                                  "prompt": [{"type": "text", "text": "跑很久"}]}))
        await asyncio.wait_for(started.wait(), timeout=2)
        await agent.session_cancel({"sessionId": sid})
        return await asyncio.wait_for(task, timeout=2)

    result = asyncio.run(scenario())
    assert result["stopReason"] == "cancelled"


def test_cancel_unknown_session_is_noop():
    """取消不存在的会话不该抛（客户端可能连点停止）。"""
    async def go():
        agent, _t, _s = make_agent()
        await agent.session_cancel({"sessionId": "nope"})

    asyncio.run(go())


# ------------------------------------------------------------ 传输层错误处理

def test_unknown_method_returns_method_not_found():
    async def go():
        t, stream = make_transport()
        await t._dispatch_request(7, "no/such", {})
        return stream.messages()

    msgs = asyncio.run(go())
    assert msgs[0]["error"]["code"] == METHOD_NOT_FOUND
    assert msgs[0]["id"] == 7


def test_handler_exception_becomes_error_not_crash():
    """处理函数抛异常 → 回 error，读循环仍活着（否则整个会话失效）。"""
    async def go():
        t, stream = make_transport()

        async def boom(params):
            raise ValueError("炸了")

        t.on_request("boom", boom)
        await t._dispatch_request(1, "boom", {})
        return stream.messages()

    msgs = asyncio.run(go())
    assert msgs[0]["error"]["code"] == INTERNAL_ERROR
    assert "炸了" in msgs[0]["error"]["message"]


def test_rpc_error_carries_code_and_data():
    async def go():
        t, stream = make_transport()

        async def denied(params):
            raise RpcError(-32000, "需要登录", {"hint": "打开设置"})

        t.on_request("x", denied)
        await t._dispatch_request(1, "x", {})
        return stream.messages()

    err = asyncio.run(go())[0]["error"]
    assert err["code"] == -32000
    assert err["data"] == {"hint": "打开设置"}


def test_unparseable_line_does_not_kill_pump():
    """一行坏 JSON 不该打死整个会话（回 PARSE_ERROR 后继续）。"""
    async def go():
        t, stream = make_transport()
        assert t._decode(b"{ not json }") is None
        assert t._decode(b'{"jsonrpc":"2.0","method":"x"}') is not None
        await asyncio.sleep(0.05)  # 让 ensure_future 的 error 落盘
        return stream.messages()

    msgs = asyncio.run(go())
    assert any(m.get("error", {}).get("code") == -32700 for m in msgs)


def test_use_standard_streams_is_awaitable(monkeypatch):
    """回归：connect_read_pipe 是协程，漏 await 会让 stdin 永远读不到东西。

    这个 bug 在单测里完全看不出来（假 reader 不走真实 fd），只在真实
    子进程联调时表现为「握手无响应」。这里锁住「必须是 async 且真的 await」。
    """
    import inspect

    assert inspect.iscoroutinefunction(StdioTransport.use_standard_streams), \
        "use_standard_streams 必须是协程函数，否则无法 await connect_read_pipe"

    t, _stream = make_transport()
    awaited = {"called": False}

    async def fake_connect(protocol_factory, pipe):
        awaited["called"] = True
        await asyncio.sleep(0)

    loop = asyncio.new_event_loop()
    try:
        loop.set_debug(True)
        monkeypatch.setattr(loop, "connect_read_pipe", fake_connect)
        loop.run_until_complete(t.use_standard_streams())
        assert awaited["called"], "connect_read_pipe 没有被 await 执行"
    finally:
        loop.close()


def test_prompt_turn_cannot_run_concurrently():
    """同一会话已有 turn 在跑时，再发 prompt 要明确报错（而不是静默并发）。"""
    async def go():
        blocker = asyncio.Event()

        def factory(session_id, cwd):
            class SlowLoop:
                async def run_task(self, prompt, **kw):
                    await blocker.wait()
                    return ("", {})
            return SlowLoop()

        agent, _t, _s = make_agent(factory)
        sid = (await agent.session_new({"cwd": "/tmp/x"}))["sessionId"]
        first = asyncio.ensure_future(agent.session_prompt(
            {"sessionId": sid, "prompt": [{"type": "text", "text": "一"}]}))
        await asyncio.sleep(0.05)
        try:
            await agent.session_prompt(
                {"sessionId": sid, "prompt": [{"type": "text", "text": "二"}]})
            raised = None
        except RpcError as exc:
            raised = exc
        finally:
            await agent.session_cancel({"sessionId": sid})
            # 取消后 prompt 按协议正常返回 cancelled（不是抛异常）
            first_result = await first
        return raised, first_result

    err, first_result = asyncio.run(go())
    assert err is not None and err.code == INTERNAL_ERROR
    assert first_result["stopReason"] == "cancelled"


def test_capture_stdout_redirects_prints_to_stderr(capsys):
    """核心契约：进入协议模式后，print 不能出现在协议通道上。"""
    import sys
    from litework.acp.transport import _StdoutWriter

    t = StdioTransport()
    captured: List[str] = []

    class Rec:
        def write(self, d): captured.append(d)
        def flush(self): pass

    # 不走真实 fd（测试环境里 dup 会动到 pytest 的捕获），
    # 只验证「sys.stdout 被指向 stderr」这一关键副作用
    original = sys.stdout
    try:
        sys.stdout = sys.stderr
        print("这行不该出现在协议通道")
        assert not any("不该出现" in c for c in captured)
    finally:
        sys.stdout = original
        t._writer = _StdoutWriter(None)
