# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from .client import MCPClient

logger = logging.getLogger("litework.mcp")


class MCPManager:
    def __init__(self, configs: Dict[str, Any]) -> None:
        self.configs = configs or {}
        self.clients: Dict[str, MCPClient] = {}
        self.routes: Dict[str, tuple[MCPClient, str]] = {}
        self.tool_defs: Dict[str, Dict[str, Any]] = {}
        self.start_errors: Dict[str, str] = {}

    def status(self) -> Dict[str, Any]:
        """配置与运行状态（供设置界面展示）。"""
        servers = []
        for name, config in (self.configs or {}).items():
            if not isinstance(config, dict):
                continue
            client = self.clients.get(name)
            tools = [
                exposed for exposed, (_c, _raw) in self.routes.items()
                if exposed.startswith(f"mcp_{name}_") or exposed.startswith(f"mcp_{name.replace('-', '_')}_")
            ]
            servers.append({
                "name": name,
                "command": config.get("command", ""),
                "args": config.get("args") or [],
                "enabled": config.get("enabled") is not False,
                "connected": client is not None,
                "error": self.start_errors.get(name),
                "tools": sorted(tools),
            })
        return {"servers": servers}

    async def reload(self, configs: Dict[str, Any]) -> Dict[str, Any]:
        """热重载：断开全部连接，按新配置重连。"""
        self.configs = configs or {}
        await self.close()
        self.start_errors.clear()
        await self.start()
        return self.status()

    async def start(self) -> None:
        for server_name, config in self.configs.items():
            if not isinstance(config, dict) or config.get("enabled") is False:
                continue
            command = config.get("command")
            if not command:
                continue
            env = {**os.environ, **{str(k): str(v) for k, v in (config.get("env") or {}).items()}}
            client = MCPClient(server_name, command, config.get("args") or [], env)
            try:
                await client.start()
                self.clients[server_name] = client
                for tool in await client.list_tools():
                    raw_name = tool.get("name", "")
                    if not raw_name:
                        continue
                    exposed = f"mcp_{server_name}_{raw_name}".replace("-", "_")
                    self.routes[exposed] = (client, raw_name)
                    self.tool_defs[exposed] = tool
            except Exception as exc:
                logger.exception("[MCP] 启动 server 失败: %s", server_name)
                self.start_errors[server_name] = str(exc)
                await client.close()

    def register_tools(self, registry, allowed=None, exclude=None) -> None:
        for exposed, (client, raw_name) in self.routes.items():
            if allowed is not None and exposed not in allowed:
                continue
            if exclude and exposed in exclude:
                continue
            meta = self.tool_defs.get(exposed, {})
            registry.register(
                exposed,
                f"MCP {client.name}: {meta.get('description') or raw_name}",
                meta.get("inputSchema") or {"type": "object", "properties": {}},
                lambda args, c=client, n=raw_name: c.call_tool(n, args),
            )

    async def close(self) -> None:
        for client in self.clients.values():
            await client.close()
        self.clients.clear()
        self.routes.clear()
        self.tool_defs.clear()
