"""用户提问门：Agent 向用户提问，等待回答（类似 ApprovalGate 但支持选项+自定义输入）。"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("litework.question")


class QuestionGate:
    def __init__(self, timeout_seconds: float = 600.0) -> None:
        self.timeout_seconds = timeout_seconds
        self._ids = itertools.count(1)
        self._pending: Dict[str, Dict[str, Any]] = {}

    def request(self, question: str, options: Optional[List[str]] = None) -> asyncio.Future:
        """挂起等待用户回答，返回 future（await 后得到用户回答字符串）。"""
        qid = f"q_{next(self._ids)}"
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[qid] = {
            "id": qid,
            "question": question,
            "options": options or [],
            "created_at": int(time.time() * 1000),
            "future": future,
        }

        # 超时保护：超过时限未回答，自动返回超时提示
        async def _timeout_guard() -> None:
            await asyncio.sleep(self.timeout_seconds)
            if not future.done():
                logger.warning("[Question] %s 等待回答超时，自动返回超时提示", qid)
                self.resolve(qid, "[Timeout] 等待回答超时", by="timeout")

        asyncio.ensure_future(_timeout_guard())
        return future

    def current_id(self, future: asyncio.Future) -> str:
        """根据 future 反查问题 ID（用于事件广播）。"""
        for qid, entry in self._pending.items():
            if entry["future"] is future:
                return qid
        return ""

    def resolve(self, qid: str, answer: str, by: str = "user") -> bool:
        """用户给出回答，释放等待的 future。"""
        entry = self._pending.pop(qid, None)
        if entry is None:
            return False
        entry["resolved_by"] = by
        entry["answer"] = answer
        if not entry["future"].done():
            entry["future"].set_result(answer)
        return True

    def get_pending_info(self, qid: str) -> Optional[Dict[str, Any]]:
        entry = self._pending.get(qid)
        if entry is None:
            return None
        return {
            "id": entry["id"],
            "question": entry["question"],
            "options": list(entry["options"]),
            "created_at": entry["created_at"],
        }

    def pending_count(self) -> int:
        return len(self._pending)