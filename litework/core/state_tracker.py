# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""死循环 / 震荡检测（对应课程第2课 AgentStateTracker）。

工具调用哈希追踪：连续 N 次以完全相同的参数调用同一个工具，判定陷入死循环。

阈值分级（v1.7）：只读/幂等工具（读取同一文件、重复搜索）是模型复查的常见
形态，3 次误杀正常任务；有副作用的工具（写入/命令/git 提交）维持严格阈值。

滑窗重读检测（v1.7.2，事故驱动）：只比较完整参数串拦不住「窄行窗口滑动重读」
——read_file 1-6、5-7、6-8、7-8……每次参数都不同，精确哈希永不命中，实测可
绕过防御空转十几轮（口头宣告"下一步就编辑"也无效）。按文件维护已读行区间
并集：一次读取 ≥50% 行数落在已读覆盖内即计一次冗余重读，同文件累计达到阈值
判定震荡。顺序分块读大文件（1-50、51-100……零重叠）不会误杀；写类工具改动
文件后重读是合法校验，会先清空该文件的覆盖记录。
"""
from __future__ import annotations

import json
import logging
import os
from enum import Enum
from typing import Dict, List, Tuple

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

# 滑窗重读震荡：同一文件冗余重读（≥50% 行数已读过）累计阈值
REDUNDANT_READ_THRESHOLD = 5
# 冗余判定比例：窗口内已读行数占比 ≥ 该值才算冗余（顺序续读 0% 不命中）
_REDUNDANT_RATIO_NUM = 2  # 即 ≥ 1/2
# 无行号参数的整读：区间上界用大哨兵近似「整个文件」
_FULL_FILE_HI = 10 ** 9


class _ReadCoverage:
    """单文件的已读行区间并集 + 冗余重读计数。

    intervals 维持互不相交且升序的合并区间，record() 先判冗余再并入。
    """

    __slots__ = ("intervals", "redundant_count")

    def __init__(self) -> None:
        self.intervals: List[Tuple[int, int]] = []
        self.redundant_count = 0

    def _covered_lines(self, lo: int, hi: int) -> int:
        return sum(
            min(hi, b) - max(lo, a) + 1
            for a, b in self.intervals
            if max(lo, a) <= min(hi, b)
        )

    def record(self, lo: int, hi: int) -> None:
        total = hi - lo + 1
        if total > 0 and self._covered_lines(lo, hi) * _REDUNDANT_RATIO_NUM >= total:
            self.redundant_count += 1
        # 合并插入（相邻/重叠区间归并），保持有序不相交
        merged: List[Tuple[int, int]] = []
        new_lo, new_hi = lo, hi
        for a, b in self.intervals:
            if b < new_lo - 1 or a > new_hi + 1:
                merged.append((a, b))
            else:
                new_lo, new_hi = min(a, new_lo), max(b, new_hi)
        merged.append((new_lo, new_hi))
        merged.sort()
        self.intervals = merged


class AgentStateTracker:
    def __init__(self, loop_threshold: int = 3) -> None:
        self.strict_threshold = loop_threshold
        self.readonly_threshold = max(loop_threshold, READONLY_LOOP_THRESHOLD)
        self.history_action_hashes: List[str] = []
        self.status: AgentStatus = AgentStatus.IDLE
        # 滑窗重读检测状态：normpath(文件路径) → 已读覆盖
        self.read_coverage: Dict[str, _ReadCoverage] = {}
        # 最近一次死循环判定的人类可读详情（agent_loop 拼中断消息用）
        self.last_loop_detail: str = ""

    def _threshold_for(self, tool_name: str) -> int:
        return (self.readonly_threshold if tool_name in READONLY_TOOLS
                else self.strict_threshold)

    @staticmethod
    def _parse_args(args_str: str) -> Dict[str, object] | None:
        """容错解析工具参数 JSON；非法/非对象返回 None（不参与滑窗检测）。"""
        try:
            parsed = json.loads(args_str) if args_str.strip() else {}
        except (json.JSONDecodeError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def _invalidate_coverage(self, tool_name: str, args_str: str) -> None:
        """非 read_file 的带路径调用（写入/补丁/删除等）改变文件内容：
        之后重读该文件是合法校验，清空其已读覆盖与冗余计数。"""
        if tool_name == "read_file":
            return
        args = self._parse_args(args_str)
        if args is None:
            return
        for key in ("filePath", "path", "file"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                self.read_coverage.pop(os.path.normpath(val.strip()), None)

    def _check_redundant_read(self, tool_name: str, args_str: str) -> bool:
        """滑窗重读震荡：参数每次都变，但读的是同一文件的重叠区域。"""
        if tool_name != "read_file":
            return False
        args = self._parse_args(args_str)
        if args is None:
            return False
        path = args.get("filePath")
        if not isinstance(path, str) or not path.strip():
            return False
        path_key = os.path.normpath(path.strip())

        lo_val, hi_val = args.get("startLine"), args.get("endLine")
        lo = lo_val if isinstance(lo_val, int) and not isinstance(lo_val, bool) and lo_val > 0 else 1
        hi = (hi_val if isinstance(hi_val, int) and not isinstance(hi_val, bool) and hi_val >= lo
              else _FULL_FILE_HI)

        cov = self.read_coverage.setdefault(path_key, _ReadCoverage())
        cov.record(lo, hi)
        if cov.redundant_count >= REDUNDANT_READ_THRESHOLD:
            self.status = AgentStatus.FAILED_LOOP_DETECTED
            self.last_loop_detail = (
                f"你已 {cov.redundant_count} 次重复读取 {path} 的已读区域"
                "（每次行窗口略有不同并不算换策略）"
            )
            logger.warning(
                '[Harness Defense] Redundant re-read loop on "%s" (%d overlapping reads). Interrupting.',
                path_key, cov.redundant_count,
            )
            return True
        return False

    def register_and_check_loop(self, tool_name: str, args_str: str) -> bool:
        # 写类调用先失效对应文件的读取覆盖（内容已变，重读是合法校验）
        self._invalidate_coverage(tool_name, args_str)

        threshold = self._threshold_for(tool_name)
        action_hash = f"{tool_name}:{args_str.strip()}"
        self.history_action_hashes.append(action_hash)

        if len(self.history_action_hashes) >= threshold:
            last = self.history_action_hashes[-threshold:]
            if len(set(last)) == 1:
                self.status = AgentStatus.FAILED_LOOP_DETECTED
                self.last_loop_detail = (
                    f"你已用完全相同参数连续调用 {tool_name} {threshold} 次"
                )
                logger.warning(
                    '[Harness Defense] Infinite loop detected on tool "%s". Interrupting.', tool_name
                )
                return True

        # 精确哈希拦不住的滑窗重读震荡（参数每次都不同）
        return self._check_redundant_read(tool_name, args_str)