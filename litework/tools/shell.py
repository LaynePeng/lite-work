"""受限本地 Shell 沙箱（对应课程第7课（安全代码操作） LocalProcessSandbox 增强版）。

- asyncio subprocess + 硬超时（超时 SIGKILL）
- 敏感环境变量擦除（防 API Key 泄漏给子进程）
- 输出缓冲限制
- 高危命令的最终防线（双保险：SecurityGuard 之外再兜底一层）
- 支持单条命令自定义 timeout 与后台异步执行（长时命令轮询检查）
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from ..core.types import ToolDefinition
from ..security.guard import SENSITIVE_ENV_VARS

_DANGEROUS_PATTERNS = [
    r"rm\s+-rf\s+[/\~]",
    r"mkfs",
    r"dd\s+if=",
    r">\s*/dev/sd",
    r":\(\)\{\s*:\|\:&\s*\};:",
]

# 后台任务输出缓冲上限（单任务）
_BG_MAX_OUTPUT = 200_000


class _BackgroundTask:
    """后台任务：进程 + 输出累积 + 状态。"""

    def __init__(self, command: str) -> None:
        self.command = command
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.started_at = time.monotonic()
        self.stdout_buf = bytearray()
        self.stderr_buf = bytearray()
        self.exit_code: Optional[int] = None
        self.done = False
        self.timed_out = False
        self.readers: List[asyncio.Task] = []

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def close(self) -> None:
        for t in self.readers:
            if not t.done():
                t.cancel()


class ShellTools:
    def __init__(self, workspace: str, timeout_seconds: float = 60.0, max_output: int = 200_000) -> None:
        self.workspace = os.path.abspath(workspace)
        self.timeout_seconds = timeout_seconds
        self.max_output = max_output
        self._background: Dict[str, _BackgroundTask] = {}

    def get_tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="execute_command",
                description=(
                    "在受限 Shell 中执行命令行指令（敏感环境变量已擦除、高危命令被拦截）。"
                    "可选参数：timeout（秒，覆盖默认超时）；background=true 时后台执行并返回 task_id，"
                    "之后用 check_command 轮询结果，适合 git clone / pip install 等长时命令。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "要执行的 Shell 指令"},
                        "timeout": {"type": "number", "description": "超时秒数（可选，覆盖默认）"},
                        "background": {"type": "boolean", "description": "是否后台异步执行（可选，默认 false）"},
                    },
                    "required": ["command"],
                },
            ),
            ToolDefinition(
                name="check_command",
                description=(
                    "检查后台命令（execute_command 带 background=true 启动）的执行状态："
                    "返回 task_id / 是否完成 / 退出码 / 已累积输出。完成后任务自动清理。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "execute_command 返回的 task_id"},
                    },
                    "required": ["task_id"],
                },
            ),
        ]

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        if name == "check_command":
            return self._check_command(args)
        if name != "execute_command":
            raise ValueError(f"Unknown Shell Tool: {name}")
        command = args.get("command", "")
        timeout = float(args.get("timeout") or self.timeout_seconds)
        background = bool(args.get("background", False))

        # 最终防线：高危命令粗暴过滤
        import re

        for pattern in _DANGEROUS_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return f"[Security Blocked]: 命令命中高危模式 /{pattern}/ 被 Harness 拒绝。"

        # 剥离敏感环境变量，防止 API Key 泄漏给子进程
        clean_env = {k: v for k, v in os.environ.items() if k not in SENSITIVE_ENV_VARS}

        if background:
            return await self._start_background(command, timeout, clean_env)

        return await self._run_foreground(command, timeout, clean_env)

    # ------------------------------------------------------------ 前台执行

    async def _run_foreground(self, command: str, timeout: float, clean_env: Dict[str, str]) -> str:
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=self.workspace,
                env=clean_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
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
            parts.append(f"[Timed Out]: 命令超过 {timeout}s 被强制终止。")
        if out.strip():
            parts.append(f"[STDOUT]:\n{clamp(out.rstrip())}")
        if err.strip():
            parts.append(f"[STDERR]:\n{clamp(err.rstrip())}")
        if not out.strip() and not err.strip():
            parts.append("[No output]")
        return "\n".join(parts)

    # ------------------------------------------------------------ 后台执行

    async def _start_background(self, command: str, timeout: float, clean_env: Dict[str, str]) -> str:
        task_id = uuid.uuid4().hex[:12]
        task = _BackgroundTask(command)
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=self.workspace,
                env=clean_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            task.proc = proc
            # 读取器：持续把 stdout/stderr 累积到缓冲
            task.readers = [
                asyncio.create_task(self._drain(proc.stdout, task.stdout_buf)),
                asyncio.create_task(self._drain(proc.stderr, task.stderr_buf)),
            ]
            # 完成监控：进程结束或超时
            asyncio.create_task(self._monitor(task, timeout))
        except Exception as exc:
            task.close()
            return f"[Error]: 启动后台命令失败: {exc}"

        self._background[task_id] = task
        return (
            f"[Background Started]: task_id={task_id} 命令已后台执行（超时 {timeout}s）。\n"
            f"使用 check_command 工具轮询状态：{{\"task_id\": \"{task_id}\"}}"
        )

    @staticmethod
    async def _drain(stream: Optional[asyncio.StreamReader], buf: bytearray) -> None:
        if stream is None:
            return
        try:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                if len(buf) < _BG_MAX_OUTPUT:
                    buf.extend(chunk[:_BG_MAX_OUTPUT - len(buf)])
        except (asyncio.CancelledError, ConnectionResetError):
            pass

    async def _monitor(self, task: _BackgroundTask, timeout: float) -> None:
        try:
            await asyncio.wait_for(task.proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            task.timed_out = True
            try:
                task.proc.kill()
            except ProcessLookupError:
                pass
            try:
                await task.proc.wait()
            except Exception:
                pass
        finally:
            task.exit_code = task.proc.returncode if task.proc is not None else None
            task.done = True

    def _check_command(self, args: Dict[str, Any]) -> str:
        task_id = str(args.get("task_id", "")).strip()
        task = self._background.get(task_id)
        if task is None:
            return f"[Error]: 未找到后台任务 {task_id!r}（可能已清理或从未启动）。"

        out = task.stdout_buf.decode("utf-8", errors="replace")
        err = task.stderr_buf.decode("utf-8", errors="replace")

        def clamp(text: str) -> str:
            if len(text) > self.max_output:
                return text[: self.max_output] + f"\n... [输出截断: {len(text) - self.max_output} 字符省略]"
            return text

        parts = [f"[task_id]: {task_id}", f"[running]: {not task.done}", f"[elapsed]: {task.elapsed:.1f}s"]
        if task.done:
            parts.append(f"[Exit Code]: {task.exit_code}")
            if task.timed_out:
                parts.append(f"[Timed Out]: 后台命令超过超时时间被强制终止。")
            # 已完成：清理并返回完整输出
            if out.strip():
                parts.append(f"[STDOUT]:\n{clamp(out.rstrip())}")
            if err.strip():
                parts.append(f"[STDERR]:\n{clamp(err.rstrip())}")
            if not out.strip() and not err.strip():
                parts.append("[No output]")
            self._background.pop(task_id, None)
            task.close()
        else:
            # 运行中：返回已累积输出（截断）
            if out.strip():
                parts.append(f"[partial STDOUT]:\n{clamp(out.rstrip()[-4000:])}")
            if err.strip():
                parts.append(f"[partial STDERR]:\n{clamp(err.rstrip()[-2000:])}")
            if not out.strip() and not err.strip():
                parts.append("[No output yet]")
        return "\n".join(parts)
