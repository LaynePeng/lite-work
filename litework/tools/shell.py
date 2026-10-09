# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""受限本地 Shell 沙箱（对应课程第7课（安全代码操作） LocalProcessSandbox 增强版）。

- asyncio subprocess + 硬超时（超时 SIGKILL）
- 敏感环境变量擦除（防 API Key 泄漏给子进程）
- 输出缓冲限制
- 高危命令的最终防线（双保险：SecurityGuard 之外再兜底一层）
- 支持单条命令自定义 timeout 与后台异步执行（长时命令轮询检查）
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from ..core.types import ToolDefinition
from ..security.guard import SENSITIVE_ENV_VARS
from .proc_utils import kill_process_tree, posix_spawn_kwargs

logger = logging.getLogger("litework.tools.shell")

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
        # 墙钟启动时间（任务日志用；started_at 是 monotonic，跨进程无意义）
        self.wall_started_at = time.time()
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


class BackgroundRegistry:
    """后台命令注册表（app 级共享，跨 ShellTools 实例）。

    为什么共享：插件按 workspace 为键缓存（AgentApp._shell_plugins），切换工作区
    会取到另一个 ShellTools 实例。若注册表随实例走，切换后 check_command /
    任务面板就查不到在旧工作区启动的 task_id（任务失联）。把它提到实例之外共享，
    **任务归属与工作区解耦**：切换项目后仍可查询、终止旧任务。

    这与 OpenCode「目录随请求、实例按目录缓存」同构的收敛方向一致——区别只是
    我们让后台任务不绑定目录，而不是让目录随请求流动。

    任务日志（journal_path）：add/remove 时把在跑的任务写到磁盘，供**下次启动
    回收孤儿进程**用（服务端 TTL 兜底——Agent 可能因崩溃/断连失去管理者）。写失败一律吞掉，绝不因为日志影响工具执行。
    """

    def __init__(self, journal_path: Optional[str] = None) -> None:
        self.tasks: Dict[str, _BackgroundTask] = {}
        self.journal_path = journal_path
        # task_id → 启动它的工作区（日志用；注册表本身与工作区无关）
        self.workspaces: Dict[str, str] = {}
        # M1 线程安全：add/remove 可能从不同 agent loop 并发调用（工具普遍经
        # to_thread 执行）；journal_payload 迭代 tasks 期间并发修改会
        # RuntimeError: dictionary changed size during iteration
        self._lock = threading.RLock()

    def add(self, task_id: str, task: _BackgroundTask, workspace: str = "") -> None:
        with self._lock:
            self.tasks[task_id] = task
            self.workspaces[task_id] = workspace
            self._write_journal()

    def get(self, task_id: str) -> Optional[_BackgroundTask]:
        return self.tasks.get(task_id)

    def remove(self, task_id: str) -> None:
        with self._lock:
            self.tasks.pop(task_id, None)
            self.workspaces.pop(task_id, None)
            self._write_journal()

    def journal_payload(self) -> Dict[str, Any]:
        """当前在跑任务的持久化快照（回收时按 pid + 启动时间核对，防 pid 复用）。"""
        with self._lock:
            items = [(tid, t, self.workspaces.get(tid, "")) for tid, t in self.tasks.items()]
        return {"tasks": [{
            "task_id": tid,
            "pid": t.proc.pid if t.proc is not None else None,
            "command": t.command[:500],
            "workspace": ws,
            "started_at": t.wall_started_at,
        } for tid, t, ws in items]}

    def _write_journal(self) -> None:
        if not self.journal_path:
            return
        try:
            parent = os.path.dirname(self.journal_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = f"{self.journal_path}.{uuid.uuid4().hex[:8]}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.journal_payload(), f, ensure_ascii=False)
            os.replace(tmp, self.journal_path)
        except OSError:
            logger.debug("[bg] 写后台任务日志失败", exc_info=True)

    def list(self) -> List[Dict[str, Any]]:
        """列出所有后台任务（供工具面板「后台」tab 展示）。"""
        with self._lock:
            items = list(self.tasks.items())
        return [{
            "task_id": tid,
            "command": t.command[:300],
            "running": not t.done,
            "elapsed": round(t.elapsed, 1),
            "exit_code": t.exit_code,
        } for tid, t in items]

    def kill(self, task_id: str) -> bool:
        """终止指定后台任务（返回是否成功发起终止）。"""
        task = self.get(task_id)
        if task is None or task.done or task.proc is None:
            return False
        try:
            kill_process_tree(task.proc.pid)
            return True
        except ProcessLookupError:
            return False


class ShellTools:
    def __init__(
        self,
        workspace: str,
        timeout_seconds: float = 60.0,
        max_output: int = 200_000,
        registry: Optional[BackgroundRegistry] = None,
    ) -> None:
        self.workspace = os.path.abspath(workspace)
        self.timeout_seconds = timeout_seconds
        self.max_output = max_output
        # 注册表可由调用方注入共享（app 级）；缺省自建（单实例/测试场景）
        self._registry = registry or BackgroundRegistry()

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
                **posix_spawn_kwargs(),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            out = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace")
            timed_out = False
            exit_code = proc.returncode
        except asyncio.TimeoutError:
            kill_process_tree(proc.pid)
            out, err = "", ""
            timed_out = True
            exit_code = 124
        except asyncio.CancelledError:
            # 外层取消（agent_loop wait_for 超时 / 任务 stop）：杀整棵进程树
            # 再抛——否则 shell 的孙进程（npm / pyinstaller 等）成孤儿继续占资源
            kill_process_tree(proc.pid)
            raise

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
                **posix_spawn_kwargs(),
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

        self._registry.add(task_id, task, workspace=self.workspace)
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
        # 调用点保证 proc 已赋值（_start_background 先 task.proc = proc 再调度）；
        # 这里做一次局部收窄，兼作进程未创建时的兜底
        proc = task.proc
        if proc is None:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            task.timed_out = True
            kill_process_tree(proc.pid)
            try:
                await proc.wait()
            except Exception:
                pass
        finally:
            task.exit_code = proc.returncode
            task.done = True

    def _check_command(self, args: Dict[str, Any]) -> str:
        task_id = str(args.get("task_id", "")).strip()
        task = self._registry.get(task_id)
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
            self._registry.remove(task_id)
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

    def list_background(self) -> List[Dict[str, Any]]:
        """列出所有后台任务（供右侧工具面板「后台」tab 展示）。

        委托共享注册表：列出的是 app 全部工作区的后台任务，不是本实例目录的。
        """
        return self._registry.list()

    def kill_background(self, task_id: str) -> bool:
        """杀掉指定后台任务（供面板「杀掉」按钮）。返回是否成功发起终止。

        委托共享注册表：任意工作区的实例都能终止其它工作区启动的任务。
        """
        return self._registry.kill(task_id)


# ------------------------------------------------------------ 孤儿回收（启动兜底）

def _pid_alive(pid: int) -> bool:
    """pid 是否仍存在（POSIX：信号 0；Windows：tasklist 探活）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                                 capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但不属于我们：保守当作存活，交给启动时间比对
    return True


def _process_age_seconds(pid: int) -> Optional[float]:
    """进程已存活秒数（读 `ps -o etime=`，macOS/Linux 通用；GNU 的 etimes 不可移植）。

    解析 `[[dd-]hh:]mm:ss`；拿不到返回 None（调用方保守跳过，不杀）。
    """
    try:
        out = subprocess.run(["ps", "-o", "etime=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    days = 0
    raw = out
    if "-" in raw:                      # dd-hh:mm:ss
        d, raw = raw.split("-", 1)
        days = int(d or 0)
    parts = raw.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:                  # mm:ss
        hh, mm, ss = 0, nums[0], nums[1]
    elif len(nums) == 3:                # hh:mm:ss
        hh, mm, ss = nums
    else:
        return None
    return days * 86400 + hh * 3600 + mm * 60 + ss


def read_journal(journal_path: str) -> List[Dict[str, Any]]:
    """读任务日志；文件缺失/损坏返回空列表（启动路径不允许因日志失败而中断）。"""
    try:
        with open(journal_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    tasks = data.get("tasks") if isinstance(data, dict) else None
    return [t for t in tasks if isinstance(t, dict)] if isinstance(tasks, list) else []


def reclaim_orphaned_tasks(journal_path: str, tolerance_seconds: float = 60.0) -> Dict[str, Any]:
    """回收上一次运行遗留的后台命令（服务端 TTL 兜底）。

    判定规则（宁可漏杀，不可误杀）：
    - pid 不存在 → 跳过（进程自己结束了）；
    - 拿不到进程存活时间 → 跳过并记日志（无法确认身份）；
    - 存活时间与日志记录不符（差 > tolerance，说明 pid 已被复用）→ 跳过；
    - 命中 → kill_process_tree（连带整棵进程树）。
    无论结果如何都清空日志（避免下次重复判定）。
    """
    entries = read_journal(journal_path)
    summary: Dict[str, Any] = {"checked": len(entries), "killed": 0,
                               "killed_pids": [], "skipped": 0}
    now = time.time()
    for entry in entries:
        pid = entry.get("pid")
        started_at = entry.get("started_at")
        if not isinstance(pid, int) or pid <= 0 or not isinstance(started_at, (int, float)):
            summary["skipped"] += 1
            continue
        if not _pid_alive(pid):
            summary["skipped"] += 1
            continue
        age = _process_age_seconds(pid)
        if age is None:
            logger.warning("[bg] 无法确认进程 %s 存活时间，跳过回收（task_id=%s）",
                           pid, entry.get("task_id"))
            summary["skipped"] += 1
            continue
        if abs(age - (now - float(started_at))) > tolerance_seconds:
            # pid 已被系统复用给别的进程：绝不能杀
            summary["skipped"] += 1
            continue
        kill_process_tree(pid)
        summary["killed"] += 1
        summary["killed_pids"].append(pid)
        logger.warning("[bg] 已回收遗留后台命令 pid=%s task_id=%s command=%s",
                       pid, entry.get("task_id"), str(entry.get("command"))[:120])
    try:
        os.remove(journal_path)
    except OSError:
        pass
    return summary
