# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""文件系统工具（对应课程第6课（代码理解） FileSystemPlugin 增强版）。

增强：带行号范围读取、list_dir、gitignore 感知文件树（pathspec）。
所有操作限定在 workspace 根目录内（防目录穿越）。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Optional

import pathspec

from ..core.types import ToolDefinition

TRUNCATE_LINES = 500


class FileSystemTools:
    def __init__(self, workspace: str) -> None:
        self.workspace = os.path.abspath(workspace)
        os.makedirs(self.workspace, exist_ok=True)

    # ------------------------------------------------------------ 安全路径

    def resolve(self, rel_path: str, access: str = "") -> str:
        """解析路径；项目外路径必须由安全中间件注入本次授权。"""
        raw = os.path.expanduser(rel_path or ".")
        target = os.path.abspath(raw if os.path.isabs(raw) else os.path.join(self.workspace, raw))
        if not (target == self.workspace or target.startswith(self.workspace + os.sep)):
            if access not in {"read", "write"}:
                raise PermissionError(f"[Security Violation]: 项目外路径未获授权: {rel_path}")
        return target

    # ------------------------------------------------------------ gitignore

    def _load_gitignore(self) -> pathspec.PathSpec:
        patterns: List[str] = [
            ".git", "node_modules", "dist", "build", "coverage", ".venv", "venv",
            "__pycache__", "*.pyc", ".DS_Store", ".lite-work", "web/node_modules",
        ]
        gitignore_path = os.path.join(self.workspace, ".gitignore")
        if os.path.exists(gitignore_path):
            try:
                with open(gitignore_path, "r", encoding="utf-8") as f:
                    patterns.extend(l for l in f.read().splitlines() if l.strip() and not l.startswith("#"))
            except OSError:
                pass
        return pathspec.PathSpec.from_lines("gitwildmatch", patterns)

    # ------------------------------------------------------------ 工具定义

    def get_tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="read_file",
                description="读取指定文件内容，支持带行号与行范围（避免读取超大文件时爆上下文）",
                parameters={
                    "type": "object",
                    "properties": {
                        "filePath": {"type": "string", "description": "相对 workspace 的文件路径"},
                        "startLine": {"type": "number", "description": "起始行号（可选，从 1 开始）"},
                        "endLine": {"type": "number", "description": "结束行号（可选）"},
                        "withLineNumbers": {"type": "boolean", "description": "是否带行号输出，默认 true"},
                    },
                    "required": ["filePath"],
                },
            ),
            ToolDefinition(
                name="write_file",
                description="写入文件内容（自动创建父目录，覆盖写）",
                parameters={
                    "type": "object",
                    "properties": {
                        "filePath": {"type": "string", "description": "相对 workspace 的文件路径"},
                        "content": {"type": "string", "description": "要写入的文本内容"},
                    },
                    "required": ["filePath", "content"],
                },
            ),
            ToolDefinition(
                name="delete_file",
                description="删除工作区内的文件（不可逆，需用户审批确认）",
                parameters={
                    "type": "object",
                    "properties": {
                        "filePath": {"type": "string", "description": "相对 workspace 的文件路径"},
                    },
                    "required": ["filePath"],
                },
            ),
            ToolDefinition(
                name="list_dir",
                description="列出指定目录下的条目（一层，不递归）",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "相对 workspace 的目录，默认 '.'"},
                    },
                },
            ),
            ToolDefinition(
                name="file_tree",
                description="生成项目目录树（自动过滤 .gitignore 忽略文件与大目录）",
                parameters={
                    "type": "object",
                    "properties": {
                        "maxDepth": {"type": "number", "description": "遍历深度，默认 3"},
                    },
                },
            ),
        ]

    # ------------------------------------------------------------ 执行

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        access = args.get("_approved_external_access", "")
        if name == "read_file":
            result = self._read_file(args, access)
        elif name == "write_file":
            result = self._write_file(args, access)
        elif name == "delete_file":
            result = self._delete_file(args, access)
        elif name == "list_dir":
            result = self._list_dir(args, access)
        elif name == "file_tree":
            result = await asyncio.to_thread(self._file_tree, args)
        else:
            raise ValueError(f"Unknown FileSystem Tool: {name}")
        return result

    def _read_file(self, args: Dict[str, Any], access: str = "") -> str:
        rel_path = args.get("filePath", "")
        target = self.resolve(rel_path, access)
        if not os.path.exists(target):
            return f"[Error]: 文件不存在: {rel_path}"
        if os.path.isdir(target):
            return f"[Error]: 这是目录不是文件: {rel_path}"

        with open(target, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().split("\n")

        start = max(1, int(args.get("startLine") or 1))
        end = min(len(lines), int(args.get("endLine") or len(lines)))
        if start > end:
            return f"[Error]: 行范围无效 startLine={start} > endLine={end}"

        slice_lines = lines[start - 1 : end]
        truncated = end - start + 1 > TRUNCATE_LINES
        if truncated:
            slice_lines = slice_lines[:TRUNCATE_LINES]

        with_numbers = args.get("withLineNumbers", True)
        if with_numbers:
            body = "\n".join(f"{start + i} | {l}" for i, l in enumerate(slice_lines))
        else:
            body = "\n".join(slice_lines)

        note = f"\n... [输出截断，仅显示前 {TRUNCATE_LINES} 行]" if truncated else ""
        return f"File: {rel_path} (行 {start}-{min(end, start + TRUNCATE_LINES - 1)} / 共 {len(lines)} 行){note}\n{body}"

    def _delete_file(self, args: Dict[str, Any], access: str = "") -> str:
        rel_path = args.get("filePath", "")
        target = self.resolve(rel_path, access)
        if not os.path.exists(target):
            return f"[Error]: 文件不存在: {rel_path}"
        if os.path.isdir(target):
            return f"[Error]: 这是目录不是文件（不支持删除目录）: {rel_path}"
        os.remove(target)
        return f"[Deleted]: 已删除文件 {rel_path}"

    def _write_file(self, args: Dict[str, Any], access: str = "") -> str:
        rel_path = args.get("filePath", "")
        content = args.get("content", "")
        raw = os.path.expanduser(rel_path)
        candidate = os.path.abspath(raw if os.path.isabs(raw) else os.path.join(self.workspace, raw))
        inside = candidate == self.workspace or candidate.startswith(self.workspace + os.sep)
        if not inside and access != "write":
            raise PermissionError(f"[Security Violation]: 项目外写入未获授权: {rel_path}")
        target = self.resolve(rel_path, access)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
        size = os.path.getsize(target)
        return f"[Success]: 已写入 {rel_path} ({size} bytes)"

    def _list_dir(self, args: Dict[str, Any], access: str = "") -> str:
        rel_path = args.get("path") or "."
        target = self.resolve(rel_path, access)
        if not os.path.isdir(target):
            return f"[Error]: 目录不存在: {rel_path}"

        entries = sorted(os.listdir(target))
        lines = [f"{e}/" if os.path.isdir(os.path.join(target, e)) else e for e in entries]
        return f"目录 {rel_path} ({len(entries)} 项):\n" + "\n".join(lines)

    def _file_tree(self, args: Dict[str, Any]) -> str:
        max_depth = max(1, int(args.get("maxDepth") or 3))
        spec = self._load_gitignore()
        lines: List[str] = []

        def walk(current: str, depth: int, prefix: str) -> None:
            if depth > max_depth:
                return
            try:
                entries = sorted(os.listdir(current), key=lambda e: (not os.path.isdir(os.path.join(current, e)), e.lower()))
            except OSError:
                return

            filtered = []
            for name in entries:
                if name.startswith("."):
                    continue
                rel = os.path.relpath(os.path.join(current, name), self.workspace)
                if spec.match_file(rel):
                    continue
                filtered.append(name)

            for index, name in enumerate(filtered):
                is_last = index == len(filtered) - 1
                connector = "└── " if is_last else "├── "
                full = os.path.join(current, name)
                lines.append(f"{prefix}{connector}{name}{'/' if os.path.isdir(full) else ''}")
                if os.path.isdir(full):
                    walk(full, depth + 1, prefix + ("    " if is_last else "│   "))

        lines.append(os.path.basename(self.workspace) + "/")
        walk(self.workspace, 1, "")
        return "\n".join(lines) if len(lines) > 1 else f"目录为空或全部被过滤: {self.workspace}"
