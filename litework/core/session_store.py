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


class SessionSnapshot:
    def __init__(
        self,
        session_id: str,
        messages: List[Message],
        metadata: Dict[str, Any],
        created_at: int,
        updated_at: int,
    ) -> None:
        self.session_id = session_id
        self.messages = messages
        self.metadata = metadata
        self.created_at = created_at
        self.updated_at = updated_at

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
            "messages": [m.to_dict() for m in self.messages],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionSnapshot":
        return cls(
            session_id=data.get("session_id", ""),
            messages=[Message.from_dict(m) for m in data.get("messages", [])],
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at", 0),
            updated_at=data.get("updated_at", 0),
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

    def _file_path(self, session_id: str) -> str:
        safe_id = session_id.replace("/", "_").replace("\\", "_")
        return os.path.join(self.storage_dir, f"{safe_id}.json")

    def save(self, session_id: str, messages: List[Message], metadata: Optional[Dict[str, Any]] = None) -> None:
        existing = self.load(session_id)
        snapshot = SessionSnapshot(
            session_id=session_id,
            messages=messages,
            metadata=metadata or {},
            created_at=existing.created_at if existing else int(__import__("time").time() * 1000),
            updated_at=int(__import__("time").time() * 1000),
        )
        # 原子写盘：先写临时文件再 rename，防止中途崩溃产生半截 JSON
        path = self._file_path(session_id)
        fd, tmp_path = tempfile.mkstemp(dir=self.storage_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(snapshot.to_dict(), f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

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

    def list(self) -> List[Dict[str, Any]]:
        """会话快照列表（未变的文件复用缓存解析结果）。

        返回值是浅拷贝：调用方可以随意在顶层加字段，不会污染缓存（嵌套的
        messages/metadata 仍共享——请勿就地修改，列表接口本就契约只读）。
        """
        snapshots: List[Dict[str, Any]] = []
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
                snapshots.append(hit[1])
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                logger.exception("[SessionStore] Error reading session file %s", fname)
                continue
            if not isinstance(data, dict):
                continue
            self._list_cache[fname] = (key, data)
            snapshots.append(data)
        # 清掉已删除会话的缓存条目，避免缓存无界增长
        for gone in [k for k in self._list_cache if k not in live]:
            self._list_cache.pop(gone, None)
        snapshots.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
        return [dict(s) for s in snapshots]

    def delete(self, session_id: str) -> bool:
        path = self._file_path(session_id)
        if os.path.exists(path):
            os.unlink(path)
            return True
        return False

    def update_metadata(self, session_id: str, updates: Dict[str, Any]) -> Optional[SessionSnapshot]:
        """原子更新会话元数据，保留消息与创建时间。"""
        snapshot = self.load(session_id)
        if snapshot is None:
            return None
        metadata = {**snapshot.metadata, **updates}
        self.save(session_id, snapshot.messages, metadata)
        return self.load(session_id)

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
