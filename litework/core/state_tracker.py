# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""死循环 / 震荡检测（对应课程第2课 AgentStateTracker）。

工具调用哈希追踪：连续 N 次以完全相同的参数调用同一个工具，判定陷入死循环。

阈值分级（v1.7）：只读/幂等工具（读取同一文件、重复搜索）是模型复查的常见
形态，3 次误杀正常任务；有副作用的工具（写入/命令/git 提交）维持严格阈值。
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


# 只读/幂等工具：连续重复调用无害（复查、缓存比对），阈值放宽
READONLY_TOOLS = frozenset({
    "read_file", "list_dir", "file_tree", "search_code",
    "get_file_outline", "read_focused_symbol",
    "git_status", "git_diff", "git_log", "git_branch",
    "review_code", "webfetch", "webfetch_batch", "load_skill",
})
READONLY_LOOP_THRESHOLD = 6
# 有副作用/未知工具：严格阈值（含 MCP 等动态工具，安全默认）
STRICT_LOOP_THRESHOLD = 3


class AgentStateTracker:
    def __init__(self, loop_threshold: int = 3) -> None:
        self.strict_threshold = loop_threshold
        self.readonly_threshold = max(loop_threshold, READONLY_LOOP_THRESHOLD)
        self.history_action_hashes: List[str] = []
        self.status: AgentStatus = AgentStatus.IDLE

    def _threshold_for(self, tool_name: str) -> int:
        return (self.readonly_threshold if tool_name in READONLY_TOOLS
                else self.strict_threshold)

    def register_and_check_loop(self, tool_name: str, args_str: str) -> bool:
        threshold = self._threshold_for(tool_name)
        action_hash = f"{tool_name}:{args_str.strip()}"
        self.history_action_hashes.append(action_hash)

        if len(self.history_action_hashes) >= threshold:
            last = self.history_action_hashes[-threshold:]
            if len(set(last)) == 1:
                self.status = AgentStatus.FAILED_LOOP_DETECTED
                logger.warning(
                    '[Harness Defense] Infinite loop detected on tool "%s". Interrupting.', tool_name
                )
                return True
        return False