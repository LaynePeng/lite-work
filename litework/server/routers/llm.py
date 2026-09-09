# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""LLM 配置与上下文统计：/api/llm/*、/api/context/stats。"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from .context import ServerContext


class LLMConfigRequest(BaseModel):
    active: Optional[str] = None
    providers: Optional[Dict[str, Dict[str, Any]]] = None


class LLMTestRequest(BaseModel):
    provider_id: Optional[str] = None
    overrides: Optional[Dict[str, Any]] = None


def create_router(ctx: ServerContext) -> APIRouter:
    router = APIRouter()
    app = ctx.app

    @router.get("/api/llm/providers")
    async def llm_providers(request: Request):
        ctx.check_auth(request)
        return app.llm_provider_meta()

    @router.get("/api/llm/config")
    async def llm_config(request: Request):
        ctx.check_auth(request)
        return app.get_llm_config()

    @router.post("/api/llm/config")
    async def update_llm_config(payload: LLMConfigRequest, request: Request):
        ctx.check_auth(request)
        return app.update_llm_config(
            active=payload.active,
            providers=payload.providers,
        )

    @router.post("/api/llm/test")
    async def test_llm(payload: LLMTestRequest, request: Request):
        ctx.check_auth(request)
        result = await app.test_llm(
            provider_id=payload.provider_id or app.llm_registry.active,
            overrides=payload.overrides,
        )
        return result

    @router.get("/api/context/stats")
    async def context_stats(session_id: str = "", request: Request = None):
        if request:
            ctx.check_auth(request)
        if not session_id:
            return {"session": {}}
        # 会话生效模型（会话覆盖 > 全局默认）及对应上下文窗口，
        # 让前端在切换模型/重开会话时立即刷新「上下文情况」面板，而非等下一轮 SSE
        snapshot = app.session_store.load(session_id)
        override = snapshot.metadata.get("model") if snapshot else None
        if not isinstance(override, dict):
            override = None
        provider_id = override.get("provider") if override else app.llm_registry.active
        if override and override.get("model"):
            model_name = override["model"]
        else:
            model_name = app.llm_registry.get_active_provider_settings().get("model", "")
        return {
            "model": model_name,
            "context_window": app.llm_registry.get_context_window(provider_id, model_name),
            "session": app.get_context_session_stats(session_id),
        }

    return router
