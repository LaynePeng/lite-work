# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""子 Agent 信箱的持久化（append-only JSONL）。

**解决什么**：消息此前只存在三处内存里——

- `AgentLoop.agent_inbox`（`deque`，运行中直达）
- `AgentRecord.pending_inbox`（`List`，启动窗口期）
- `AgentRecord.mailbox`（`List`，不在运行时滞留）

进程一退全部丢失，且没有幂等键：重试可能重复投递。本模块把它们收敛为
**一条 append-only 日志 + 消费标记**，重启后可回放。

**为什么是 JSONL 而不是 SQLite**：信箱天然是「追加 + 标记已消费」的
模式，命中 `core/jsonl_io.py` 的既有能力（追加 / 轮转 / 一切 OSError
静默），零新依赖。真正的审计在会话历史里，信箱只需要「不丢、不重」。

**与 SessionAgentManager 的职责边界**：本模块只管「一行消息怎么落盘、
怎么读回、怎么标已消费」；投递时机（运行中直达 / 窗口期暂存 / 沉睡滞留）
仍由 manager 决定——那是业务语义，不该埋进存储层。

**失败语义**：一切 IO 异常静默降级为「内存模式」（调用方拿到的仍是可用
的列表），绝不因为信箱写盘失败而阻断 agent 投递或任务执行。
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.jsonl_io import append_jsonl, read_jsonl

#: 单个会话信箱文件的轮转上限。信箱是短生命周期数据（消费即可丢），
#: 2MB 足够容纳大量未消费消息；轮转后旧文件保留作为排查线索。
MAILBOX_MAX_BYTES = 2 * 1024 * 1024

#: 已消费记录的保留上限（行）。超过就压缩重写，只留未消费的。
#: 不压缩会让「常开的会话」信箱无限增长——每轮都会新增消费记录。
COMPACT_THRESHOLD = 500


def _now() -> float:
    return time.time()


class AgentMailbox:
    """一个会话的 agent 信箱（落盘 JSONL + 内存索引）。

    文件位置：`<config_dir>/agent_mail/<session_id>.jsonl`
    （session_id 经 `_safe` 归一，防路径穿越）

    行格式::

        {"seq": "<单调 id>", "to": "<agent_id>", "sender": "<who>",
         "text": "...", "at": 1234.5, "consumed_at": 1234.9}

    未消费 = 没有 `consumed_at`。消费只追加一条同 seq 的新行（last-write-wins），
    不原地改——这才叫 append-only，崩溃时最多多一行，不会写出半条。
    """

    def __init__(self, config_dir: str, session_id: str) -> None:
        self.session_id = session_id
        safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in session_id)[:80]
        self.path = Path(config_dir) / "agent_mail" / f"{safe}.jsonl"
        self._rows: List[Dict[str, Any]] = []
        self._loaded = False
        #: 上次压缩时的行数基线（避免每次都扫全量判断是否该压缩）
        self._ops_since_compact = 0

    # ------------------------------------------------------------------ 读写

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        rows = read_jsonl(self.path)
        # last-write-wins 归并：同一 seq 后出现的行覆盖先出现的
        merged: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for row in rows:
            seq = str(row.get("seq") or "")
            if not seq:
                continue
            if seq not in merged:
                order.append(seq)
            merged[seq] = row
        self._rows = [merged[s] for s in order]

    def _persist(self, row: Dict[str, Any]) -> None:
        # 失败静默：信箱是旁路可靠性设施，绝不能因为它阻断投递
        append_jsonl(self.path, row, max_bytes=MAILBOX_MAX_BYTES)
        self._ops_since_compact += 1

    # ------------------------------------------------------------------ 投递

    def deliver(self, to: str, text: str, sender: str = "main",
                msg_id: Optional[str] = None) -> str:
        """投递一条消息，返回其 seq。

        `msg_id` 提供幂等：同一 msg_id 重复投递只保留第一条（重试不会让
        agent 收到两条相同消息）。不传则生成新 seq。
        """
        self._load()
        seq = msg_id or f"{uuid.uuid4().hex[:12]}"
        # 幂等：已存在同一 seq 且未消费 → 直接复用，不重复追加
        for row in self._rows:
            if str(row.get("seq")) == seq and not row.get("consumed_at"):
                return seq
        row = {"seq": seq, "to": to, "sender": sender, "text": text, "at": _now()}
        self._rows.append(row)
        self._persist(row)
        return seq

    def peek(self, to: str) -> List[Dict[str, Any]]:
        """该 agent 尚未消费的消息（按投递顺序）。"""
        self._load()
        return [r for r in self._rows
                if str(r.get("to")) == to and not r.get("consumed_at")]

    def mark_consumed(self, seq: str) -> bool:
        """按 seq 标记一条消息已消费（运行中直达的场景：已交给 loop）。"""
        self._load()
        for row in self._rows:
            if str(row.get("seq")) == seq:
                if row.get("consumed_at"):
                    return False
                self._consume(row)
                return True
        return False

    def take(self, to: str) -> List[Dict[str, Any]]:
        """取出并标记已消费（返回取出前的内容，供并入上下文）。"""
        self._load()
        rows = self.peek(to)
        for row in rows:
            self._consume(row)
        return rows

    def _consume(self, row: Dict[str, Any]) -> None:
        row["consumed_at"] = _now()
        # 追加一条「已消费」记录（不原地改文件）
        self._persist({
            "seq": row.get("seq"), "to": row.get("to"),
            "sender": row.get("sender"), "text": "",
            "at": row.get("at"), "consumed_at": row["consumed_at"],
        })

    # ------------------------------------------------------------------ 维护

    def pending_count(self, to: Optional[str] = None) -> int:
        self._load()
        return len([r for r in self._rows
                    if not r.get("consumed_at")
                    and (to is None or str(r.get("to")) == to)])

    def compact(self) -> int:
        """压缩日志：只保留未消费记录，返回压缩前行数。

        不压缩的话，「常开会话 + 高频互发」会让文件单调增长——每轮投递都
        追加消费记录。压缩是原地重写（原子 rename），只在超过阈值时触发。
        """
        self._load()
        live = [r for r in self._rows if not r.get("consumed_at")]
        before = len(self._rows)
        if before <= COMPACT_THRESHOLD and not live:
            return before
        self._rows = live
        self._ops_since_compact = 0
        tmp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            import json
            import os

            tmp.write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in live),
                encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
        return before

    def maybe_compact(self) -> None:
        """超过阈值才压缩（调用方在投递路径上顺手调用即可）。"""
        if self._ops_since_compact >= COMPACT_THRESHOLD:
            self.compact()
