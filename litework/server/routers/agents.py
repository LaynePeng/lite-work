# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""Agent 配置管理：/api/agents/*。"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .context import ServerContext


class AgentSaveRequest(BaseModel):
    profile: Dict[str, Any]               # AgentProfile 字段（id/description/prompt/tools/permissions）


def create_router(ctx: ServerContext) -> APIRouter:
    router = APIRouter()
    app = ctx.app

    @router.get("/api/collab/modes")
    async def list_collab_modes(request: Request):
        """协作模式选择器数据源：内置策略 + 已安装模式插件（kind=collab 社区包）。"""
        ctx.check_auth(request)
        from ...orchestration.collab_policy import list_collab_modes as _list

        return {"modes": _list(app)}

    @router.get("/api/agents")
    async def list_agents(request: Request):
        ctx.check_auth(request)
        return app.agents_meta()

    @router.get("/api/agents/tools")
    async def agents_tools(request: Request):
        """返回全部可用工具 + 各 agent 的工具白名单（供设置页配置）。"""
        ctx.check_auth(request)
        return app.agents_available_tools()

    @router.post("/api/agents/save")
    async def save_agent(payload: AgentSaveRequest, request: Request):
        ctx.check_auth(request)
        try:
            return app.agent_save(payload.profile)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @router.delete("/api/agents/{agent_id}")
    async def delete_agent(agent_id: str, request: Request):
        ctx.check_auth(request)
        try:
            return app.agent_delete(agent_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    return router
