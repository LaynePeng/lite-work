# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""JSONL 追加/读取/轮转的公共实现（R1 重构，消除三处逐字级重复）。

此前 core/metrics.py、core/trajectory.py、core/observation_pack.py 各自实现了
几乎相同的「追加写 JSONL + 错误静默」模式，metrics 与 trajectory 还各自复制了
完全相同的文件轮转逻辑（超限保留最近一半）。本模块收敛为一个实现。

设计原则：
- **一切 OSError 静默**——指标/轨迹/观察账本都是旁路数据，绝不因它们影响任务；
- **轮转在追加前自动执行**——调用方无需显式调 rotate；
- **读取跳过坏行**——文件损坏（磁盘满/断电）不应让整个读取失败。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

PathLike = Union[str, Path]


def append_jsonl(path: PathLike, event: Dict[str, Any],
                 *, max_bytes: Optional[int] = None) -> None:
    """追加一行 JSON；自动建父目录；超 max_bytes 先轮转；一切 OSError 静默。"""
    p = Path(path)
    try:
        if max_bytes is not None:
            rotate_if_needed(p, max_bytes)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_jsonl(path: PathLike, *, offset: int = 0, limit: Optional[int] = None,
               tail: Optional[int] = None) -> List[Dict[str, Any]]:
    """读 JSONL 行 → 容错解析 → 跳过坏行/非 dict；缺失/损坏返回 []。

    - offset + limit：分页模式（trajectory 用）
    - tail：只取最后 N 条（metrics 用）
    两者互斥，同时传时 tail 优先。
    """
    p = Path(path)
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    if tail is not None and tail > 0:
        lines = lines[-tail:]
        offset = 0
        limit = None

    out: List[Dict[str, Any]] = []
    for line in lines[offset:offset + limit if limit else None]:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if isinstance(data, dict):
                out.append(data)
        except json.JSONDecodeError:
            continue
    return out


def rotate_if_needed(path: PathLike, max_bytes: int) -> None:
    """文件超过 max_bytes 时保留最近一半行；OSError 静默。"""
    p = Path(path)
    try:
        if not p.exists() or p.stat().st_size <= max_bytes:
            return
        lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
        keep = lines[len(lines) // 2:]
        p.write_text("".join(keep), encoding="utf-8")
    except OSError:
        pass
