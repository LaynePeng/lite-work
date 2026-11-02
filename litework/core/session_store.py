# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""会话持久化（对应课程第10课（插件架构） SessionStore，增强：列表/删除/备份安全写盘）。"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .types import Message

logger = logging.getLogger("litework.session")


class SessionConflictError(Exception):
    """会话已被别人更新（乐观锁冲突）。

    多客户端连同一 Core、或两端共用同一个 ~/.lite-work 时，同一 session 可能
    被两边同时持有。整份快照写回是「后写者通吃」——另一边刚存的消息会被
    静默覆盖，用户看到的是对话凭空回退。

    这里不做静默覆盖，也不做静默合并（合并两个 Agent 的不同推理过程只会
    产生自相矛盾的历史），而是把冲突**显式暴露**给调用方，由它决定重试、
    放弃还是提示用户。静默丢数据是这一类 bug 最糟的形态：用户没有任何线索。
    """

    def __init__(self, session_id: str, expected: Optional[int], actual: Optional[int]) -> None:
        super().__init__(
            f"会话 {session_id} 已被其他进程更新（期望版本 {expected}，磁盘版本 {actual}）"
        )
        self.session_id = session_id
        self.expected = expected
        self.actual = actual


def _tail_activity_summary(messages: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """消息尾部倒扫：最后一个工具调用（含成败）或助手文本 → 最近活动摘要。

    与 server/routers/sessions.py 的 _last_activity 同构，但这里是纯 dict
    提取（server 侧消费 SessionStore 摘要，不再自己扫 messages）。
    """
    tail_results: Dict[str, bool] = {}
    for m in reversed(messages):
        role = m.get("role")
        if role == "tool":
            content = str(m.get("content") or "")
            tail_results[str(m.get("tool_call_id") or "")] = not (
                content.startswith("[Error]") or content.startswith("[Execution Exception]"))
            continue
        if role == "assistant":
            calls = m.get("tool_calls") or []
            if calls:
                call = calls[-1]
                tc = call.get("function", {}) if isinstance(call, dict) else {}
                name = str(tc.get("name") or "tool")
                try:
                    import json as _json
                    args_raw = tc.get("arguments") or ""
                    args = _json.loads(args_raw) if isinstance(args_raw, str) else {}
                    key = next((str(args.get(k)) for k in
                                ("filePath", "filename", "path", "query", "command", "url", "name")
                                if args.get(k)), "")
                except Exception:
                    key = ""
                ok = tail_results.get(str(call.get("id") or ""))
                return {"summary": f"{name} {key[:40]}".strip(), "ok": ok, "tool": name}
            content = str(m.get("content") or "").strip()
            if content:
                return {"summary": content[:60], "ok": None, "tool": ""}
    return None


class SessionSnapshot:
    def __init__(
        self,
        session_id: str,
        messages: List[Message],
        metadata: Dict[str, Any],
        created_at: int,
        updated_at: int,
        version: int = 0,
    ) -> None:
        self.session_id = session_id
        self.messages = messages
        self.metadata = metadata
        self.created_at = created_at
        self.updated_at = updated_at
        # 乐观锁版本号：每次成功写盘 +1。旧文件没有该字段 → 读成 0，
        # 首次写入后变为 1，因此不需要数据迁移。
        self.version = version

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "version": self.version,
            "metadata": self.metadata,
            "messages": [m.to_dict() for m in self.messages],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionSnapshot":
        raw_version = data.get("version", 0)
        try:
            version = int(raw_version)
        except (TypeError, ValueError):
            version = 0
        return cls(
            session_id=data.get("session_id", ""),
            messages=[Message.from_dict(m) for m in data.get("messages", [])],
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at", 0),
            updated_at=data.get("updated_at", 0),
            version=version,
        )


class SessionStore:
    def __init__(self, storage_dir: str = "./.lite-work/sessions") -> None:
        self.storage_dir = os.path.abspath(storage_dir)
        os.makedirs(self.storage_dir, exist_ok=True)
        # 会话列表缓存：文件名 → ((inode, mtime_ns, size), 解析后的 JSON)。
        # `list()` 要读取并解析**每个**会话文件（含完整消息历史），会话一多纯属浪费。
        # 键含 inode：`save()` 走 os.replace 写临时文件，每次保存都是新 inode，
        # 即使文件系统 mtime 粒度粗或两次保存同毫秒也不会误判为「未变化」。
        # 只读不写盘（不改文件、不排序写回），因此不会引起列表抖动或写竞态。
        self._list_cache: Dict[str, Tuple[Tuple[int, int, int], Dict[str, Any]]] = {}
        # 会话 id → 最近一次本进程写入的版本号。仅用于无条件写时免读盘递增；
        # 乐观锁路径一律读盘比对，不信这个缓存（可能已被别的进程写脏，也会
        # 因 delete 而失效——所以 delete 里要清）
        self._versions: Dict[str, int] = {}

    def _file_path(self, session_id: str) -> str:
        safe_id = session_id.replace("/", "_").replace("\\", "_")
        return os.path.join(self.storage_dir, f"{safe_id}.json")

    def save(self, session_id: str, messages: List[Message], metadata: Optional[Dict[str, Any]] = None,
             created_at: Optional[int] = None,
             expected_version: Optional[int] = None) -> int:
        """保存会话（原子写盘），返回写入后的新版本号。

        created_at 由调用方显式传入；不传时不再读旧文件回填（旧实现每次
        save 全量 load 一次，大会话纯浪费 IO——调用链里 AgentLoop 每次
        落盘都走这里，3300+ 条消息的会话每轮重复解析 3MB JSON）。
        需要保留原 created_at 的调用方用 `update_metadata`（那里本来就要 load）。

        expected_version：乐观锁。传了就做比较写入——磁盘上的版本与它不一致
        就抛 `SessionConflictError`，**不覆盖**。不传 = 无条件写（保持既有
        调用方语义，多数场景只有一个执行者在写该会话）。

        为什么默认不比较：AgentLoop 每轮都落盘同一个会话，让它全程持有版本号
        会把「正常连续写」也变成冲突。真正需要保护的是**跨进程/跨客户端**
        这类长间隔写入，由那些调用点显式传 expected_version。
        """
        now_ms = int(__import__("time").time() * 1000)
        path = self._file_path(session_id)
        if expected_version is not None:
            # 乐观锁必须读盘比对：进程内缓存可能已被别的进程写脏
            current = self.load(session_id)
            actual = current.version if current is not None else 0
            if actual != expected_version:
                raise SessionConflictError(session_id, expected_version, actual)
            version = actual + 1
        else:
            # 无条件写：优先用内存版本号递增，**不读盘**——AgentLoop 每轮都
            # 落盘，大会话（3300+ 条消息 / 3MB JSON）在这里读一次纯属浪费。
            # 缓存未命中时才读一次文件（首次保存该会话）。
            cached = self._versions.get(session_id)
            if cached is None:
                current = self.load(session_id) if os.path.exists(path) else None
                cached = current.version if current is not None else 0
            else:
                current = None
            version = cached + 1
        created = created_at
        if created is None:
            # 缓存未命中时上面已 load 过（current 有值），直接复用；
            # 命中缓存时说明此前写过该会话，created_at 交给调用方传或不紧要，
            # 这里用本次时间戳即可——真正的「保留创建时间」由 update_metadata 负责。
            created = current.created_at if current is not None and current.created_at else now_ms
        snapshot = SessionSnapshot(
            session_id=session_id,
            messages=messages,
            metadata=metadata or {},
            created_at=created,
            updated_at=now_ms,
            version=version,
        )
        # 原子写盘：先写临时文件再 rename，防止中途崩溃产生半截 JSON
        fd, tmp_path = tempfile.mkstemp(dir=self.storage_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(snapshot.to_dict(), f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
        self._versions[session_id] = version
        return version

    def load(self, session_id: str) -> Optional[SessionSnapshot]:
        path = self._file_path(session_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return SessionSnapshot.from_dict(json.load(f))
        except Exception:
            logger.exception("[SessionStore] Error reading session file %s", session_id)
            return None

    #: list() 缓存的条目上限：会话文件多如牛毛时防缓存无界增长（LRU 淘汰）
    _LIST_CACHE_MAX = 200

    #: update_metadata 的乐观锁重试次数：冲突只可能发生在「读版本→写盘」
    #: 这几毫秒内，重试 3 次足以覆盖正常并发；持续冲突说明有别的进程在
    #: 高频写同一会话，此时抛出比无限重试更诚实。
    METADATA_UPDATE_RETRIES = 3

    @staticmethod
    def _summarize(data: Dict[str, Any]) -> Dict[str, Any]:
        """全量快照 → 轻量列表行（不含 messages 大头）。

        列表消费方（/api/sessions、cleanup、meta 计数）只用到：元数据、
        计数、首条用户消息（标题）、尾部活动摘要。全量 messages 动辄几 MB，
        常驻缓存几十个会话就是上百 MB——只留摘要。
        """
        messages = data.get("messages") or []
        first_user = next(
            (str(m.get("content") or "") for m in messages if m.get("role") == "user"), "")
        return {
            "session_id": data.get("session_id", ""),
            "created_at": data.get("created_at", 0),
            "updated_at": data.get("updated_at", 0),
            "metadata": data.get("metadata", {}) or {},
            "message_count": len(messages),
            "has_user_message": any(m.get("role") == "user" for m in messages),
            "first_user_message": first_user[:200],
            # 尾部活动摘要（与 server _last_activity 同构，但这里是无依赖的
            # 纯提取：最后一个工具调用名 + 首个关键参数，或尾部助手文本）
            "last_activity": _tail_activity_summary(messages),
        }

    def list(self) -> List[Dict[str, Any]]:
        """会话摘要列表（未变的文件复用缓存解析结果，**不含 messages**）。

        返回值是浅拷贝：调用方可以随意在顶层加字段，不会污染缓存。
        需要完整消息历史的调用方用 `load(session_id)` 单个读。
        """
        rows: List[Dict[str, Any]] = []
        try:
            names = os.listdir(self.storage_dir)
        except OSError:
            return []
        live = set()
        for fname in names:
            if not fname.endswith(".json"):
                continue
            live.add(fname)
            path = os.path.join(self.storage_dir, fname)
            try:
                st = os.stat(path)
            except OSError:
                continue
            key = (st.st_ino, st.st_mtime_ns, st.st_size)
            hit = self._list_cache.get(fname)
            if hit is not None and hit[0] == key:
                rows.append(hit[1])
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                logger.exception("[SessionStore] Error reading session file %s", fname)
                continue
            if not isinstance(data, dict):
                continue
            summary = self._summarize(data)
            self._list_cache[fname] = (key, summary)
            rows.append(summary)
        # 清掉已删除会话的缓存条目，避免缓存无界增长
        for gone in [k for k in self._list_cache if k not in live]:
            self._list_cache.pop(gone, None)
        # 超上限时按访问序淘汰（list 每次都重写命中条目实现简易 LRU）
        if len(self._list_cache) > self._LIST_CACHE_MAX:
            for victim in list(self._list_cache)[:len(self._list_cache) - self._LIST_CACHE_MAX]:
                self._list_cache.pop(victim, None)
        rows.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
        return [dict(r) for r in rows]

    def delete(self, session_id: str) -> bool:
        path = self._file_path(session_id)
        # 版本缓存必须同步清掉：否则同一 id 被重建后，无条件写会从旧版本
        # 继续递增，乐观锁的期望值就对不上了
        self._versions.pop(session_id, None)
        if os.path.exists(path):
            os.unlink(path)
            return True
        return False

    def update_metadata(self, session_id: str, updates: Dict[str, Any]) -> Optional[SessionSnapshot]:
        """只更新会话元数据，保留消息与创建时间。

        ⚠️ 这里是 read-modify-write，`load()` 与 `save()` 之间存在竞态窗口：
        若有别的执行者（另一后端进程共用 ~/.lite-work，或同进程里正在跑的
        AgentLoop）在这个窗口内落了新消息，直接 `save()` 整份写回会把它们
        **覆盖回旧版本**——只改一个元数据键，代价却是整段对话历史回退。

        解决方式：**乐观锁**。写盘前重读最新快照，只把 `updates` 合并进它的
        metadata，并以读到的 version 作为 `expected_version` 做比较写入——
        若这几毫秒里又被别人改了，`save()` 抛 `SessionConflictError`，这里
        重读再合并（元数据合并且幂等，重试安全）。

        仍非跨进程原子（那需要文件锁），但保证：**元数据更新永远不会让消息
        历史倒退**，且冲突不会静默吞掉。
        """
        for attempt in range(self.METADATA_UPDATE_RETRIES):
            latest = self.load(session_id)
            if latest is None:  # 不存在或期间被删除
                return None
            metadata = {**latest.metadata}
            for key, value in updates.items():
                # 约定：value=None 表示「删除该键」（不是写 null）——调用方需要
                # 清除元数据（如自定义会话名）时用它，避免各自去 load+save 整套快照。
                if value is None:
                    metadata.pop(key, None)
                else:
                    metadata[key] = value
            try:
                self.save(
                    session_id,
                    latest.messages,
                    metadata,
                    created_at=latest.created_at,
                    expected_version=latest.version,
                )
                return self.load(session_id)
            except SessionConflictError:
                # 恰好在这几毫秒里被别人写了：重读再合并一次。
                # 元数据合并是幂等的（只覆盖本函数负责的键），重试安全。
                if attempt + 1 >= self.METADATA_UPDATE_RETRIES:
                    raise
                continue
        return None

    def get_or_create_conversation_id(self, session_id: str, provider_id: str) -> str:
        """按 (会话 × 供应商) 惰性生成/复用 opaque 会话标识（用于 custom_headers 的 {conversation_id}）。

        同一会话对同一供应商始终返回同一个 UUID（跨重启稳定）；
        不同供应商各自独立，避免第三方通过同一 ID 关联多个网关的行为。
        仅在使用该模板的会话上才会调用，因此不会给所有会话写入元数据。
        """
        if not session_id or not provider_id:
            return ""
        snapshot = self.load(session_id)
        if snapshot is None:
            return ""
        ids = snapshot.metadata.get("conversation_ids")
        if not isinstance(ids, dict):
            ids = {}
        existing = ids.get(provider_id)
        if isinstance(existing, str) and existing:
            return existing
        new_id = uuid.uuid4().hex
        metadata = dict(snapshot.metadata)
        metadata["conversation_ids"] = {**ids, provider_id: new_id}
        self.save(session_id, snapshot.messages, metadata)
        return new_id
