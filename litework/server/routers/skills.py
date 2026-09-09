# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""技能 / 插件 / 命令管理：/api/skills*、/api/plugins*、/api/commands。"""
from __future__ import annotations

import base64
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .context import ServerContext


class SkillCreateRequest(BaseModel):
    name: str
    description: str
    scope: str = "workspace"


class SkillImportRequest(BaseModel):
    source: Optional[str] = None          # 本地目录 / zip 文件路径 / GitHub URL
    zip_base64: Optional[str] = None      # 前端上传的 zip（base64）
    scope: str = "workspace"
    name: Optional[str] = None            # 覆盖技能名（单技能来源时）
    overwrite: bool = False               # 同名技能覆盖更新（保留本地 .env）


class SkillUpdateRequest(BaseModel):
    description: str                      # 新的技能描述
    scope: str = "workspace"


class PluginImportRequest(BaseModel):
    source: Optional[str] = None          # 本地目录 / zip 文件路径 / GitHub URL（含子目录）
    zip_base64: Optional[str] = None      # 前端上传的 zip（base64）
    name: Optional[str] = None            # 覆盖插件名（单插件来源时）
    overwrite: bool = False               # 同名插件已存在时覆盖安装（更新）
    version: Optional[str] = None         # 来源版本（用于 installed.json 记录）


def create_router(ctx: ServerContext) -> APIRouter:
    router = APIRouter()
    app = ctx.app

    # ------------------------------------------------------------ Skills 与命令

    @router.get("/api/skills")
    async def list_skills(request: Request):
        ctx.check_auth(request)
        try:
            return {"skills": app.skills_list()}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @router.get("/api/skills/{name}")
    async def read_skill(name: str, request: Request):
        ctx.check_auth(request)
        content = app.skills_read(name)
        if content is None:
            raise HTTPException(status_code=404, detail=f"技能 {name!r} 不存在")
        return {"name": name, "content": content}

    @router.post("/api/skills/create")
    async def create_skill(payload: SkillCreateRequest, request: Request):
        ctx.check_auth(request)
        try:
            return app.skills_create(payload.name, payload.description, payload.scope)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.post("/api/skills/import")
    async def import_skill(payload: SkillImportRequest, request: Request):
        ctx.check_auth(request)
        try:
            if payload.zip_base64:
                # 检查 zip 大小（base64 编码膨胀约 1.33 倍）
                max_bytes = app.config.get("max_zip_size_mb", 20) * 1024 * 1024
                raw_size = len(payload.zip_base64) * 3 // 4
                if raw_size > max_bytes:
                    max_mb = max_bytes // (1024 * 1024)
                    raise ValueError(f"zip 文件过大（{raw_size / 1024 / 1024:.1f}MB），上限为 {max_mb}MB")
                data = base64.b64decode(payload.zip_base64)
                return {"skills": app.skills_import_zip(data, payload.scope, payload.name, payload.overwrite)}
            if payload.source:
                return {"skills": app.skills_import(payload.source, payload.scope, payload.name, payload.overwrite)}
            raise ValueError("需要 source（目录/zip/GitHub URL）或 zip_base64")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @router.put("/api/skills/{name}")
    async def update_skill(name: str, payload: SkillUpdateRequest, request: Request):
        ctx.check_auth(request)
        try:
            return app.skills_update(name, payload.description, payload.scope)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.delete("/api/skills/{name}")
    async def delete_skill(name: str, request: Request, scope: str = "workspace"):
        ctx.check_auth(request)
        try:
            return app.skills_delete(name, scope)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # ------------------------------------------------------------ Plugins 管理

    @router.get("/api/plugins")
    async def list_plugins(request: Request):
        ctx.check_auth(request)
        try:
            return {"plugins": app.plugins_list()}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @router.get("/api/plugins/builtin")
    async def list_builtin_plugins(request: Request):
        ctx.check_auth(request)
        try:
            return {"plugins": app.plugins_builtin()}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @router.get("/api/plugins/community")
    async def community_plugins(url: str = "", request: Request = None):
        if request:
            ctx.check_auth(request)
        try:
            return app.plugins_community(url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @router.post("/api/plugins/import")
    async def import_plugin(payload: PluginImportRequest, request: Request):
        ctx.check_auth(request)
        try:
            if payload.zip_base64:
                import base64 as _base64
                raw = _base64.b64decode(payload.zip_base64)
                return {"plugins": app.plugins_import_zip(raw, payload.name, payload.overwrite)}
            if payload.source:
                return {"plugins": app.plugins_install(
                    payload.source, payload.name, payload.overwrite, payload.version)}
            raise ValueError("需要 source（目录/zip/GitHub URL）或 zip_base64")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @router.get("/api/plugins/{name}/icon")
    async def plugin_icon(name: str, request: Request = None):
        """插件图标（icon.svg/png/...，目录插件可选自带）。

        前端 <img> 加载失败自行回退默认图标；404 即无图标。
        """
        if request:
            ctx.check_auth(request)
        from ...tools.plugin_loader import find_plugin_icon

        icon_path = find_plugin_icon(name, os.path.join(app.config_dir, "plugins"))
        if icon_path is None:
            raise HTTPException(status_code=404, detail="插件无图标")
        import mimetypes

        media_type, _ = mimetypes.guess_type(icon_path)
        from fastapi.responses import FileResponse

        return FileResponse(icon_path, media_type=media_type or "application/octet-stream",
                            headers={"Cache-Control": "max-age=3600"})

    @router.delete("/api/plugins/{name}")
    async def delete_plugin(name: str, request: Request):
        ctx.check_auth(request)
        try:
            return app.plugins_delete(name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.get("/api/commands")
    async def list_commands(request: Request):
        ctx.check_auth(request)
        return {"commands": app.commands_list()}

    return router
