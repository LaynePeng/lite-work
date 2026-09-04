"""工具调用原子对修复测试：不完整历史续聊不再触发 LLM HTTP 400。"""
from __future__ import annotations

from litework.core.agent_loop import AgentLoop
from litework.core.context_manager import repair_tool_call_pairs
from litework.core.kernel import Kernel
from litework.core.session_store import SessionStore
from litework.core.types import Message
from litework.tools.registry import ToolRegistry
from tests.conftest import MockLLMAdapter, tool_call


def _asm(content, *calls):
    return Message(role="assistant", content=content, tool_calls=list(calls))


def _tool(cid, text="ok"):
    return Message(role="tool", content=text, tool_call_id=cid)


# ---------------------------------------------------------------- 单元测试

def test_complete_pair_kept():
    msgs = [
        Message(role="user", content="q"),
        _asm(None, tool_call("write_file", "{}", cid="c1")),
        _tool("c1"),
        Message(role="assistant", content="done"),
    ]
    out = repair_tool_call_pairs(msgs)
    assert len(out) == len(msgs)
    assert out[1].tool_calls and out[1].tool_calls[0].id == "c1"
    assert out[2].tool_call_id == "c1"


def test_missing_tool_result_drops_pair():
    msgs = [
        Message(role="user", content="q"),
        _asm(None, tool_call("write_file", "{}", cid="c1"),
             tool_call("read_file", "{}", cid="c2")),
        _tool("c1"),  # c2 的结果缺失
        Message(role="user", content="再来"),
    ]
    out = repair_tool_call_pairs(msgs)
    assert all(m.role != "tool" for m in out)
    assert all(not (m.role == "assistant" and m.tool_calls) for m in out)
    assert [m.role for m in out] == ["user", "user"]


def test_orphan_tool_message_dropped():
    msgs = [
        Message(role="user", content="q"),
        _tool("ghost"),
        Message(role="assistant", content="ok"),
    ]
    out = repair_tool_call_pairs(msgs)
    assert [m.role for m in out] == ["user", "assistant"]


def test_multiple_pairs_and_system_preserved():
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="q1"),
        _asm(None, tool_call("a", "{}", cid="c1")),
        _tool("c1"),
        Message(role="assistant", content="mid"),
        Message(role="user", content="q2"),
        _asm(None, tool_call("b", "{}", cid="c2"), tool_call("c", "{}", cid="c3")),
        _tool("c2"),
        _tool("c3"),
    ]
    out = repair_tool_call_pairs(msgs)
    assert len(out) == len(msgs)
    assert out[0].role == "system"
    assert out[-1].tool_call_id == "c3"


def test_broken_history_then_complete_pair():
    """残缺对在前、完整对在后：只丢弃残缺的。"""
    msgs = [
        Message(role="user", content="q1"),
        _asm(None, tool_call("a", "{}", cid="c1"), tool_call("b", "{}", cid="c2")),
        _tool("c1"),  # c2 缺失 → 整对丢弃
        Message(role="user", content="q2"),
        _asm(None, tool_call("c", "{}", cid="c3")),
        _tool("c3"),
    ]
    out = repair_tool_call_pairs(msgs)
    assert [m.role for m in out] == ["user", "user", "assistant", "tool"]
    assert out[-1].tool_call_id == "c3"


def test_empty_id_pair_gets_synthetic_ids():
    """旧版本适配器 bug 残留：assistant/tool 都缺 id → 按位置补齐一致的合成 id。"""
    msgs = [
        Message(role="user", content="q"),
        _asm(None, tool_call("webfetch", "{}", cid=""),
             tool_call("read_file", "{}", cid="")),
        _tool(""),
        _tool(""),
    ]
    out = repair_tool_call_pairs(msgs)
    assert len(out) == len(msgs)
    asm = out[1]
    assert asm.tool_calls[0].id and asm.tool_calls[1].id
    assert asm.tool_calls[0].id != asm.tool_calls[1].id
    assert out[2].tool_call_id == asm.tool_calls[0].id
    assert out[3].tool_call_id == asm.tool_calls[1].id


