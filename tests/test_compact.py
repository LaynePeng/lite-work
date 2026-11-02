# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""/compact 手动压缩测试：强制折叠 / 摘要落盘 / focus 透传 / 统计回写 / 拒绝场景。"""
import asyncio

import pytest

from litework.app import AgentApp
from litework.core.types import Message
from tests.conftest import MockLLMAdapter


def _seed_messages():
    """4 轮对话（system + 4 user + 4 assistant），keep_turns=2 → 折叠前 2 轮。"""
    msgs = [Message(role="system", content="sys prompt")]
    for i in range(4):
        msgs.append(Message(role="user", content=f"问题{i}：请帮我做事情{i}"))
        msgs.append(Message(role="assistant", content=f"回答{i}：已完成事情{i}"))
    return msgs


class RecordingAdapter(MockLLMAdapter):
    """记录每次请求的最后一条 user 消息（验证 focus 透传）。"""

    def __init__(self, reply: str):
        super().__init__([(reply, [])])
        self.last_instruction = None

    async def chat_stream(self, messages, tools, events=None):
        self.last_instruction = messages[-1].content
        return await super().chat_stream(messages, tools, events)


def _make_app(tmp_path, adapter) -> AgentApp:
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lc"))
    app._mock_adapter = adapter
    return app


def test_compact_folds_old_turns_and_saves(tmp_path):
    adapter = RecordingAdapter("这是摘要：完成了事情0和事情1")
    app = _make_app(tmp_path, adapter)
    sid = "s1"
    app.session_store.save(sid, _seed_messages())

    result = asyncio.run(app.compact_session(sid))

    assert result["ok"] is True
    assert result["before_tokens"] > result["after_tokens"]
    assert result["removed_tokens"] == result["before_tokens"] - result["after_tokens"]
    assert result["turns_compacted"] == 2
    # 落盘结构：system + [历史摘要] + 最近 2 轮原样保留
    snap = app.session_store.load(sid)
    assert snap.messages[0].role == "system"
    assert snap.messages[1].role == "user"
    assert snap.messages[1].content.startswith("[历史摘要] ")
    assert "这是摘要" in snap.messages[1].content
    tail = snap.messages[2:]
    assert [m.content for m in tail] == ["问题2：请帮我做事情2", "回答2：已完成事情2",
                                         "问题3：请帮我做事情3", "回答3：已完成事情3"]


def test_compact_focus_reaches_summarizer(tmp_path):
    adapter = RecordingAdapter("摘要")
    app = _make_app(tmp_path, adapter)
    sid = "s2"
    app.session_store.save(sid, _seed_messages())

    result = asyncio.run(app.compact_session(sid, focus="数据库设计"))

    assert result["ok"] is True
    assert "数据库设计" in (adapter.last_instruction or "")


def test_compact_updates_session_stats(tmp_path):
    adapter = RecordingAdapter("摘要内容")
    app = _make_app(tmp_path, adapter)
    sid = "s3"
    app.session_store.save(sid, _seed_messages())

    before = app.get_context_session_stats(sid)
    assert before.get("compression_count") is None

    result = asyncio.run(app.compact_session(sid))

    stats = app.get_context_session_stats(sid)
    assert stats["compression_count"] == 1
    assert stats["compressed_tokens"] > 0
    assert stats["last_prompt_tokens"] == result["after_tokens"]


def test_compact_rejects_short_session(tmp_path):
    app = _make_app(tmp_path, RecordingAdapter("摘要"))
    sid = "s4"
    # 只有 1 轮
    app.session_store.save(sid, [Message(role="user", content="hi"),
                                 Message(role="assistant", content="hello")])
    result = asyncio.run(app.compact_session(sid))
    assert result["ok"] is False
    # 会话只有 2 轮（keep_turns=2）→ 没有可压缩历史
    sid2 = "s5"
    msgs = [Message(role="user", content="q1"), Message(role="assistant", content="a1"),
            Message(role="user", content="q2"), Message(role="assistant", content="a2")]
    app.session_store.save(sid2, msgs)
    result2 = asyncio.run(app.compact_session(sid2))
    assert result2["ok"] is False and "没有可压缩" in result2["reason"]


