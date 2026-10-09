# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""Agent 轨迹。

解决什么：现有事件流是瞬态的（SSE 发完即弃），没有"Agent 为什么这样行动"的
持久化轨迹；评测/复盘/审计都缺数据基础。

设计（两层，刻意从简）：
1. **落盘**：`$CONFIG_DIR/trajectories/<session_id>/<task_id>.jsonl`，一行一个
   JSON 对象（header / step / model_call / tool_call / skill_exec / outcome）；
2. **分析**：级联漏斗第一步——确定性规则全量筛（重试风暴 / 成本异常 /
   上下文膨胀），灰区交给人工或 LLM。

隐私与体积：
- 复用 `gate.py` 的 `redact_evidence`（凭证脱敏 + 截断）；
- 保留策略：单会话上限 TRAJECTORY_MAX_FILE_BYTES，超限保留最近一半；
- 默认**关闭**（`trajectory_enabled: false`），设置页手动开启——轨迹是
  低频需求的持久化数据，不应默认产生存储压力。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

#: 单会话轨迹文件上限（约 5MB，超出后保留最近一半）
TRAJECTORY_MAX_FILE_BYTES = 5 * 1024 * 1024

#: 轨迹事件类型
EVENT_TYPES = frozenset({
    "header", "step", "model_call", "tool_call", "skill_exec",
    "state_change", "outcome",
})


class TrajectoryWriter:
    """轨迹写入器（一个 session 一个实例；trajectory_enabled=false 时零开销）。"""

    def __init__(self, config_dir: str, session_id: str, task_id: str,
                 workspace: str = "", agent_id: str = "") -> None:
        self.enabled = True
        self.session_id = session_id
        self.task_id = task_id
        self._path = Path(config_dir) / "trajectories" / session_id / f"{task_id}.jsonl"
        self._step_count = 0
        self._header_written = False
        self._header = {
            "type": "header",
            "session_id": session_id,
            "task_id": task_id,
            "workspace": workspace,
            "agent_id": agent_id,
            "started_at": time.time(),
            "trace_id": f"{session_id[:8]}-{task_id[:8]}",
        }

    def write_header(self) -> None:
        if not self._header_written:
            self._append(self._header)
            self._header_written = True

    def step(self, turn: int, **fields: Any) -> None:
        self._append({"type": "step", "step_id": self._next_step(), "turn": turn,
                      "ts": time.time(), **fields})

    def model_call(self, turn: int, model: str = "", tokens: Optional[Dict] = None,
                   latency_ms: Optional[float] = None) -> None:
        self._append({
            "type": "model_call", "step_id": self._next_step(), "turn": turn,
            "ts": time.time(), "model": model,
            "tokens": tokens or {}, "latency_ms": latency_ms,
        })

    def tool_call(self, turn: int, tool: str, outcome: str = "ok",
                  duration_ms: Optional[float] = None, args_hint: str = "") -> None:
        from .gate import redact_evidence
        self._append({
            "type": "tool_call", "step_id": self._next_step(), "turn": turn,
            "ts": time.time(), "tool": tool, "outcome": outcome,
            "duration_ms": duration_ms,
            "args_hint": redact_evidence(args_hint),
        })

    def skill_exec(self, turn: int, skill: str, source: str = "auto") -> None:
        self._append({
            "type": "skill_exec", "step_id": self._next_step(), "turn": turn,
            "ts": time.time(), "skill": skill, "source": source,
        })

    def outcome(self, verdict: str, gate: Optional[Dict] = None,
                cost_estimate: float = 0.0, turns: int = 0) -> None:
        self._append({
            "type": "outcome", "step_id": self._next_step(),
            "ts": time.time(), "verdict": verdict,
            "gate": gate or {}, "cost_estimate": cost_estimate, "turns": turns,
        })

    def _next_step(self) -> int:
        self._step_count += 1
        return self._step_count

    def _append(self, event: Dict[str, Any]) -> None:
        if not self.enabled:
            return
        # R1 重构：公共 JSONL 追加（含自动轮转），见 core/jsonl_io.py
        from .jsonl_io import append_jsonl
        append_jsonl(self._path, event, max_bytes=TRAJECTORY_MAX_FILE_BYTES)


