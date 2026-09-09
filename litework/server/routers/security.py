# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""安全规则与 MCP Server 配置：/api/security、/api/mcp。"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .context import ServerContext


class SecurityUpdateRequest(BaseModel):
    rules: Dict[str, Any]


class MCPServersUpdateRequest(BaseModel):
    servers: Dict[str, Any]


def create_router(ctx: ServerContext) -> APIRouter:
    router = APIRouter()
    app, tasks = ctx.app, ctx.tasks

    @router.get("/api/security")
    async def get_security(request: Request):
        ctx.check_auth(request)
        return app.guard.to_dict()

    @router.post("/api/security")
    async def update_security(payload: SecurityUpdateRequest, request: Request):
        ctx.check_auth(request)
        app.update_security_rules(payload.rules)
        return {"ok": True}

    @router.get("/api/mcp")
    async def mcp_status(request: Request):
        ctx.check_auth(request)
        return app.mcp_status()

    @router.post("/api/mcp")
    async def update_mcp(payload: MCPServersUpdateRequest, request: Request):
        ctx.check_auth(request)
        # 任务运行时工具集不可热变（运行中任务/后台子 Agent 的 registry 已装配完成，
        # reload 会 close 它们捕获的 MCP 连接）
        if tasks.active_count() > 0:
            raise HTTPException(status_code=409, detail="当前有任务运行，请等待任务结束后再更新 MCP 配置")
        bg = app.background_agent_count()
        if bg > 0:
            raise HTTPException(status_code=409, detail=f"当前有 {bg} 个后台 Agent 运行中，请等待结束后再更新 MCP 配置")
        status = await app.update_mcp_servers(payload.servers)
        return {"ok": True, **status}

    return router
