from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from .. import __version__

logger = logging.getLogger("litework.mcp")


class MCPClient:
    def __init__(self, name: str, command: str, args: Optional[List[str]] = None, env: Optional[Dict[str, str]] = None) -> None:
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env
        self.process: Optional[asyncio.subprocess.Process] = None
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._next_id = 1

    def _resolve_command(self) -> str:
        """把裸命令解析为可直接执行的完整路径（Windows 关键）。

        CreateProcess 不做 PATHEXT 解析，`npx` 这类裸命令（实际是
        npx.cmd）会得到 [WinError 2] 系统找不到指定的文件；
        shutil.which 按 PATHEXT 补全扩展名后即可正常拉起。
        """
        command = self.command
        if not command or os.sep in command or (os.altsep and os.altsep in command):
            return command  # 已含路径分隔符：相对/绝对路径，交由系统处理
        return shutil.which(command) or command

    async def start(self) -> None:
        resolved = self._resolve_command()
        # GUI 场景（Electron → 后端 → MCP 子进程）避免弹出额外控制台窗口
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self.process = await asyncio.create_subprocess_exec(
                resolved, *self.args,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=self.env,
                creationflags=creationflags,
            )
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                f"找不到 MCP server `{self.name}` 的命令 `{self.command}`"
                f"（解析为 `{resolved}`）。请确认命令已安装且在 PATH 中，"
                "或直接填写完整路径。"
            ) from exc
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self.request("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "lite-work", "version": __version__},
        })
        await self.notify("notifications/initialized", {})

    async def _drain_stderr(self) -> None:
        """持续读取子进程 stderr 并记入日志——server 崩溃/缺依赖不再无声无息。"""
        if not self.process or not self.process.stderr:
            return
        try:
            async for raw in self.process.stderr:
                text = raw.decode("utf-8", "replace").strip()
                if text:
                    logger.warning("[MCP %s] stderr: %s", self.name, text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[MCP %s] stderr 读取异常", self.name)

    async def _read_loop(self) -> None:
        assert self.process and self.process.stdout
        reader = self.process.stdout
        while True:
            line = await reader.readline()
            if not line:
                return
            try:
                message = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            req_id = message.get("id")
            if req_id in self._pending:
                future = self._pending.pop(req_id)
                if "error" in message:
                    future.set_exception(RuntimeError(str(message["error"])))
                else:
                    future.set_result(message.get("result"))

    async def _send(self, message: Dict[str, Any]) -> None:
        if not self.process or not self.process.stdin:
            raise RuntimeError(f"MCP server {self.name} is not running")
        payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
        self.process.stdin.write(payload + b"\n")
        await self.process.stdin.drain()

    async def request(self, method: str, params: Dict[str, Any]) -> Any:
        req_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        await self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        return await asyncio.wait_for(future, timeout=30)

    async def notify(self, method: str, params: Dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def list_tools(self) -> List[Dict[str, Any]]:
        result = await self.request("tools/list", {})
        return (result or {}).get("tools") or []

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        result = await self.request("tools/call", {"name": name, "arguments": arguments})
        parts = []
        for item in (result or {}).get("content") or []:
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        return "\n".join(parts)

    async def close(self) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=3)
            except asyncio.TimeoutError:
                self.process.kill()
                await asyncio.wait_for(self.process.wait(), timeout=3)
        for task in (self._reader_task, self._stderr_task):
            if task:
                task.cancel()
        for task in (self._reader_task, self._stderr_task):
            if task:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        # 显式关闭管道：释放 Proactor transport，避免解释器退出时
        # 出现 "unclosed transport / Event loop is closed" 噪音
        if self.process:
            for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
                try:
                    if pipe:
                        pipe.close()
                except Exception:
                    pass
            # transport 无公开访问器（_transport 为 CPython 稳定内部属性），
            # 不 close 会在 GC 阶段产生 ResourceWarning
            transport = getattr(self.process, "_transport", None)
            if transport is not None:
                with contextlib.suppress(Exception):
                    transport.close()
