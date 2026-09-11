# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""服务元信息与全局配置：/api/status、/api/config。"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ... import __version__
from ...core.agent_loop import pricing_payload
from .context import ServerContext

VERSION = __version__


class ConfigUpdateRequest(BaseModel):
    updates: Dict[str, Any]


def create_router(ctx: ServerContext) -> APIRouter:
    router = APIRouter()
    app, tasks, token = ctx.app, ctx.tasks, ctx.auth.token

    @router.get("/api/status")
    async def status(request: Request):
        ctx.check_auth(request)
        llm_active = app.llm_registry.active
        settings = app.llm_registry.get_active_provider_settings()
        return {
            "version": VERSION,
            "workspace": app.workspace,
            "model": settings.get("model", ""),
            "base_url": settings.get("base_url", ""),
            "active_provider": llm_active,
            "api_key_configured": bool(settings.get("api_key")),
            "active_tasks": tasks.active_count(),
            "sessions_count": len(app.session_store.list()),
            "token_auth": bool(token),
        }

    @router.get("/api/config")
    async def get_config(request: Request):
        ctx.check_auth(request)
        return {
            k: app.config.get(k) for k in (
                "max_steps", "token_budget", "tool_timeout",
                "auto_approve", "pricing", "context_full_turns", "llm_timeout",
                "llm_retries", "skill_permissions", "subagent_timeout",
                # 多智能体（docs/multi-agent-design.md §3 配置面）
                "max_parallel_agents", "agent_total_limit", "agent_max_steps",
                "agent_max_steps_cap", "agent_spawn_depth", "agent_message_max_chars",
                "agent_meeting_rounds", "agent_ledger_interval", "agent_persist_max",
                "agent_collab_mode",
                # 协作模式（配方文本 / 模式名）
                "collab_policy", "collab_recipe",
            )
        }

    @router.post("/api/config")
    async def update_config(payload: ConfigUpdateRequest, request: Request):
        ctx.check_auth(request)
        app.save_config(payload.updates)
        return {"ok": True}

    # ------------------------------------------------------------ 模型元数据

    @router.get("/api/model-meta")
    async def model_meta(request: Request):
        """models.dev 元数据缓存状态（设置页展示同步情况）。"""
        ctx.check_auth(request)
        return app.model_meta_status()

    @router.post("/api/model-meta/refresh")
    async def refresh_model_meta(request: Request):
        """手动同步 models.dev 元数据（离线启动后无缓存时的兜底入口）。

        拉取是阻塞 IO → 丢到线程执行避免卡住事件循环；返回同步后的缓存状态
        与当前会话生效模型的计费单价，便于直接和账单对账。
        """
        ctx.check_auth(request)
        ok = await asyncio.to_thread(app.refresh_model_meta)
        active = app.llm_registry.active
        model = app.llm_registry.get_active_provider_settings().get("model", "")
        return {
            "ok": bool(ok),
            **app.model_meta_status(),
            "pricing": pricing_payload(app.resolve_pricing(active, model)),
        }

    return router
