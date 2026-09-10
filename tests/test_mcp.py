# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

import asyncio
import json
import os
import shutil
import sys

import pytest

from litework.mcp.client import MCPClient
from litework.mcp.manager import MCPManager
from litework.tools.registry import ToolRegistry

MOCK_SERVER_SOURCE = '''import json, sys
for line in sys.stdin:
    req = json.loads(line)
    method = req.get("method")
    if method == "notifications/initialized":
        continue
    if method == "initialize":
        result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "mock"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "hello", "description": "hello tool", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "hello " + req["params"]["arguments"].get("name", "")}]}
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}), flush=True)
'''


async def _run_mock_flow(client: MCPClient) -> None:
    await client.start()
    try:
        tools = await client.list_tools()
        assert tools[0]["inputSchema"]["properties"]["name"]["type"] == "string"
        assert await client.call_tool("hello", {"name": "lite-work"}) == "hello lite-work"
    finally:
        await client.close()


async def test_mcp_stdio_tools(tmp_path):
    server = tmp_path / "server.py"
    server.write_text(MOCK_SERVER_SOURCE, encoding="utf-8")
    client = MCPClient("mock", sys.executable, [str(server)])
    await _run_mock_flow(client)


def test_resolve_bare_command_finds_executable():
    """裸命令经 shutil.which 解析为带扩展名的完整路径（Windows PATHEXT）。"""
    client = MCPClient("mock", "python")
    resolved = client._resolve_command()
    if os.name == "nt":
        assert resolved.lower().endswith((".exe", ".cmd", ".bat")), resolved
        assert os.path.isabs(resolved), resolved
    elif shutil.which("python"):
        assert os.path.isabs(resolved)


async def test_mcp_stdio_via_cmd_shim(tmp_path, monkeypatch):
    """Windows 主场景复现：配置裸命令（如 npx），实际是 .cmd —— 必须能拉起。

    用一个 .cmd 垫片（内部转调 python mock server）模拟 npx.cmd，
    垫片目录加入 PATH，裸命令启动，完整跑通 initialize/list/call。
    """
    if os.name != "nt":
        pytest.skip("仅 Windows 需要 .cmd 垫片场景")
    server = tmp_path / "server.py"
    server.write_text(MOCK_SERVER_SOURCE, encoding="utf-8")
    shim = tmp_path / "mock-mcp-cmd.cmd"
    # 引号包裹含空格路径；%~dp0 保证 shim/server 同目录
    shim.write_text(f'@echo off\r\n"{sys.executable}" "%~dp0server.py" %*\r\n', encoding="utf-8")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    client = MCPClient("mock", "mock-mcp-cmd")  # 裸命令，不带扩展名
    await _run_mock_flow(client)


async def test_start_command_not_found_friendly_error(tmp_path):
    """命令不存在时报错应包含原命令名与解析结果（替代裸 WinError 2）。"""
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
