# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""W10 反思 → 技能草稿：run_reflection 的旁路纪律与落盘行为。

不跑真实 LLM：app.adapter 用假 chat_stream。HOME 隔离到 tmp（草稿落个人区，
不能污染真实 ~/lite-work/personal）。
"""
from __future__ import annotations

import os

import pytest

from litework.core.reflection import REFLECTION_CONFIG_KEY, run_reflection


class _FakeAdapter:
    def __init__(self, reply: str):
        self._reply = reply

    async def chat_stream(self, messages, tools, events):
        return self._reply, [], None


class _FakeSessionStore:
    def __init__(self, messages):
        self.messages = messages


class _Snapshot:
    def __init__(self, messages, metadata):
        self.messages = messages
        self.metadata = metadata


class _FakeApp:
    def __init__(self, reply, messages=None, metadata=None, enabled=True):
        self.adapter = _FakeAdapter(reply)
        self.config = {REFLECTION_CONFIG_KEY: enabled}
        self.session_store = _FakeSessionStore(messages or [])
        self._snapshot = _Snapshot(messages or [], metadata or {})
        self.config_dir = "/tmp/unused"

    # session_store.load 的 duck-typing 替身
    def _load(self, sid):
        return self._snapshot


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setenv("HOME", str(fake))
    # reflection 模块在函数内 import .personal——模块级常量需重算
    import importlib
    from litework.core import personal, reflection
    importlib.reload(personal)
    importlib.reload(reflection)
    yield fake
    monkeypatch.undo()
    importlib.reload(personal)
    importlib.reload(reflection)


async def test_reflection_writes_skill_draft(isolated_home):
    """开启反思 + LLM 返回草稿 → SKILL.md 落个人区 skill-drafts，带溯源头。"""
    from litework.core.personal import PERSONAL_WORKSPACE

    from litework.core.types import Message

    app = _FakeApp(
        "# 技能：季度报告汇总\n\n1. 收集数据\n2. 写正文\n3. 排版",
        messages=[Message(role="user", content="做季度报告汇总")],
        metadata={"goal": "季度报告汇总"},
    )
    app.session_store.load = app._load

    path = await run_reflection(app, "session_reflect_1", goal="季度报告汇总")
    assert path is not None
    assert path.startswith(os.path.join(PERSONAL_WORKSPACE, "skill-drafts"))
    content = open(path, encoding="utf-8").read()
    assert "季度报告汇总" in content
    assert "session_reflect_1" in content  # 溯源头
    assert "审阅后" in content  # 采纳指引


async def test_reflection_no_skill_writes_nothing(isolated_home):
    """LLM 判定 NO_SKILL → 不落盘。"""
    from litework.core.personal import PERSONAL_WORKSPACE

    app = _FakeApp("NO_SKILL")
    app.session_store.load = app._load
    path = await run_reflection(app, "session_reflect_2", goal="琐碎任务")
    assert path is None
    assert not os.path.exists(os.path.join(PERSONAL_WORKSPACE, "skill-drafts"))


async def test_reflection_disabled_by_default(isolated_home):
    """reflection_enabled=False（默认）→ 直接返回 None，不调 LLM。"""
    app = _FakeApp("should not be called", enabled=False)
    app.session_store.load = app._load
    assert await run_reflection(app, "session_reflect_3") is None


async def test_reflection_silent_on_llm_failure(isolated_home):
    """LLM 抛异常 → 静默返回 None（旁路纪律：不阻塞任务收尾）。"""
    class _Boom(_FakeAdapter):
        async def chat_stream(self, messages, tools, events):
            raise RuntimeError("LLM down")

    app = _FakeApp("x")
    app.adapter = _Boom("x")
    app.session_store.load = app._load
    assert await run_reflection(app, "session_reflect_4") is None


async def test_reflection_falls_back_to_messages(isolated_home):
    """无轨迹 → 会话消息尾部退化（FakeStore 无轨迹目录即走 fallback）。"""
    from litework.core.personal import PERSONAL_WORKSPACE
    from litework.core.types import Message

    app = _FakeApp(
        "# 技能：照片整理\n\n1. 盘点来源目录",
        messages=[
            Message(role="user", content="整理照片"),
            Message(role="assistant", content="已完成整理"),
        ],
        metadata={"goal": "整理照片"},
    )
    app.session_store.load = app._load
    path = await run_reflection(app, "session_reflect_5", goal="整理照片")
    assert path is not None
    content = open(path, encoding="utf-8").read()
    assert "照片整理" in content
    assert "素材 messages" in content  # 溯源头标注退化来源
