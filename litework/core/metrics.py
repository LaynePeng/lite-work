# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""工具与技能使用指标（W3）。

对应《AI Native 研发范式实践手册》p.47 的「工具评测」：工具与技能变多之后，
Agent 可能**选错**能力（命中率问题），也可能选对了却**用不成**（成功率问题）。
手册的结论是要把这两件事持续量化，再据此改描述、拆能力、删长期低效项。

本模块只做**确定的计量**（不做语义判断，语义交给可选的判定层）：
- 记录：每个工具调用的结果（ok/error/cancelled）与耗时、每次技能加载；
- 归属：记录"本次任务加载过哪些技能"，工具调用带上该列表（任务级归属，明确不精确）；
- 聚合：按工具算 calls / errors / success_rate，按技能算 uses，并列出成功率最低的工具。

隐私与体积：
- **只记录结构化计数**，不落盘工具参数与输出正文（凭证/业务数据不进指标）；
- JSONL 追加写，单文件超限时自动截断保留最近部分（见 `_rotate_if_needed`）。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

#: 结果三态（与门禁的语义无关，这里只描述调用成败）
OUTCOMES = ("ok", "error", "cancelled")

#: 单个指标文件上限（约 2MB，超出后保留最近一半）——指标是"可再生的旁路数据"，
#: 不允许无限增长影响磁盘/加载
_MAX_FILE_BYTES = 2 * 1024 * 1024


def classify_outcome(result: str) -> str:
    """从工具结果文本判定调用成败。

    只认工具层的显式失败前缀（各工具约定），不做语义猜测：
    - `[Error]` / `[Denied` / `[Security Blocked]` / `[Failed]` / `[Timed Out]`
      → error
    - `[Stopped]` / `[Cancelled]` → cancelled
    - 其余 → ok
    """
    text = (result or "").lstrip()
    if text.startswith(("[Error]", "[Failed]", "[Security Blocked]", "[Denied")):
        return "error"
    if "[Timed Out]" in text[:200] or text.startswith(("[Stopped]", "[Cancelled]")):
        return "cancelled"
    return "ok"


class ToolMetrics:
    """工具/技能指标记录器（app 级共享；path=None 时整体停用）。"""

    def __init__(self, path: Optional[str] = None, enabled: bool = True) -> None:
        self.path = path
        self.enabled = bool(enabled and path)

    # ------------------------------------------------------------ 写入

    def record_tool(self, tool: str, outcome: str, *, duration_ms: Optional[float] = None,
                    skills: Optional[List[str]] = None, agent_id: Optional[str] = None,
                    session_id: Optional[str] = None) -> None:
        self._append({
            "type": "tool",
            "tool": str(tool),
            "outcome": outcome if outcome in OUTCOMES else "ok",
            "duration_ms": round(float(duration_ms), 1) if duration_ms else None,
            # 任务级归属：本次任务加载过的技能（不精确，但足以看出"某技能带来哪些调用"）
            "skills": [str(s) for s in (skills or [])],
            "agent_id": agent_id or "",
            "session_id": session_id or "",
            "at": time.time(),
        })

    def record_skill(self, skill: str, *, source: str = "auto",
                     session_id: Optional[str] = None) -> None:
        self._append({
            "type": "skill",
            "skill": str(skill),
            "source": source if source in ("auto", "explicit") else "auto",
            "session_id": session_id or "",
            "at": time.time(),
        })

    def _append(self, event: Dict[str, Any]) -> None:
        if not self.enabled or not self.path:
            return
        # R1 重构：公共 JSONL 追加（含自动轮转），见 core/jsonl_io.py
        from .jsonl_io import append_jsonl
        append_jsonl(self.path, event, max_bytes=_MAX_FILE_BYTES)


def read_events(path: Optional[str], limit: int = 20000) -> List[Dict[str, Any]]:
    """读指标事件（只取最近 limit 行；文件缺失/损坏返回空列表）。"""
    if not path:
        return []
    from .jsonl_io import read_jsonl
    return read_jsonl(path, tail=limit)


def summarize(events: List[Dict[str, Any]], worst_n: int = 3) -> Dict[str, Any]:
    """聚合：命中/成功率视角（纯计数，机械可核对）。"""
    tools: Dict[str, Dict[str, Any]] = {}
    skills: Dict[str, Dict[str, Any]] = {}
    for e in events:
        if e.get("type") == "tool":
            name = str(e.get("tool") or "?")
            slot = tools.setdefault(name, {"calls": 0, "errors": 0, "cancelled": 0,
                                           "total_ms": 0.0, "timed_calls": 0})
            slot["calls"] += 1
            outcome = e.get("outcome")
            if outcome == "error":
                slot["errors"] += 1
            elif outcome == "cancelled":
                slot["cancelled"] += 1
            ms = e.get("duration_ms")
            if isinstance(ms, (int, float)):
                slot["total_ms"] += float(ms)
                slot["timed_calls"] += 1
        elif e.get("type") == "skill":
            name = str(e.get("skill") or "?")
            slot = skills.setdefault(name, {"uses": 0, "auto": 0, "explicit": 0, "tools": {}})
            slot["uses"] += 1
            slot["auto" if e.get("source") != "explicit" else "explicit"] += 1
    # 技能 → 关联工具（任务级归属）
    for e in events:
        if e.get("type") != "tool":
            continue
        for s in e.get("skills") or []:
            slot = skills.setdefault(str(s), {"uses": 0, "auto": 0, "explicit": 0, "tools": {}})
            slot["tools"][str(e.get("tool"))] = slot["tools"].get(str(e.get("tool")), 0) + 1

    for name, slot in tools.items():
        calls = slot["calls"] or 1
        slot["success_rate"] = round((calls - slot["errors"] - slot["cancelled"]) / calls, 4)
        slot["avg_ms"] = round(slot["total_ms"] / slot["timed_calls"], 1) if slot["timed_calls"] else None
        slot.pop("total_ms", None)
        slot.pop("timed_calls", None)

    failing = sorted(
        ({"tool": n, **v} for n, v in tools.items() if v["errors"] or v["cancelled"]),
        key=lambda x: (x["success_rate"], -x["calls"]),
    )
    return {
        "tools": tools,
        "skills": skills,
        "tool_count": len(tools),
        "skill_count": len(skills),
        "worst_tools": failing[:worst_n],
        "events": len(events),
    }
