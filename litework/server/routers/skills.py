# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""技能 / 插件 / 命令管理：/api/skills*、/api/plugins*、/api/commands。

路由统一声明为**同步**处理函数（`def`）而非 `async def`：本模块的操作都是
阻塞式的（社区 manifest 拉取 / GitHub zipball 下载 / 插件依赖 pip 安装 /
技能与插件目录扫描），FastAPI 会把同步处理函数放到线程池执行，绝不阻塞
uvicorn 事件循环。此前写成 `async def` 却在事件循环里跑阻塞 I/O：网络差时
单个「检查社区更新」可卡死整个内核事件循环（其余请求全部排队，前端 15s
超时中断 → 表现为「signal 无理由中断」）。
"""
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
    def list_skills(request: Request):
        ctx.check_auth(request)
        try:
            return {"skills": app.skills_list()}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @router.get("/api/skills/{name}")
    def read_skill(name: str, request: Request):
        ctx.check_auth(request)
        content = app.skills_read(name)
        if content is None:
            raise HTTPException(status_code=404, detail=f"技能 {name!r} 不存在")
        return {"name": name, "content": content}

    @router.post("/api/skills/create")
    def create_skill(payload: SkillCreateRequest, request: Request):
        ctx.check_auth(request)
        try:
            return app.skills_create(payload.name, payload.description, payload.scope)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.post("/api/skills/import")
    def import_skill(payload: SkillImportRequest, request: Request):
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
    def update_skill(name: str, payload: SkillUpdateRequest, request: Request):
        ctx.check_auth(request)
        try:
            return app.skills_update(name, payload.description, payload.scope)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.delete("/api/skills/{name}")
    def delete_skill(name: str, request: Request, scope: str = "workspace"):
        ctx.check_auth(request)
        try:
            return app.skills_delete(name, scope)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # ------------------------------------------------------------ Plugins 管理

    @router.get("/api/plugins")
    def list_plugins(request: Request):
        ctx.check_auth(request)
        try:
            return {"plugins": app.plugins_list()}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @router.get("/api/plugins/builtin")
    def list_builtin_plugins(request: Request):
        ctx.check_auth(request)
        try:
            return {"plugins": app.plugins_builtin()}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @router.get("/api/plugins/community")
    def community_plugins(url: str = "", request: Request = None):
        if request:
            ctx.check_auth(request)
        try:
            return app.plugins_community(url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @router.post("/api/plugins/import")
    def import_plugin(payload: PluginImportRequest, request: Request):
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
    def plugin_icon(name: str, request: Request = None):
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
    def delete_plugin(name: str, request: Request):
        ctx.check_auth(request)
        try:
            return app.plugins_delete(name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.get("/api/commands")
    def list_commands(request: Request):
        ctx.check_auth(request)
        return {"commands": app.commands_list()}

    # ------------------------------------------------------------ 安装任务（带步骤 + 进度）

    def _is_remote(source: Optional[str]) -> bool:
        return (source or "").startswith(("http://", "https://"))

    @router.post("/api/install/plugin")
    def start_install_plugin(payload: PluginImportRequest, request: Request):
        """后台安装插件，返回 job_id；前端轮询进度条（步骤 + 下载百分比）。"""
        ctx.check_auth(request)
        import base64 as _b64

        from ...tools.install_progress import LOCAL_STEPS, PLUGIN_STEPS
        from ..install_jobs import run_job

        def _work():
            if payload.zip_base64:
                return app.plugins_import_zip(
                    _b64.b64decode(payload.zip_base64), payload.name, payload.overwrite)
            if not payload.source:
                raise ValueError("需要 source（目录/zip/GitHub URL）或 zip_base64")
            return app.plugins_install(
                payload.source, payload.name, payload.overwrite, payload.version)

        steps = PLUGIN_STEPS if _is_remote(payload.source) else LOCAL_STEPS
        job = run_job(ctx.jobs, "plugin", steps, _work)
        return {"job_id": job.id}

    @router.post("/api/install/skill")
    def start_install_skill(payload: SkillImportRequest, request: Request):
        """后台安装/更新技能，返回 job_id；前端轮询进度条。"""
        ctx.check_auth(request)
        import base64 as _b64

        from ...tools.install_progress import LOCAL_STEPS, SKILL_STEPS
        from ..install_jobs import run_job

        def _work():
            if payload.zip_base64:
                max_bytes = app.config.get("max_zip_size_mb", 20) * 1024 * 1024
                data = _b64.b64decode(payload.zip_base64)
                if len(data) > max_bytes:
                    raise ValueError(f"zip 文件过大（{len(data) / 1024 / 1024:.1f}MB）")
                return app.skills_import_zip(data, payload.scope, payload.name, payload.overwrite)
            if not payload.source:
                raise ValueError("需要 source（目录/zip/GitHub URL）或 zip_base64")
            return app.skills_import(payload.source, payload.scope, payload.name, payload.overwrite)

        steps = SKILL_STEPS if _is_remote(payload.source) else LOCAL_STEPS
        job = run_job(ctx.jobs, "skill", steps, _work)
        return {"job_id": job.id}

    @router.get("/api/install/jobs/{job_id}")
    def install_job_status(job_id: str, request: Request):
        ctx.check_auth(request)
        snap = ctx.jobs.snapshot(job_id)
        if snap is None:
            raise HTTPException(status_code=404, detail="安装任务不存在")
        return snap

    @router.post("/api/install/jobs/{job_id}/pause")
    def install_job_pause(job_id: str, request: Request):
        """暂停安装：下载线程停在协作检查点，断点保留，可 resume 继续。"""
        ctx.check_auth(request)
        if not ctx.jobs.request_pause(job_id):
            raise HTTPException(status_code=409, detail="任务不在运行中，无法暂停")
        return {"ok": True, "status": "paused"}

    @router.post("/api/install/jobs/{job_id}/resume")
    def install_job_resume(job_id: str, request: Request):
        """恢复已暂停的安装。"""
        ctx.check_auth(request)
        if not ctx.jobs.request_resume(job_id):
            raise HTTPException(status_code=409, detail="任务不在暂停状态")
        return {"ok": True, "status": "running"}

    @router.post("/api/install/jobs/{job_id}/cancel")
    def install_job_cancel(job_id: str, request: Request):
        """取消安装：worker 在下一个检查点退出，并清理下载缓存（含断点 .part）。"""
        ctx.check_auth(request)
        if not ctx.jobs.request_cancel(job_id):
            raise HTTPException(status_code=409, detail="任务已结束，无法取消")
        return {"ok": True, "status": "cancelling"}

    return router
