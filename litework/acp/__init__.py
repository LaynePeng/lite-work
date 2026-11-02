# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""ACP（Agent Client Protocol）支持：让编辑器把 lite-work 当 Agent 驱动。

入口：``lite-work acp``（stdio 子进程，由客户端拉起）。
协议：https://agentclientprotocol.com —— JSON-RPC 2.0 over stdio。
"""
from .agent import PROTOCOL_VERSION, AcpAgent, AcpSession
from .transport import RpcError, StdioTransport

__all__ = ["PROTOCOL_VERSION", "AcpAgent", "AcpSession", "RpcError", "StdioTransport"]