def test_empty_id_pair_with_missing_result_dropped():
    """空 id 链但 tool 结果缺失 → 整对丢弃（而不是保留非法对）。"""
    msgs = [
        Message(role="user", content="q"),
        _asm(None, tool_call("webfetch", "{}", cid=""),
             tool_call("read_file", "{}", cid="")),
        _tool(""),  # 只跟了 1 条结果
    ]
    out = repair_tool_call_pairs(msgs)
    assert all(not (m.role == "assistant" and m.tool_calls) for m in out)
    assert all(m.role != "tool" for m in out)


def test_finalize_tool_calls_fills_missing_ids():
    """适配器兜底：供应商流式响应缺 id 时补齐合成 id。"""
    from litework.llm.openai_compat import OpenAICompatAdapter
    from litework.core.types import ToolCall

    calls = OpenAICompatAdapter._finalize_tool_calls({
        0: ToolCall(id="", name="webfetch", arguments="{}"),
        1: ToolCall(id="call_real", name="read_file", arguments="{}"),
    })
    assert len(calls) == 2
    assert calls[0].id and calls[0].id.startswith("call_")
    assert calls[1].id == "call_real"


# ---------------------------------------------------------------- 复现验证（真实 SSE 解析器）

class FakeSSEResponse:
    """模拟供应商的流式响应：逐字节喂给真实 _parse_sse 解析器。"""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c


def _sse_chunk(data: str) -> bytes:
    return f"data: {data}\n\n".encode("utf-8")


def _build_sse_payload(id_in_first_chunk: bool) -> bytes:
    """构造一个 webfetch tool_call 的流式响应。

    id_in_first_chunk=True  → 正常供应商（DeepSeek/OpenAI）：id 只出现在首个 chunk
    id_in_first_chunk=False → 问题供应商（Kimi/GLM/通义等）：全程不带 id
    """
    first = {"index": 0, "function": {"name": "webfetch", "arguments": ""}}
    if id_in_first_chunk:
        first["id"] = "call_abc123"
    return (
        _sse_chunk('{"choices":[{"delta":{"tool_calls":[' + _json(first) + "]}}]}")
        + _sse_chunk('{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"url\\":\\"https://example.com/\\"}"}}]}}]}')
        + b"data: [DONE]\n\n"
    )


def _json(d) -> str:
    import json as _json_mod

    return _json_mod.dumps(d, ensure_ascii=False)


async def test_parse_sse_with_id_in_first_chunk():
    """正常供应商：id 只出现在首个 chunk，解析后 id 保留。"""
    from litework.llm.openai_compat import OpenAICompatAdapter

    adapter = OpenAICompatAdapter(api_key="k")
    _, calls, _ = await adapter._parse_sse(FakeSSEResponse([_build_sse_payload(True)]), None)
    assert len(calls) == 1
    assert calls[0].id == "call_abc123"
    assert calls[0].name == "webfetch"
    assert calls[0].arguments == '{"url":"https://example.com/"}'


async def test_parse_sse_without_id_reproduces_400_root_cause():
    """复现用户问题：供应商全程不带 id → 解析后必须补齐合成 id，
    否则 assistant(tool_calls) 与 tool 消息无法匹配，API 返回 400。"""
    from litework.llm.openai_compat import OpenAICompatAdapter

    adapter = OpenAICompatAdapter(api_key="k")
    _, calls, _ = await adapter._parse_sse(FakeSSEResponse([_build_sse_payload(False)]), None)
    assert len(calls) == 1
    assert calls[0].id and calls[0].id.startswith("call_"), "缺 id 时必须补齐合成 id"


# ---------------------------------------------------------------- 集成测试

class RecordingAdapter(MockLLMAdapter):
    """记录每次发给 LLM 的消息链。"""

    def __init__(self, responses):
        super().__init__(responses)
        self.seen: list = []

    async def chat_stream(self, messages, tools, events=None):
        self.seen.append([m.to_dict() for m in messages])
        return await super().chat_stream(messages, tools, events)


