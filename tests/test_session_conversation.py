# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""会话级 conversation_id 测试：按 (会话 × 供应商) 惰性生成、复用、跨重启稳定、隔离。"""
from __future__ import annotations

from pathlib import Path

from litework.core.session_store import SessionStore
from litework.core.types import Message


def _save_session(store: SessionStore, sid: str) -> None:
    store.save(sid, [Message(role="user", content="hello")], {})


def test_get_or_create_conversation_id_stable_across_calls(tmp_path: Path):
    store = SessionStore(str(tmp_path / "sessions"))
    sid = "session_test_1"
    _save_session(store, sid)

    first = store.get_or_create_conversation_id(sid, "custom_opencode")
    second = store.get_or_create_conversation_id(sid, "custom_opencode")

    assert first
    assert second == first


def test_get_or_create_conversation_id_per_provider_isolation(tmp_path: Path):
    store = SessionStore(str(tmp_path / "sessions"))
    sid = "session_test_2"
    _save_session(store, sid)

    id_a = store.get_or_create_conversation_id(sid, "openai")
    id_b = store.get_or_create_conversation_id(sid, "custom_opencode")

    assert id_a != id_b
    # 同一供应商跨不同会话也应隔离
    other = store.get_or_create_conversation_id("session_test_other", "openai")
    assert other != id_a


def test_get_or_create_conversation_id_survives_restart(tmp_path: Path):
    dir = str(tmp_path / "sessions")
    sid = "session_test_3"
    store1 = SessionStore(dir)
    _save_session(store1, sid)
    cid = store1.get_or_create_conversation_id(sid, "opencode")

    store2 = SessionStore(dir)  # 模拟重启：新实例读同一磁盘
    loaded = store2.get_or_create_conversation_id(sid, "opencode")
    assert loaded == cid


def test_get_or_create_conversation_id_empty_inputs(tmp_path: Path):
    store = SessionStore(str(tmp_path / "sessions"))
    _save_session(store, "session_test_4")

    assert store.get_or_create_conversation_id("", "opencode") == ""
    assert store.get_or_create_conversation_id("session_test_4", "") == ""
    assert store.get_or_create_conversation_id("session_missing", "opencode") == ""
