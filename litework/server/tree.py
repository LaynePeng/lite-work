# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""工作区目录树与 Git 状态服务（侧边栏「文件」页签）。

- 结构化返回目录/文件条目，前端按需懒加载子目录
- gitignore 感知（复用 FileSystemTools 的过滤逻辑）
- `git status --porcelain -z` 解析 → A/M/D/U/R/C 状态字母（对齐 OpenCode 侧边栏）
- 已删除但仍在索引中的文件（D）补回目录列表，与 OpenCode 一致
- git 状态短 TTL 缓存，动态刷新时避免频繁拉起 git 进程
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("litework.server.tree")

GIT_CACHE_TTL = 3.0
_git_cache: Dict[str, Tuple[float, Optional[str], Dict[str, str]]] = {}


def _run_git(workspace: str, args: List[str]) -> Tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", workspace, *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10,
        )
        return proc.returncode, proc.stdout
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""


def _status_char(xy: str) -> str:
    """XY 双字符 → 单字母（OpenCode 风格）：优先暂存区，其次工作区。"""
    x, y = xy[0], xy[1] if len(xy) > 1 else " "
    c = x if x != " " else y
    if c == "?":  # untracked
        return "U"
    if c == "T":  # 类型变更（symlink/mode）≈ 修改
        return "M"
    return c


def git_snapshot(workspace: str) -> Tuple[Optional[str], Dict[str, str]]:
    """返回 (分支名, {相对路径: 状态字母})；非 git 仓库返回 (None, {})。带 TTL 缓存。"""
    now = time.time()
    cached = _git_cache.get(workspace)
    if cached and now - cached[0] <= GIT_CACHE_TTL:
        return cached[1], cached[2]

    code, out = _run_git(workspace, ["status", "--porcelain", "-z"])
    if code != 0:
        _git_cache[workspace] = (now, None, {})
        return None, {}

    branch: Optional[str] = None
    code2, out2 = _run_git(workspace, ["rev-parse", "--abbrev-ref", "HEAD"])
    if code2 == 0 and out2.strip():
        branch = out2.strip() or None

    status_map: Dict[str, str] = {}
    if out:
        fields = out.split("\0")
        i = 0
        while i < len(fields):
            entry = fields[i]
            i += 1
            if not entry:
                continue
            xy = entry[:2]
            path = entry[3:]
            # 重命名/复制：-z 模式下第一个字段即新路径，紧随的字段是旧路径，跳过
            if xy[0] in ("R", "C") and i < len(fields) and fields[i]:
                i += 1
            status_map[path] = _status_char(xy)

    _git_cache[workspace] = (now, branch, status_map)
    return branch, status_map


def _dir_has_changes(status_map: Dict[str, str], rel: str) -> bool:
    prefix = rel + "/"
    return any(k == rel or k.startswith(prefix) for k in status_map)


def list_tree(workspace: str, rel_path: str) -> Dict[str, Any]:
    """列出目录条目：目录在前、文件在后，各自按名排序，附 git 状态。

    返回 {"branch", "has_repo", "entries"}，entry 形如：
    {"name", "path", "type": "dir"|"file", "status"?|"has_changes"?}
    """
    from ..tools.filesystem import FileSystemTools

    fs = FileSystemTools(workspace)
    spec = fs._load_gitignore()

    base = os.path.abspath(os.path.join(workspace, rel_path))
    if not (base == workspace or base.startswith(workspace + os.sep)):
        raise ValueError("路径越界：只能浏览工作区内的目录")
    if not os.path.isdir(base):
        raise ValueError(f"目录不存在: {rel_path or '.'}")

    branch, status_map = git_snapshot(workspace)

    try:
        names = os.listdir(base)
    except OSError as exc:
        raise ValueError(f"无法读取: {exc}") from exc

    rel_prefix = (rel_path.rstrip("/\\") + "/") if rel_path else ""

    entries: List[Dict[str, Any]] = []
    for name in names:
        if name.startswith("."):
            continue
        rel = rel_prefix + name
        if spec.match_file(rel):
            continue
        full = os.path.join(base, name)
        try:
            is_dir = os.path.isdir(full)
        except OSError:
            continue
        if is_dir:
            entries.append({
                "name": name, "path": rel, "type": "dir",
                "has_changes": _dir_has_changes(status_map, rel),
            })
        else:
            entries.append({
                "name": name, "path": rel, "type": "file",
                "status": status_map.get(rel),
            })

    # 已删除文件（D）仍展示在目录树中（OpenCode 风格：磁盘不存在但索引还在）
    if status_map:
        parent = rel_prefix.rstrip("/") if rel_prefix else ""
        seen = {e["name"] for e in entries}
        for rel, st in status_map.items():
            if st != "D":
                continue
            dirname, _, fname = rel.rpartition("/")
            if dirname != parent or not fname or fname in seen:
                continue
            if spec.match_file(rel):
                continue
            entries.append({"name": fname, "path": rel, "type": "file", "status": "D"})
            seen.add(fname)

    entries.sort(key=lambda e: (0 if e["type"] == "dir" else 1, e["name"].lower()))
    return {"branch": branch, "has_repo": branch is not None, "entries": entries}