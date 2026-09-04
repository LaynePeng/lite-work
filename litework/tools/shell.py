"""受限本地 Shell 沙箱（对应课程第7课（安全代码操作） LocalProcessSandbox 增强版）。

- asyncio subprocess + 硬超时（超时 SIGKILL）
- 敏感环境变量擦除（防 API Key 泄漏给子进程）
- 输出缓冲限制
- 高危命令的最终防线（双保险：SecurityGuard 之外再兜底一层）
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List

from ..core.types import ToolDefinition
from ..security.guard import SENSITIVE_ENV_VARS

_DANGEROUS_PATTERNS = [
    r"rm\s+-rf\s+[/\~]",
    r"mkfs",
    r"dd\s+if=",
    r">\s*/dev/sd",
    r":\(\)\{\s*:\|\:&\s*\};:",
]


class ShellTools:
    def __init__(self, workspace: str, timeout_seconds: float = 60.0, max_output: int = 200_000) -> None:
        self.workspace = os.path.abspath(workspace)
        self.timeout_seconds = timeout_seconds
        self.max_output = max_output

    def get_tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="execute_command",
                description="在受限 Shell 中执行命令行指令（超时自动终止、敏感环境变量已擦除、高危命令被拦截）",
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "要执行的 Shell 指令"},
                    },
                    "required": ["command"],
                },
            ),
        ]

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        if name != "execute_command":
            raise ValueError(f"Unknown Shell Tool: {name}")
        command = args.get("command", "")

        # 最终防线：高危命令粗暴过滤
        import re

        for pattern in _DANGEROUS_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return f"[Security Blocked]: 命令命中高危模式 /{pattern}/ 被 Harness 拒绝。"

        # 剥离敏感环境变量，防止 API Key 泄漏给子进程
        clean_env = {k: v for k, v in os.environ.items() if k not in SENSITIVE_ENV_VARS}

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=self.workspace,
                env=clean_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout_seconds
            )
            out = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace")
            timed_out = False
            exit_code = proc.returncode
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            out, err = "", ""
            timed_out = True
            exit_code = 124

        def clamp(text: str) -> str:
            if len(text) > self.max_output:
                return text[: self.max_output] + f"\n... [输出截断: {len(text) - self.max_output} 字符省略]"
            return text

        parts = [f"[Exit Code]: {exit_code}"]
        if timed_out:
            parts.append(f"[Timed Out]: 命令超过 {self.timeout_seconds}s 被强制终止。")
        if out.strip():
            parts.append(f"[STDOUT]:\n{clamp(out.rstrip())}")
        if err.strip():
            parts.append(f"[STDERR]:\n{clamp(err.rstrip())}")
        if not out.strip() and not err.strip():
            parts.append("[No output]")
        return "\n".join(parts)