def test_compact_preserves_metadata_and_tool_pairs(tmp_path):
    """metadata（模型覆盖等）保留；tail 含完整 tool 对时不被拆坏。"""
    adapter = RecordingAdapter("摘要")
    app = _make_app(tmp_path, adapter)
    sid = "s6"
    from litework.core.types import ToolCall
    msgs = _seed_messages() + [
        Message(role="user", content="q4"),
        Message(role="assistant", content=None,
                tool_calls=[ToolCall(id="c1", name="read_file", arguments="{}")]),
        Message(role="tool", content="data", tool_call_id="c1"),
    ]
    app.session_store.save(sid, msgs, metadata={"model": {"provider": "p", "model": "m"}})

    result = asyncio.run(app.compact_session(sid))

    assert result["ok"] is True
    snap = app.session_store.load(sid)
    assert snap.metadata.get("model") == {"provider": "p", "model": "m"}
    # 最后一轮的 tool 对完整保留在 tail
    assert snap.messages[-1].role == "tool"
    assert snap.messages[-1].tool_call_id == "c1"
    assert snap.messages[-2].tool_calls and snap.messages[-2].tool_calls[0].id == "c1"


def test_compact_summary_failure_keeps_session(tmp_path):
    """LLM 摘要失败（空回复）→ ok=False 且会话原样保留。"""

    class EmptyAdapter(MockLLMAdapter):
        async def chat_stream(self, messages, tools, events=None):
            return "", [], None

    app = _make_app(tmp_path, EmptyAdapter([]))
    sid = "s7"
    app.session_store.save(sid, _seed_messages())

    result = asyncio.run(app.compact_session(sid))

    assert result["ok"] is False and "摘要" in result["reason"]
    snap = app.session_store.load(sid)
    assert len(snap.messages) == len(_seed_messages())  # 原样未动


def test_compact_then_task_stats_no_keyerror(tmp_path):
    """先手动压缩、后任务累计 context:stats：不得因统计字典缺字段而 KeyError。

    回归：compact_session 曾用 setdefault(sid, {}) 初始化空统计字典，
    后续 accumulate_context_stats 对缺失的 prompt_tokens 等字段 += 时抛
    KeyError，导致 context:stats 事件断裂、前端上下文面板数据不显示。
    """
    adapter = RecordingAdapter("摘要内容")
    app = _make_app(tmp_path, adapter)
    sid = "s8"
    app.session_store.save(sid, _seed_messages())

    # 先手动压缩（写入压缩统计）
    result = asyncio.run(app.compact_session(sid))
    assert result["ok"] is True

    # 模拟随后的任务 context:stats 累计：不应抛 KeyError
    stats = app.accumulate_context_stats(sid, {
        "prompt_tokens": 1000, "output_tokens": 200,
        "cache_hit_tokens": 300, "cache_miss_tokens": 700,
        "compression_count": 0, "compressed_tokens": 0,
        "tool_calls": 1, "blocked": 0, "cost_estimate": 0.001,
    })
    assert stats["prompt_tokens"] == 1000
    assert stats["compression_count"] == 1
    assert stats["last_prompt_tokens"] == result["after_tokens"]


def test_compact_summary_repairs_orphan_tool_after_truncation(tmp_path, monkeypatch):
    """head 超长软截断的刀口落在 assistant(tool_calls)+tool 原子对中间 →
    selected 以无主 tool 消息开头，必须先修复再发。

    回归（用户报障，DeepSeek 400）：Messages with role 'tool' must be a
    response to a preceding message with 'tool_calls'。
    """
    monkeypatch.setattr("litework.app.MAX_SUMMARY_CHARS", 100)

    sent = {}

    class SpyAdapter(RecordingAdapter):
        async def chat_stream(self, messages, tools, events=None):
            sent["roles"] = [m.role for m in messages]
            return await super().chat_stream(messages, tools, events)

    adapter = SpyAdapter("摘要")
    app = _make_app(tmp_path, adapter)
    sid = "s-trunc"
    from litework.core.types import ToolCall
    msgs = [Message(role="system", content="sys"), Message(role="user", content="q0"),
            # 超长 tool 结果：截断保留最近消息时，刀口落在 c1 与 tool 结果之间
            Message(role="assistant", content=None,
                    tool_calls=[ToolCall(id="c1", name="read_file", arguments="{}")]),
            Message(role="tool", content="x" * 600, tool_call_id="c1"),
            Message(role="user", content="q1"), Message(role="assistant", content="a1")]
    for i in (2, 3):  # 轮2/3 = tail（keep_turns=2）
        msgs.append(Message(role="user", content=f"q{i}"))
        msgs.append(Message(role="assistant", content=f"a{i}"))
    app.session_store.save(sid, msgs)

    result = asyncio.run(app.compact_session(sid))

    assert result["ok"] is True
    # 修复后发给 LLM 的请求不含无主 tool 消息（修复前 roles 以 tool 开头）
    assert "tool" not in sent["roles"][1:]
    assert sent["roles"][0] == "system"


