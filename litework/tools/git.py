"""Git 自动化工具（对应课程第14课（实战 Core）总结提出的增强插件：Git 自动化）。

只读操作直通；写操作（commit）需要显式 message；破坏性操作
（push --force / reset --hard / branch -D）由 SecurityGuard 判定为中危 → 人工确认。
"""
from __future__ import annotations

import asyncio
import os
import shutil
from typing import Any, Dict, List, Tuple

from ..core.types import ToolDefinition


class GitTools:
    def __init__(self, workspace: str) -> None:
        self.workspace = os.path.abspath(workspace)

    def get_tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="git_status",
                description="查看仓库当前状态（分支、暂存区、未提交改动摘要）",
                parameters={"type": "object", "properties": {}},
            ),
            ToolDefinition(
                name="git_diff",
                description="查看工作区未提交的代码改动 diff（可选指定文件）",
                parameters={
                    "type": "object",
                    "properties": {
                        "filePath": {"type": "string", "description": "仅查看指定文件的 diff（可选）"},
                        "staged": {"type": "boolean", "description": "查看已暂存改动，默认 false"},
                    },
                },
            ),
            ToolDefinition(
                name="git_log",
                description="查看最近的提交历史",
                parameters={
                    "type": "object",
                    "properties": {
                        "count": {"type": "number", "description": "显示条数，默认 10"},
                    },
                },
            ),
            ToolDefinition(
                name="git_commit",
                description="暂存全部改动并创建一次提交（message 必填）",
                parameters={
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "提交信息"},
                        "files": {"type": "array", "items": {"type": "string"}, "description": "只提交指定文件（可选，默认全部）"},
                    },
                    "required": ["message"],
                },
            ),
            ToolDefinition(
                name="git_branch",
                description="查看分支列表与当前分支",
                parameters={"type": "object", "properties": {}},
            ),
        ]

    async def _run(self, args: List[str], timeout: float = 30.0) -> Tuple[int, str, str]:
        git = shutil.which("git")
        if not git:
            return (1, "", "git 不可用。")
        proc = await asyncio.create_subprocess_exec(
            git, *args,
            cwd=self.workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return (124, "", f"git 命令超时（{timeout}s）。")
        return (
            proc.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        if name == "git_status":
            code, out, err = await self._run(["status", "--short", "--branch"])
            return out.strip() or err.strip() or "[无输出]"
        if name == "git_diff":
            cmd = ["diff", "--color=never"]
            if args.get("staged"):
                cmd.append("--cached")
            if args.get("filePath"):
                cmd.append(args["filePath"])
            code, out, err = await self._run(cmd)
            return out.strip() or err.strip() or "[无改动]"
        if name == "git_log":
            count = max(1, min(50, int(args.get("count") or 10)))
            code, out, err = await self._run(["log", f"--max-count={count}", "--oneline", "--decorate"])
            return out.strip() or err.strip() or "[无提交历史]"
        if name == "git_commit":
            message = args.get("message", "").strip()
            if not message:
                return "[Error]: 提交信息 message 不能为空。"
            files = args.get("files")
            if files:
                code, out, err = await self._run(["add", "--"] + [str(f) for f in files])
            else:
                code, out, err = await self._run(["add", "-A"])
            if code != 0:
                return f"[git add 失败]: {err.strip()}"
            code, out, err = await self._run(["commit", "-m", message])
            if code != 0:
                return f"[git commit 失败]: {err.strip()}"
            return f"[Success]: 已提交 -> {out.strip()}"
        if name == "git_branch":
            code, out, err = await self._run(["branch", "-a"])
            return out.strip() or err.strip() or "[无分支]"
        raise ValueError(f"Unknown Git Tool: {name}")