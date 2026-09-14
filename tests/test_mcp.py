# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""MCP 客户端单测：全程 mock 子进程（不拉起真实进程、不依赖本机环境）。

原先用 `sys.executable` 起真实 stdio server，问题：
- 依赖 PATH 上有可用的 python/python3（CI 机器差异）；
- 真实进程启动 + 管道 IO 耗时（慢机/CI 超时抖动）；
- Windows .cmd 垫片场景只能在 Windows 上验证，其他平台直接跳过。

改为 FakeProcess（内存 StreamReader/Writer 对）注入 JSON-RPC 响应：
断言的是协议行为本身，因此运行快、与机器环境无关、跨平台一致。
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from litework.mcp import client as mcp_client
from litework.mcp.client import MCPClient
from litework.mcp.manager import MCPManager
from litework.tools.registry import ToolRegistry


class _FakeWriter:
    """StreamWriter 替身：写入的每一行直接交给 handler（无真实管道）。"""

    def __init__(self, on_line) -> None:
        self._on_line = on_line
        self.closed = False

    def write(self, data: bytes) -> None:
        for raw in data.splitlines():
            if raw.strip():
                self._on_line(json.loads(raw.decode("utf-8")))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    """asyncio.subprocess.Process 替身：协议响应由内存生成。"""

    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = _FakeWriter(self._handle)
        self.returncode = None
        self.terminated = False

    def _handle(self, message: dict) -> None:
        """按 MCP 协议应答：notification（无 id）不回包。"""
        req_id = message.get("id")
        if req_id is None:
            return
        method = message.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock"},
            }
        elif method == "tools/list":
            result = {"tools": [{
                "name": "hello",
                "description": "hello tool",
                "inputSchema": {"type": "object",
                                "properties": {"name": {"type": "string"}}},
            }]}
        elif method == "tools/call":
            name = message["params"]["arguments"].get("name", "")
            result = {"content": [{"type": "text", "text": "hello " + name}]}
        else:
            return
        payload = json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result})
        self.stdout.feed_data(payload.encode("utf-8") + b"\n")

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0
        self.stdout.feed_eof()

    def kill(self) -> None:
        self.terminated = True
        self.returncode = -9
        self.stdout.feed_eof()

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0


@pytest.fixture
def fake_exec(monkeypatch):
    """拦截 asyncio.create_subprocess_exec → 返回内存假进程（不起真实进程）。"""
    created: list = []

    async def _fake_exec(*args, **kwargs):
        proc = _FakeProcess()
        created.append({"args": args, "kwargs": kwargs, "proc": proc})
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    return created


async def _run_mock_flow(client: MCPClient) -> None:
    await client.start()
    try:
        tools = await client.list_tools()
        assert tools[0]["inputSchema"]["properties"]["name"]["type"] == "string"
        assert await client.call_tool("hello", {"name": "lite-work"}) == "hello lite-work"
    finally:
        await client.close()


async def test_mcp_stdio_tools(fake_exec):
    """initialize → tools/list → tools/call 全链路走 stdio 协议（mock 进程）。"""
    client = MCPClient("mock", "mock-server", ["--stdio"])
    await _run_mock_flow(client)

    assert len(fake_exec) == 1, "应通过 create_subprocess_exec 拉起一次"
    call = fake_exec[0]
    assert call["args"][0] == "mock-server" and call["args"][1] == "--stdio"
    # 三条管道都必须按 PIPE 打开，否则协议读写无从进行
    assert call["kwargs"]["stdin"] == asyncio.subprocess.PIPE
    assert call["kwargs"]["stdout"] == asyncio.subprocess.PIPE
    assert call["kwargs"]["stderr"] == asyncio.subprocess.PIPE
    # close() 收尾：子进程被 terminate 且 reader 任务不再挂着响应
    assert call["proc"].terminated


def test_resolve_bare_command_uses_which(monkeypatch):
    """裸命令必须经 shutil.which 补全为完整路径（Windows PATHEXT 关键路径）。

    原用例依赖真实 .cmd 垫片 + 真实进程，只能在 Windows 跑；改为 mock
    which 直接验证解析逻辑，三平台同款断言。
    """
    monkeypatch.setattr(mcp_client.shutil, "which",
                        lambda name: r"C:\npm\npx.cmd" if name == "npx" else None)
    assert MCPClient("mock", "npx")._resolve_command() == r"C:\npm\npx.cmd"


def test_resolve_command_keeps_explicit_path(monkeypatch):
    """已含路径分隔符（或为空）的命令不做 which 解析，原样透传。"""
    asked: list = []
    monkeypatch.setattr(mcp_client.shutil, "which",
                        lambda name: asked.append(name) or "should-not-be-used")

    explicit = os.path.join("some", "dir", "server")
    assert MCPClient("mock", explicit)._resolve_command() == explicit
    assert MCPClient("mock", "")._resolve_command() == ""
    assert asked == [], "显式路径/空命令不应查 which"


async def test_start_command_not_found_friendly_error(monkeypatch):
    """命令不存在 → 报错含原命令名与解析结果（替代裸 WinError 2）。"""

    async def _boom(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
    client = MCPClient("ghost", "definitely-not-a-real-command-xyz")
    with pytest.raises(FileNotFoundError) as exc_info:
        await client.start()
    message = str(exc_info.value)
    assert "ghost" in message and "definitely-not-a-real-command-xyz" in message
    await client.close()


def test_register_tools_bypasses_static_whitelist():
    """回归：MCP 工具名连接前不可知，Agent 静态 tools 白名单不得将其过滤。

    场景：build agent 白名单（不含任何 mcp_* 名字）+ 已连接的 mock server
    → mcp_mock_hello 仍必须注册进 Agent 注册表；exclude 显式排除仍生效。
    """
    class _FakeClient:
        name = "mock"

    manager = MCPManager({})
    manager.routes["mcp_mock_hello"] = (_FakeClient(), "hello")
    manager.tool_defs["mcp_mock_hello"] = {
        "name": "hello",
        "description": "hello tool",
        "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}},
    }

    # 修复前：allowed 非空且不含 mcp_mock_hello → 被跳过，Agent 看不到任何 MCP 工具
    registry = ToolRegistry()
    manager.register_tools(registry, allowed=["read_file", "write_file", "execute_command"])
    assert registry.has("mcp_mock_hello")

    # exclude 显式排除仍然生效
    registry_excluded = ToolRegistry()
    manager.register_tools(registry_excluded, allowed=None, exclude=["mcp_mock_hello"])
    assert not registry_excluded.has("mcp_mock_hello")
