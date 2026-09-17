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


# ---------------------------------------------------------------- 列表缓存（B）

def test_list_cache_reuses_parsed_snapshot(tmp_path: Path):
    """列表缓存：文件未变时复用已解析结果（不是每次重新读盘解析）。"""
    store = SessionStore(str(tmp_path / "sessions"))
    _save_session(store, "session_cache_1")

    assert len(store.list()) == 1
    # 直接改缓存内部对象：若第二次 list 复用缓存，就会看到这个改动
    assert len(store._list_cache) == 1
    cached = next(iter(store._list_cache.values()))[1]
    cached["updated_at"] = 424242
    rows = store.list()
    assert rows[0]["updated_at"] == 424242, "未复用缓存（每次都在重新解析）"


def test_list_cache_invalidates_on_save_delete_create(tmp_path: Path):
    """保存/删除/新建会话后，列表必须立即反映（不得返回陈旧数据）。"""
    store = SessionStore(str(tmp_path / "sessions"))
    _save_session(store, "session_a")
    assert {s["session_id"] for s in store.list()} == {"session_a"}

    # 新建 → 立即出现
    _save_session(store, "session_b")
    assert {s["session_id"] for s in store.list()} == {"session_a", "session_b"}

    # 保存已有会话（追加消息）→ 立即反映（靠 inode 变化失效，与 mtime 粒度无关）
    store.save("session_a", [
        Message(role="user", content="hello"),
        Message(role="assistant", content="world"),
    ], {})
    row = {s["session_id"]: s for s in store.list()}["session_a"]
    assert len(row["messages"]) == 2, f"追加消息未反映: {row}"

    # 删除 → 立即消失，且缓存条目被清理
    assert store.delete("session_b") is True
    assert {s["session_id"] for s in store.list()} == {"session_a"}
    assert len(store._list_cache) == 1


def test_list_returns_shallow_copies(tmp_path: Path):
    """返回值是浅拷贝：调用方在顶层加字段不会污染缓存。"""
    store = SessionStore(str(tmp_path / "sessions"))
    _save_session(store, "session_c")
    rows = store.list()
    rows[0]["extra"] = "调用方塞的"
    rows[0]["updated_at"] = 1
    fresh = store.list()[0]
    assert "extra" not in fresh
    assert fresh["updated_at"] != 1


def test_list_sorted_by_updated_at_desc(tmp_path: Path):
    store = SessionStore(str(tmp_path / "sessions"))
    _save_session(store, "session_old")
    old_path = store._file_path("session_old")
    _save_session(store, "session_new")
    # 手工把 old 的 updated_at 调小（缓存以 inode+mtime 判失效，故改文件后必失效）
    import json as _json
    with open(old_path, "r", encoding="utf-8") as f:
        data = _json.load(f)
    data["updated_at"] = 1
    with open(old_path, "w", encoding="utf-8") as f:
        _json.dump(data, f)
    ids = [s["session_id"] for s in store.list()]
    assert ids == ["session_new", "session_old"], ids
