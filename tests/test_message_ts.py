# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""消息时间戳（Message.ts）：落盘往返、出网剥离、老数据兼容。

背景：气泡时间必须由后端在消息诞生时定死——前端打点会在切页签、刷新重拉
历史（全部盖成拉取时刻）、任务结束重拉快照覆盖本地消息三种情况下出错。
"""
from __future__ import annotations

from pathlib import Path

from litework.core.session_store import SessionStore
from litework.core.types import Message, now_ms


def test_ts_round_trips_through_dict():
    m = Message(role="user", content="hi", ts=1792897251000)
    assert m.to_dict()["ts"] == 1792897251000
    back = Message.from_dict(m.to_dict())
    assert back.ts == 1792897251000
    assert back == m


def test_missing_ts_stays_none():
    """老快照没有 ts：不得伪造成 0 或当前时间（前端据此不显示时间）。"""
    d = {"role": "user", "content": "old"}
    m = Message.from_dict(d)
    assert m.ts is None
    assert "ts" not in m.to_dict(), "ts 为 None 时不写进落盘/事件负载"


def test_dirty_ts_values_are_dropped():
    """脏值（字符串 / bool / 0 / 负数 / None）一律按缺失处理，不炸前端。"""
    for bad in ("1792897251000", True, 0, -1, None, "", {}):
        m = Message.from_dict({"role": "user", "content": "x", "ts": bad})
        assert m.ts is None, bad


def test_ts_survives_session_save_load(tmp_path: Path):
    store = SessionStore(str(tmp_path / "sessions"))
    ts = now_ms()
    store.save("s1", [Message(role="user", content="你好", ts=ts)], {})
    snap = store.load("s1")
    assert snap is not None
    assert snap.messages[0].ts == ts


def test_now_ms_is_milliseconds():
    """口径是毫秒（与 session created_at/updated_at 一致），不是秒。"""
    assert now_ms() > 1_700_000_000_000


def test_wire_message_strips_ts():
    """ts 是内部字段，不得出网——严格网关对未知字段直接 400（同 name/agent）。"""
    from litework.llm.openai_compat import OpenAICompatAdapter

    m = Message(role="assistant", content="答案", agent="build", ts=1792897251000)
    wire = OpenAICompatAdapter._wire_message(m)
    assert "ts" not in wire
    assert "agent" not in wire
    assert "name" not in wire
    assert wire["content"] == "答案"


# ---------------------------------------------------------------- 端到端

async def test_run_task_stamps_user_and_assistant_messages(tmp_path: Path):
    """真实跑一轮任务：user 与 assistant 消息都必须带上毫秒 ts 并落盘。

    同时校验发给 LLM 的消息**不含** ts（否则严格网关 400）。
    """
    from litework.core.agent_loop import AgentLoop
    from litework.core.kernel import Kernel
    from litework.tools.registry import ToolRegistry
    from tests.conftest import MockLLMAdapter

    sent: list = []

    class RecordingAdapter(MockLLMAdapter):
        """在真实 wire 上再转一道：确认出网负载不含内部字段。"""

        async def chat_stream(self, messages, tools, events=None):
            from litework.llm.openai_compat import OpenAICompatAdapter

            sent.append([OpenAICompatAdapter._wire_message(m) for m in messages])
            return await super().chat_stream(messages, tools, events)

    store = SessionStore(str(tmp_path / "sessions"))
    adapter = RecordingAdapter([("收到，开始处理。", [])])
    loop = AgentLoop(
        kernel=Kernel("ts-session"), adapter=adapter, registry=ToolRegistry(),
        session_store=store, max_steps=3,
    )

    result, _ = await loop.run_task("你好", system_prompt="测试")
    assert result == "收到，开始处理。"

    snap = store.load("ts-session")
    assert snap is not None
    users = [m for m in snap.messages if m.role == "user"]
    assistants = [m for m in snap.messages if m.role == "assistant"]
    assert users and all(isinstance(m.ts, int) and m.ts > 1_700_000_000_000 for m in users)
    assert assistants and all(isinstance(m.ts, int) and m.ts > 1_700_000_000_000
                             for m in assistants)
    # system 消息不打时间戳（不是气泡）
    system = next(m for m in snap.messages if m.role == "system")
    assert system.ts is None
    # 出网负载不含 ts（落盘有、wire 无）
    assert sent, "应至少发生一次 LLM 调用"
    assert all("ts" not in m for m in sent[0])
    assert any("ts" in m.to_dict() for m in snap.messages), "落盘必须保留 ts（wire 与落盘分离）"