# ---------------------------------------------------------------- 读取与分析

def read_trajectory(config_dir: str, session_id: str, task_id: str,
                    offset: int = 0, limit: int = 500) -> List[Dict[str, Any]]:
    """读一个任务的轨迹（分页）。文件缺失返回空列表。"""
    from .jsonl_io import read_jsonl
    path = Path(config_dir) / "trajectories" / session_id / f"{task_id}.jsonl"
    return read_jsonl(path, offset=offset, limit=limit)


def list_session_trajectories(config_dir: str, session_id: str) -> List[Dict[str, Any]]:
    """列出某会话的所有轨迹任务（文件名 = task_id）。"""
    base = Path(config_dir) / "trajectories" / session_id
    if not base.is_dir():
        return []
    out = []
    for f in sorted(base.glob("*.jsonl")):
        try:
            stat = f.stat()
            out.append({
                "task_id": f.stem,
                "file": str(f),
                "size_bytes": stat.st_size,
                "modified_at": stat.st_mtime,
            })
        except OSError:
            continue
    return out


# ---------------------------------------------------------------- 级联漏斗（第一步：确定性规则）

#: 重试风暴：同一工具连续失败次数阈值
RETRY_STORM_THRESHOLD = 3

#: 成本异常：单任务成本超过此值（美元）
COST_OUTLIER_THRESHOLD = 5.0

#: 上下文膨胀：单任务压缩次数阈值
CONTEXT_BLOAT_THRESHOLD = 5


def analyze_trajectory(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """确定性规则筛异常（级联漏斗第一步；灰区留给人工/LLM）。

    规则：
    1. 重试风暴：同一工具连续失败 ≥ RETRY_STORM_THRESHOLD 次
    2. 成本异常：outcome 的 cost_estimate > COST_OUTLIER_THRESHOLD
    3. 上下文膨胀：压缩/compaction 次数 > CONTEXT_BLOAT_THRESHOLD（从
       state_change 事件统计）
    """
    findings: List[Dict[str, Any]] = []

    # 1. 重试风暴
    tool_failures: Dict[str, int] = {}
    consecutive: Dict[str, int] = {}
    # 每工具只记一条（连续失败结束时取峰值），避免同一场风暴产出 N 条 findings
    storm_peak: Dict[str, int] = {}
    for e in events:
        if e.get("type") != "tool_call":
            continue
        tool = str(e.get("tool") or "?")
        if e.get("outcome") == "error":
            consecutive[tool] = consecutive.get(tool, 0) + 1
            tool_failures[tool] = tool_failures.get(tool, 0) + 1
            storm_peak[tool] = max(storm_peak.get(tool, 0), consecutive[tool])
        else:
            consecutive[tool] = 0
    # 风暴结束后（或到末尾）统一记录
    for tool, peak in storm_peak.items():
        if peak >= RETRY_STORM_THRESHOLD:
            findings.append({
                "rule": "retry_storm",
                "tool": tool,
                "consecutive_failures": peak,
                "total_failures": tool_failures[tool],
                "severity": "high" if peak >= 5 else "medium",
            })

    # 2. 成本异常
    # 取最后一条 outcome（events 按时间序，最后一条才是最终结果）
    outcome_events = [e for e in events if e.get("type") == "outcome"]
    if outcome_events:
        final = outcome_events[-1]
        cost = float(final.get("cost_estimate") or 0)
        if cost > COST_OUTLIER_THRESHOLD:
            findings.append({
                "rule": "cost_outlier",
                "cost_estimate": cost,
                "threshold": COST_OUTLIER_THRESHOLD,
                "severity": "medium",
            })

    # 3. 上下文膨胀
    compactions = sum(1 for e in events
                      if e.get("type") == "state_change"
                      and "compact" in str(e.get("change") or ""))
    if compactions > CONTEXT_BLOAT_THRESHOLD:
        findings.append({
            "rule": "context_bloat",
            "compactions": compactions,
            "threshold": CONTEXT_BLOAT_THRESHOLD,
            "severity": "low",
        })

    return findings