def test_compact_summary_sets_header_context(tmp_path):
    """手动压缩直连 adapter：必须注入 header_context，让 custom_headers 的
    {conversation_id} 模板展开（否则会话亲和头被丢弃，opencode zen go 实测
    400 MissingSessionID）；压缩保存也不得覆盖已生成的 conversation_ids。
    """
    from litework.llm.openai_compat import OpenAICompatAdapter

    adapter = OpenAICompatAdapter(
        api_key="k", model="m", provider_id="custom_x",
        custom_headers={"x-opencode-session": "{conversation_id}"},
    )
    captured = {}

    async def fake_stream(messages, tools, events=None):
        captured["headers"] = adapter._headers()
        return "摘要", [], None

    adapter.chat_stream = fake_stream
    app = _make_app(tmp_path, adapter)
    sid = "s-hdr"
    app.session_store.save(sid, _seed_messages())

    result = asyncio.run(app.compact_session(sid))

    assert result["ok"] is True
    cid = captured["headers"].get("x-opencode-session")
    assert cid, "会话亲和头必须展开为非空值"
    # 压缩落盘后 conversation_ids 仍在（不被开头的旧 metadata 覆盖）
    snap = app.session_store.load(sid)
    assert snap.metadata.get("conversation_ids", {}).get("custom_x") == cid


# ---------------------------------------------------------------- 软截断回归
#
# 回归的是真实事故：_summarize_history 在 head 超过 200_000 字符时**直接
# return None**，调用方据此判定「摘要失败」→ 回退免费裁剪（prune）。而 prune
# 是逐轮投影、不写回 messages，于是下一轮又从完整历史重算一遍，历史永远
# 压不下去（线上日志：604 次 Exceeded token budget、单调涨到 1.5M tokens）。
# 正确行为：超长时软截断（丢最早的、保最近的），让压缩真的发生。


def _huge_head(n_messages=40, chars_each=10_000):
    """构造一个必然超过 MAX_SUMMARY_CHARS 的 head。"""
    from litework.core.context_manager import MAX_SUMMARY_CHARS

    assert n_messages * chars_each > MAX_SUMMARY_CHARS
    head = [Message(role="system", content="sys prompt")]
    for i in range(n_messages):
        head.append(Message(role="user", content=f"[{i}] " + "x" * chars_each))
        head.append(Message(role="assistant", content=f"[{i}] 回答 " + "y" * chars_each))
    return head


async def test_summarize_history_truncates_instead_of_giving_up(tmp_path):
    """head 超长 → 软截断后仍产出摘要，而不是返回 None（历史压不下去的根因）。"""
    from litework.core.context_manager import MAX_SUMMARY_CHARS

    app = _make_app(tmp_path, MockLLMAdapter([("摘要正文", [])]))
    app._mock_adapter = MockLLMAdapter([("摘要正文", [])])

    captured = {}

    class CapturingAdapter(MockLLMAdapter):
        async def chat_stream(self, messages, tools, events=None):
            captured["chars"] = sum(len(m.content or "") for m in messages)
            captured["n"] = len(messages)
            # 必须带 tools（前缀对齐）；这里只验证不返回 None
            return "摘要正文", [], None

    from litework.core.agent_loop import AgentLoop
    from litework.core.kernel import Kernel
    from litework.tools.registry import ToolRegistry

    loop = AgentLoop(kernel=Kernel("trunc"), adapter=CapturingAdapter([("完成", [])]),
                     registry=ToolRegistry(), token_budget=1000, context_window=1_000_000)
    head = _huge_head()
    summary = await loop._summarize_history(head)

    assert summary == "摘要正文", "超长时必须软截断后继续摘要，不能返回 None"
    # 软截断确实发生了：送出的字符数被压到上限附近，而不是原始 80 万
    assert captured["chars"] <= MAX_SUMMARY_CHARS + 5_000
    assert captured["chars"] < sum(len(m.content or "") for m in head)


