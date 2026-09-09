# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""FastAPI 服务：REST API + SSE 事件流 + 静态前端托管 + 可选 Bearer 鉴权。

路由拆分至 routers/ 包，本模块只保留应用工厂、CORS、lifespan 与静态托管。
"""
from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from .. import __version__
from ..app import AgentApp
from .routers import (
    create_agents_router,
    create_chat_router,
    create_llm_router,
    create_meta_router,
    create_office_router,
    create_security_router,
    create_sessions_router,
    create_skills_router,
    create_workspace_router,
)
from .routers.context import ServerContext, TokenAuth
from .tasks import TaskManager

logger = logging.getLogger("litework.server")

VERSION = __version__

# CORS 白名单：默认覆盖开发态 vite；额外来源经 LITEWORK_CORS_ORIGINS 追加
_DEFAULT_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


def _build_cors_origins() -> list[str]:
    origins = list(_DEFAULT_CORS_ORIGINS)
    extra = os.environ.get("LITEWORK_CORS_ORIGINS", "")
    for raw in extra.split(","):
        origin = raw.strip()
        if origin and origin not in origins:
            origins.append(origin)
    return origins


# ---------------------------------------------------------------- 应用工厂


def create_app(app: AgentApp, token: Optional[str] = None,
               cors_origins: Optional[list[str]] = None) -> FastAPI:
    """组装 FastAPI 应用。

    - token=None：鉴权关闭（库级默认；CLI 入口默认自动生成 token，见 cli.py）
    - cors_origins：覆盖默认白名单（测试用）
    """

    @asynccontextmanager
    async def _lifespan(_fast_app: FastAPI):
        # models.dev 元数据同步：启动零网络等待，进入系统后在后台线程刷新。
        # 缓存未过期（7 天）时后台任务也只是读盘；查询侧永远走缓存/内置表，
        # 刷新结果落盘后自然生效——任何时刻都不阻塞启动与用户操作。
        import asyncio as _asyncio

        def _bg_refresh():
            try:
                app.refresh_model_meta()
            except Exception:
                pass

        _refresh_task = _asyncio.create_task(_asyncio.to_thread(_bg_refresh))
        await app.mcp_manager.start()
        yield
        # 应用关闭时回收后台任务（to_thread 的线程无法强杀，仅标记取消）
        _refresh_task.cancel()

    fast_app = FastAPI(title="lite-work", version=VERSION, lifespan=_lifespan)
    auth = TokenAuth(token)
    tasks = TaskManager(app)
    ctx = ServerContext(app=app, tasks=tasks, auth=auth)

    fast_app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins if cors_origins is not None else _build_cors_origins(),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ------------------------------------------------------------ 路由注册

    fast_app.include_router(create_meta_router(ctx))
    fast_app.include_router(create_llm_router(ctx))
    fast_app.include_router(create_security_router(ctx))
    fast_app.include_router(create_sessions_router(ctx))
    fast_app.include_router(create_chat_router(ctx))
    fast_app.include_router(create_skills_router(ctx))
    fast_app.include_router(create_agents_router(ctx))
    fast_app.include_router(create_workspace_router(ctx))
    fast_app.include_router(create_office_router(ctx))

    # ------------------------------------------------------------ 静态前端

    dist_dir = _resolve_dist_dir()
    if os.path.isdir(dist_dir):
        @fast_app.get("/")
        async def index(request: Request):
            return await _serve_index(dist_dir)

        @fast_app.get("/{path:path}")
        async def spa_fallback(path: str, request: Request):
            if path.startswith("api/"):
                raise HTTPException(status_code=404, detail="Not Found")
            file_path = os.path.join(dist_dir, path)
            if os.path.isfile(file_path):
                return FileResponse(file_path)
            return await _serve_index(dist_dir)

    return fast_app


async def _serve_index(dist_dir: str):
    index_path = os.path.join(dist_dir, "index.html")
    if os.path.isfile(index_path):
        return FileResponse(index_path)
    return JSONResponse({"message": "lite-work 后端已就绪（前端未构建，运行 npm run build:web）"},
                        status_code=200)


def _resolve_dist_dir() -> str:
    """定位前端构建产物目录，兼容 PyInstaller 打包环境。"""
    candidates = []
    # PyInstaller: 前端资源通过 --add-data 打入 _MEIPASS
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        candidates.append(os.path.join(bundle_dir, "web", "dist"))
    # 开发/源码环境
    candidates.append(
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "web", "dist",
        )
    )
    for c in candidates:
        if os.path.isdir(c) and os.path.isfile(os.path.join(c, "index.html")):
            return c
    return candidates[-1]
