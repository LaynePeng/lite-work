# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""代码库感知工具（对应课程第6课（代码理解））：Ripgrep 高速搜索 + gitignore 过滤文件树。"""
from __future__ import annotations

import asyncio
import os
import shutil
from typing import Any, Dict, List

from ..core.types import ToolDefinition


class CodebaseTools:
    def __init__(self, workspace: str) -> None:
        self.workspace = os.path.abspath(workspace)

    def get_tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="search_code",
                description="使用 Ripgrep 在整个代码库中高速搜索（正则/关键词），返回 文件:行号:列号 与匹配行",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "正则表达式或搜索关键字"},
                        "includePattern": {"type": "string", "description": "文件过滤，如 '*.ts' '*.py'"},
                        "maxResults": {"type": "number", "description": "最大匹配数，默认 50"},
                    },
                    "required": ["query"],
                },
            ),
        ]

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        if name == "search_code":
            return await self._search(args)
        raise ValueError(f"Unknown Codebase Tool: {name}")

    async def _search(self, args: Dict[str, Any]) -> str:
        query = args.get("query", "")
        include = args.get("includePattern")
        max_results = max(1, int(args.get("maxResults") or 50))

        rg = self._resolve_rg()
        if not rg:
            return (
                "[Error]: 未检测到 ripgrep (rg)。已检查 PATH 及常见安装目录；"
                "请安装 ripgrep，或通过 LITEWORK_RG_PATH 指定可执行文件。"
            )

        cmd = [
            rg, "--line-number", "--column", "--color=never", "--smart-case",
            "--max-count", str(max_results), "--no-messages", "--hidden", "--glob", "!.git",
            "--glob", "!node_modules", "--glob", "!.venv", "--glob", "!dist", "--glob", "!build",
        ]
        if include:
            cmd += ["--glob", include]
        cmd.append(query)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            text = stdout.decode("utf-8", errors="replace").strip()
            if not text:
                return f'未找到匹配: "{query}"'
            lines = text.split("\n")
            note = f"\n[... 仅显示前 {max_results} 条匹配]" if len(lines) >= max_results else ""
            return f'搜索 "{query}" 找到 {len(lines)} 处匹配:{note}\n{text}'
        except asyncio.TimeoutError:
            return "[Error]: 搜索超时（30s）。"
        except Exception as exc:
            return f"[Error]: 搜索执行失败: {exc}"

    @staticmethod
    def _resolve_rg() -> str | None:
        """兼容从 Finder/Electron 启动时缺少 shell PATH 的环境。"""
        configured = os.environ.get("LITEWORK_RG_PATH", "").strip()
        candidates = [
            configured,
            shutil.which("rg") or "",
            "/opt/homebrew/bin/rg",
            "/usr/local/bin/rg",
            "/opt/local/bin/rg",
            os.path.expanduser("~/.cargo/bin/rg"),
            os.path.expanduser("~/.local/bin/rg"),
        ]
        for candidate in candidates:
            if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None