async def test_summarize_history_keeps_newest_when_truncating(tmp_path):
    """软截断丢最早的、保最近的（最近的才是当前任务上下文）。"""
    from litework.core.agent_loop import AgentLoop
    from litework.core.context_manager import MAX_SUMMARY_CHARS
    from litework.core.kernel import Kernel
    from litework.tools.registry import ToolRegistry

    sent = []

    class CapturingAdapter(MockLLMAdapter):
        async def chat_stream(self, messages, tools, events=None):
            sent.extend(messages)
            return "摘要正文", [], None

    loop = AgentLoop(kernel=Kernel("trunc2"), adapter=CapturingAdapter([("完成", [])]),
                     registry=ToolRegistry(), token_budget=1000, context_window=1_000_000)
    head = _huge_head()
    await loop._summarize_history(head)

    body_text = " ".join(m.content or "" for m in sent)
    # 最后一条编号（39）必须在，最早一条（0）应已被丢弃
    assert "[39]" in body_text
    assert "[0] " not in body_text


async def test_summarize_history_short_head_unchanged(tmp_path):
    """未超长的 head 不应被截断（不引入行为变化）。"""
    from litework.core.agent_loop import AgentLoop
    from litework.core.kernel import Kernel
    from litework.tools.registry import ToolRegistry

    sent = []

    class CapturingAdapter(MockLLMAdapter):
        async def chat_stream(self, messages, tools, events=None):
            sent.extend(messages)
            return "短摘要", [], None

    loop = AgentLoop(kernel=Kernel("short"), adapter=CapturingAdapter([("完成", [])]),
                     registry=ToolRegistry(), token_budget=1000, context_window=1_000_000)
    head = [
        Message(role="system", content="sys"),
        Message(role="user", content="问题一"),
        Message(role="assistant", content="回答一"),
    ]
    assert await loop._summarize_history(head) == "短摘要"
    body = [m for m in sent if m.role != "system"]
    assert any("问题一" in (m.content or "") for m in body)


async def test_manual_compact_reuses_tool_schema_cache(tmp_path):
    """手动压缩不应每次都 build_registry() 把整套插件重装一遍。

    线上证据：日志里「Plugin loaded」在同一毫秒刷出 20 条、全会话 223 次
    Kernel 重建——摘要路径每轮都 build_registry() 是来源之一。
    """
    calls = {"n": 0}
    app = _make_app(tmp_path, MockLLMAdapter([("摘要正文", [])]))

    orig = app.build_registry

    def counting_build_registry(*a, **kw):
        calls["n"] += 1
        return orig(*a, **kw)

    app.build_registry = counting_build_registry  # type: ignore[method-assign]

    first = app.cached_tool_schemas()
    second = app.cached_tool_schemas()
    assert first == second
    assert calls["n"] == 1, "第二次应命中缓存，不该再重建插件"

    # 插件/MCP 变更后缓存必须失效
    app._invalidate_plugin_cache()
    app.cached_tool_schemas()
    assert calls["n"] == 2, "失效后应重新构建一次"


async def test_manual_compact_uses_cached_schemas(tmp_path):
    """真正的 /compact 走缓存：摘要调用的 tools 来自 cached_tool_schemas。"""
    adapter = RecordingAdapter("压缩摘要")
    app = _make_app(tmp_path, adapter)
    app._mock_adapter = adapter
    sid = "sess-cache"
    await asyncio.to_thread(
        app.session_store.save, sid, _seed_messages(),
    )

    used = {"n": 0}
    real_cached = app.cached_tool_schemas

    def spy_cached():
        used["n"] += 1
        return real_cached()

    app.cached_tool_schemas = spy_cached  # type: ignore[method-assign]
    res = await app.compact_session(sid, "")
    assert res["ok"] is True
    assert used["n"] == 1, "手动压缩应走 cached_tool_schemas 而非 build_registry"
