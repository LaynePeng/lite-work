# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""应用级通知总线（盯梢/定时的结果推送）。

**为什么不复用任务 SSE**：现有 `/api/tasks/{id}/events` 是**按任务**订阅的
（`server/tasks.py` 里每个 TaskHandle 一个队列）。盯梢通知的时刻由调度器决定
——可能压根没有任务在跑（纯定时、指纹没变、探针失败）。所以要一条与任务无关的
**应用级**通道。

设计要点：

1. **多订阅者**：同一应用可能开着多个窗口（每个窗口一条 SSE）。广播给全部。
2. **历史 + 回放**（`after` 游标）：SSE 会断线重连，重连窗口内产生的通知不能
   丢。每个通知带单调递增 id；重连时带 `after=<最后收到的 id>` 回放缺口。
   这比「重连后拉一次列表」更精确，也不会重复推送。
3. **有界内存**：历史只保留最近 `max_history` 条（默认 200）。这是**展示用**
   的通道，不是审计日志——真正的运行历史在 `schedules.json` 的 runs 里。
4. **不因订阅者异常影响调度**：push() 对每个队列的放入都吞异常（队列满就丢
   该订阅者的这一条，绝不阻塞调度循环）。
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("litework.watch.notify")

#: 历史保留条数。展示通道，够回放一次断线窗口即可。
DEFAULT_MAX_HISTORY = 200

#: 单个订阅者的队列上限。满了说明前端卡住/断连未清理，丢新通知而不是阻塞调度。
SUBSCRIBER_QUEUE_MAX = 64


class WatchNotificationBus:
    """盯梢通知总线：广播 + 历史回放。"""

    def __init__(self, max_history: int = DEFAULT_MAX_HISTORY) -> None:
        self._subs: List[asyncio.Queue] = []
        self._history: List[Dict[str, Any]] = []
        self._seq = itertools.count(1)
        self.max_history = max_history

    # ---- 发布 ----

    def push(self, note: Dict[str, Any]) -> Dict[str, Any]:
        """发布一条通知，返回补全 id/at 后的记录。

        同步方法（调度器在事件循环里调用，不需要 await）——
        订阅队列用 put_nowait：满了就丢这一条，绝不阻塞调度。
        """
        item = dict(note)
        item["id"] = next(self._seq)
        item.setdefault("at", time.time())
        self._history.append(item)
        if len(self._history) > self.max_history:
            self._history = self._history[-self.max_history:]
        dead: List[asyncio.Queue] = []
        for q in list(self._subs):
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                logger.warning("[Watch] 订阅者队列已满，丢弃一条通知（前端可能已断连）")
            except Exception:  # noqa: BLE001
                dead.append(q)
        for q in dead:
            self._unsubscribe(q)
        return item

    # ---- 订阅 ----

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._unsubscribe(q)

    def _unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._subs.remove(q)
        except ValueError:
            pass

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    # ---- 历史 ----

    def recent(self, after: Optional[int] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """取历史通知。`after` 给定时只返回 id 更大的（断线回放用）。"""
        items = self._history
        if after is not None:
            items = [it for it in items if int(it.get("id", 0)) > after]
        if limit > 0:
            items = items[-limit:]
        return [dict(it) for it in items]

    def clear(self) -> None:
        """清空历史（前端「清空通知」用）。不影响已订阅者。"""
        self._history = []
