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
