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
        ctx.check_auth(request)
        from ...orchestration.collab_policy import list_collab_modes as _list

        return {"modes": _list(app)}

    @router.get("/api/collab/icon/{mode_name}")
    async def collab_mode_icon(mode_name: str, request: Request):
        """协作模式图标：本地覆盖包优先，回退内置包目录。"""
        ctx.check_auth(request)
        import mimetypes
        import os as _os

        from fastapi.responses import FileResponse

        from ...orchestration.collab_policy import _installed_mode_plugins
        from ...tools.plugin_loader import builtin_plugins_root, find_plugin_icon

        for plugin in _installed_mode_plugins(app):
            if plugin.mode_name != mode_name:
                continue
            local_root = _os.path.join(app.config_dir, "plugins")
            roots = (local_root, builtin_plugins_root()) if getattr(
                plugin, "_is_local_override", False) else (builtin_plugins_root(),)
            icon_path = find_plugin_icon(plugin.name, *roots)
            if icon_path:
                media, _ = mimetypes.guess_type(icon_path)
                return FileResponse(icon_path, media_type=media or "application/octet-stream",
                                    headers={"Cache-Control": "max-age=3600"})
            raise HTTPException(status_code=404, detail="该模式无图标")
        raise HTTPException(status_code=404, detail="未知的协作模式")

    @router.get("/api/agents")
    async def list_agents(request: Request):
        ctx.check_auth(request)
        return app.agents_meta()

    @router.get("/api/agents/tools")
    async def agents_tools(request: Request):
        """返回全部可用工具 + 各 agent 的工具白名单（供设置页配置）。"""
        ctx.check_auth(request)
        return app.agents_available_tools()

    @router.get("/api/agents/live")
    async def list_all_live_agents(request: Request):
        """全部会话的子 Agent 总览（**只读**）。

        只读是刻意的边界：跨会话不做消息投递、不做唤醒、不做关闭——那些操作
        必须回到 agent 所属的会话。此端点只解决「别的会话派的活看不到」。

        内存中没有 manager 的会话不会出现（不为查询而实例化 manager，否则
        会再造出 release_session 正要回收的东西）。
        """
        ctx.check_auth(request)
        try:
            return {"sessions": app.all_agents()}
        except Exception:  # noqa: BLE001 - 总览失败不阻断前端
            return {"sessions": {}}

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
