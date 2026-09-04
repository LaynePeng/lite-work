"""死循环 / 震荡检测（对应课程第2课 AgentStateTracker）。

工具调用哈希追踪：若连续 3 次以完全相同的参数调用同一个工具，判定陷入死循环。
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import List

logger = logging.getLogger("litework.state")


class AgentStatus(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED_MAX_TURNS = "FAILED_MAX_TURNS"
    FAILED_LOOP_DETECTED = "FAILED_LOOP_DETECTED"
    FAILED_BUDGET_EXCEEDED = "FAILED_BUDGET_EXCEEDED"
    STOPPED = "STOPPED"


class AgentStateTracker:
    def __init__(self, loop_threshold: int = 3) -> None:
        self.loop_threshold = loop_threshold
        self.history_action_hashes: List[str] = []
        self.status: AgentStatus = AgentStatus.IDLE

    def register_and_check_loop(self, tool_name: str, args_str: str) -> bool:
        action_hash = f"{tool_name}:{args_str.strip()}"
        self.history_action_hashes.append(action_hash)

        if len(self.history_action_hashes) >= self.loop_threshold:
            last = self.history_action_hashes[-self.loop_threshold :]
            if len(set(last)) == 1:
                self.status = AgentStatus.FAILED_LOOP_DETECTED
                logger.warning(
                    '[Harness Defense] Infinite loop detected on tool "%s". Interrupting.', tool_name
                )
                return True
        return False