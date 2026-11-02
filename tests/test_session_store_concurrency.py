# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""SessionStore 并发写与元数据更新的数据安全问题。

回归背景（单 Core 多客户端 / 双后端共用一个 ~/.lite-work）：

`update_metadata` 是read-modify-write——先 `load()` 把整份快照（含全部
messages）读进内存，改几个元数据键，再 `save()` 整份写回。如果这中间有
别的执行者（另一个后端进程、或同进程里跑 AgentLoop 的任务）已经把新消息
落盘，这次写回就会**用旧消息覆盖新消息**——元数据只改了一个键，代价却是
整段对话历史回退。

`save()` 本身虽是原子写（临时文件 + os.replace），但原子性只保证「不产生
半截文件」，不保证「不丢别人的更新」。这两件事必须分开看。
"""
from __future__ import annotations

import json
import os

import pytest

from litework.core.session_store import SessionStore, SessionConflictError
from litework.core.types import Message


@pytest.fixture
def store(tmp_path):
    return SessionStore(str(tmp_path / "sessions"))


def _user(text: str) -> Message:
    return Message(role="user", content=text)


def _raw(store: SessionStore, sid: str) -> dict:
    with open(store._file_path(sid), "r", encoding="utf-8") as f:
        return json.load(f)


def test_update_metadata_keeps_messages_written_concurrently(store, monkeypatch):
    """元数据更新不得复活旧消息（覆盖别人在 load 与 save 之间写入的内容）。

    竞态窗口就是 load 与 save 之间：必须真的在那个时刻插入一次外部写入，
    否则读到的就是最新文件、覆盖问题被掩盖。
    """
    sid = "sess-1"
    store.save(sid, [_user("第一条")], {"a": 1})
    created = _raw(store, sid)["created_at"]

    real_load = store.load
    injected = {"done": False}

    def load_then_race(session_id):
        snap = real_load(session_id)
        if not injected["done"]:
            injected["done"] = True
            # 模拟：另一个执行者在本端 load 之后、save 之前落盘了新消息
            store.save(sid, [_user("第一条"), _user("第二条（并发写入）")],
                       {"a": 1}, created_at=created)
        return snap

    monkeypatch.setattr(store, "load", load_then_race)
    store.update_metadata(sid, {"b": 2})
    monkeypatch.undo()

    snap = store.load(sid)
    assert snap is not None
    contents = [m.content for m in snap.messages]
    assert "第二条（并发写入）" in contents, "元数据更新把并发写入的消息覆盖掉了"
    assert contents == ["第一条", "第二条（并发写入）"]
    assert snap.metadata == {"a": 1, "b": 2}


def test_update_metadata_preserves_created_at(store):
    """docstring 承诺「保留创建时间」，实际必须真的保留。"""
    sid = "sess-2"
    store.save(sid, [_user("hi")], {})
    old_created = _raw(store, sid)["created_at"]

    # 让 save 里的默认时间戳与旧值必然不同（否则同毫秒测不出来）
    original = old_created - 5000
    snap = store.load(sid)
    assert snap is not None
    store.save(sid, snap.messages, snap.metadata, created_at=original)

    store.update_metadata(sid, {"x": 1})
    assert _raw(store, sid)["created_at"] == original


def test_update_metadata_merges_without_clobbering_concurrent_metadata(store):
    """两个执行者各改不同元数据键，结果必须都保留。"""
    sid = "sess-3"
    store.save(sid, [_user("q")], {"keep": "me"})
    created = _raw(store, sid)["created_at"]

    # 另一端写了 last_agent_id（模拟另一进程）
    store.save(sid, [_user("q")], {"keep": "me", "last_agent_id": "build"},
               created_at=created)

    # 本端只加自己的键
    store.update_metadata(sid, {"todos": ["t1"]})

    meta = store.load(sid).metadata
    assert meta == {"keep": "me", "last_agent_id": "build", "todos": ["t1"]}


def test_update_metadata_missing_session_returns_none(store):
    """会话不存在时返回 None（调用方据此判断，不要静默新建）。"""
    assert store.update_metadata("nope", {"a": 1}) is None
    assert not os.path.exists(store._file_path("nope"))


def test_update_metadata_none_removes_key(store):
    """value=None 表示删除该键（清除自定义名等场景），不是写 null。"""
    sid = "sess-5"
    store.save(sid, [_user("q")], {"name": "旧名字", "pinned": True})

    store.update_metadata(sid, {"name": None})

    meta = store.load(sid).metadata
    assert "name" not in meta, "None 应删除键，而不是留下 name: null"
    assert meta.get("pinned") is True, "其它键不应受影响"


def test_update_metadata_removing_absent_key_is_noop(store):
    """删除不存在的键不该报错，也不该创建 null。"""
    sid = "sess-6"
    store.save(sid, [_user("q")], {"a": 1})
    store.update_metadata(sid, {"ghost": None})
    assert store.load(sid).metadata == {"a": 1}


def test_save_then_update_metadata_keeps_updated_at_monotonic(store):
    """updated_at 必须随写入推进（列表排序依赖它）。"""
    sid = "sess-4"
    store.save(sid, [_user("a")], {})
    first = _raw(store, sid)["updated_at"]
    store.update_metadata(sid, {"k": "v"})
    second = _raw(store, sid)["updated_at"]
    assert second >= first


# ---------------------------------------------------------------- 乐观锁

def test_save_increments_version(store):
    """每次成功写盘版本号 +1，且能从磁盘读回。"""
    sid = "v-1"
    v1 = store.save(sid, [_user("a")], {})
    v2 = store.save(sid, [_user("b")], {})
    assert (v1, v2) == (1, 2)
    assert store.load(sid).version == 2


def test_legacy_file_without_version_reads_as_zero(store):
    """老会话文件没有 version 字段 → 读成 0，不需要数据迁移。"""
    sid = "v-legacy"
    import json as _json
    path = store._file_path(sid)
    with open(path, "w", encoding="utf-8") as f:
        _json.dump({"session_id": sid, "created_at": 1, "updated_at": 2,
                    "metadata": {}, "messages": []}, f)
    assert store.load(sid).version == 0
    # 首次保存后从 1 起算
    assert store.save(sid, [_user("x")], {}) == 1


def test_save_with_stale_expected_version_raises(store):
    """版本过期的写入必须拒绝，**不能**静默覆盖别人刚写的内容。"""
    sid = "v-conflict"
    store.save(sid, [_user("原始")], {})          # version=1
    stale = store.load(sid).version              # 读到 1

    # 另一个执行者写入（version → 2）
    store.save(sid, [_user("原始"), _user("别人的新消息")], {})

    with pytest.raises(SessionConflictError) as exc:
        store.save(sid, [_user("原始")], {}, expected_version=stale)
    assert exc.value.expected == 1
    assert exc.value.actual == 2

    # 关键：磁盘内容仍是别人的，未被回退
    contents = [m.content for m in store.load(sid).messages]
    assert "别人的新消息" in contents


def test_save_with_matching_expected_version_succeeds(store):
    """版本匹配时正常写入并递增。"""
    sid = "v-ok"
    store.save(sid, [_user("a")], {})
    cur = store.load(sid)
    new_version = store.save(sid, cur.messages + [_user("b")], cur.metadata,
                              expected_version=cur.version)
    assert new_version == cur.version + 1


def test_update_metadata_retries_on_conflict(store, monkeypatch):
    """update_metadata 撞到冲突应重读重试，最终成功且不丢别人的消息。"""
    sid = "v-retry"
    store.save(sid, [_user("原始")], {"a": 1})

    real_save = store.save
    calls = {"n": 0}

    def save_with_race(session_id, messages, metadata=None, created_at=None,
                       expected_version=None):
        # 第一次尝试前，模拟别人抢先写入（让本次 expected_version 过期）
        if calls["n"] == 0:
            calls["n"] += 1
            real_save(session_id, [_user("原始"), _user("抢先写入")], {"a": 1})
        return real_save(session_id, messages, metadata,
                         created_at=created_at, expected_version=expected_version)

    monkeypatch.setattr(store, "save", save_with_race)
    store.update_metadata(sid, {"b": 2})
    monkeypatch.undo()

    snap = store.load(sid)
    assert [m.content for m in snap.messages] == ["原始", "抢先写入"]
    assert snap.metadata == {"a": 1, "b": 2}


def test_delete_clears_version_cache(store):
    """删除后同 id 重建：版本号从头开始，不能沿用旧缓存。"""
    sid = "v-del"
    store.save(sid, [_user("a")], {})
    store.save(sid, [_user("b")], {})
    assert store.load(sid).version == 2

    store.delete(sid)
    assert store.load(sid) is None
    assert store.save(sid, [_user("新")], {}) == 1
