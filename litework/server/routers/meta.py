# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""服务元信息与全局配置：/api/status、/api/config。"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ... import __version__
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
                # 协作模式（Tier 1 配方文本 / Tier 2 模式名，见 orchestration/collab_policy.py）
                "collab_policy", "collab_recipe",
            )
        }

    @router.post("/api/config")
    async def update_config(payload: ConfigUpdateRequest, request: Request):
        ctx.check_auth(request)
        app.save_config(payload.updates)
        return {"ok": True}

    return router