async def test_resumed_broken_history_is_repaired(tmp_path):
    """会话历史里残留不完整 tool_calls（任务被停止时落盘）→ 续聊自动修复。"""
    kernel = Kernel("broken-session")
    # 模拟上次任务被中止：assistant 声称调用了 2 个工具，只落盘了 1 条结果
    kernel.ctx.messages = [
        _asm(None, tool_call("write_file", "{}", cid="c1"),
             tool_call("read_file", "{}", cid="c2")),
        _tool("c1"),
    ]
    store = SessionStore(str(tmp_path / "sessions"))
    adapter = RecordingAdapter([("继续完成。", [])])
    loop = AgentLoop(kernel=kernel, adapter=adapter, registry=ToolRegistry(),
                     session_store=store, max_steps=5)

    result, _ = await loop.run_task("接着做", system_prompt="测试")

    assert result == "继续完成。"
    first_call = adapter.seen[0]
    # 发给 LLM 的消息链中不再有残缺的 tool_calls / 无主 tool 消息
    assert all(not (m.get("role") == "assistant" and m.get("tool_calls")) for m in first_call)
    assert all(m.get("role") != "tool" for m in first_call)
    # 修复后的干净历史已落盘
    snap = store.load("broken-session")
    assert snap is not None
    assert all(not (m.role == "assistant" and m.tool_calls) for m in snap.messages)


async def test_complete_history_untouched(tmp_path):
    """完整历史（配对齐全）续聊：修复函数不改动任何消息。"""
    kernel = Kernel("ok-session")
    kernel.ctx.messages = [
        _asm(None, tool_call("write_file", "{}", cid="c1")),
        _tool("c1"),
        Message(role="assistant", content="done"),
    ]
    adapter = RecordingAdapter([("好。", [])])
    loop = AgentLoop(kernel=kernel, adapter=adapter, registry=ToolRegistry(),
                     session_store=SessionStore(str(tmp_path / "sessions")), max_steps=5)

    await loop.run_task("继续", system_prompt="测试")

    first_call = adapter.seen[0]
    roles = [m.get("role") for m in first_call]
    assert roles == ["system", "assistant", "tool", "assistant", "user"]
    asm = next(m for m in first_call if m.get("role") == "assistant" and m.get("tool_calls"))
    assert asm["tool_calls"][0]["id"] == "c1"


async def test_empty_id_tool_calls_round_trip(tmp_path):
    """复现用户场景：供应商返回无 id 的 tool_calls（webfetch）→ 整轮闭环不 400。

    第一次调用返回空 id 的 webfetch 调用；工具执行后第二次调用时，
    消息链里的 assistant(tool_calls) 与 tool 消息必须带一致的 id。
    """
    registry = ToolRegistry()

    async def webfetch(args):
        return "[Fetch OK]: https://example.com/ (200)"

    registry.register("webfetch", "抓网页", {"type": "object"}, webfetch)

    kernel = Kernel("empty-id-session")
    adapter = RecordingAdapter([
        # 模拟 Kimi/GLM/通义等供应商：流式 tool_calls 不带 id
        ("", [tool_call("webfetch", '{"url":"https://example.com/"}', cid="")]),
        ("查到了。", []),
    ])
    loop = AgentLoop(kernel=kernel, adapter=adapter, registry=registry,
                     session_store=SessionStore(str(tmp_path / "sessions")), max_steps=5)

    result, stats = await loop.run_task("网上查一下", system_prompt="测试")

    assert result == "查到了。"
    assert stats["tool_calls"] == 1
    # 第二次 LLM 调用的消息链：assistant(tool_calls) 必须能被 tool 消息匹配
    second_call = adapter.seen[1]
    asm = next(m for m in second_call if m.get("role") == "assistant" and m.get("tool_calls"))
    tools = [m for m in second_call if m.get("role") == "tool"]
    ids = {tc["id"] for tc in asm["tool_calls"]}
    assert len(ids) == 1 and "" not in ids
    assert {t["tool_call_id"] for t in tools} == ids