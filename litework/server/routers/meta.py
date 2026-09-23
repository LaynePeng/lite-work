# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""服务元信息与全局配置：/api/status、/api/config。"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ... import __version__
from ...core.agent_loop import pricing_payload
from .context import ServerContext

VERSION = __version__


class ConfigUpdateRequest(BaseModel):
    updates: Dict[str, Any]


class SyncRequest(BaseModel):
    """手动同步的数据源：models_dev 或定价插件声明的官方源（deepseek/kimi…）。"""
    source: str = "models_dev"


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
                "auto_approve", "pricing", "pricing_check_ttl_days",
                "context_full_turns", "llm_timeout",
                "llm_retries", "skill_permissions", "subagent_timeout",
                # 效率机制（v1.6.0）：观察打包 / 压缩经济学 / 证据收据小模型
                "observation_pack", "compaction_economics", "reducer_model", "reducer_provider",
                # 聊天区展示折叠阈值（轮数 / 消息数，任一超限即折叠）
                "chat_fold_turns", "chat_fold_messages",
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
        """定价数据源状态（设置页「模型元数据与定价」展示同步情况）。

        分两组：models_dev（上下文窗口 + 无官方源供应商的定价）与各官方定价源
        （deepseek / kimi）。每项含 cached / models / age_seconds / stale，
        过期时前端提示「建议同步」（不强制、不自动联网）。
        """
        ctx.check_auth(request)
        return app.pricing_status()

    @router.post("/api/model-meta/refresh")
    async def refresh_model_meta(request: Request,
                                 payload: Optional[SyncRequest] = None):
        """手动同步**单个**数据源（source: models_dev | deepseek | kimi）。

        前端逐源调用以呈现同步步骤（每步完成即可回显该源状态）。拉取是阻塞
        IO → 丢到线程执行避免卡住事件循环；返回同步结果与当前生效模型的计费
        单价，便于直接和账单对账。
        """
        ctx.check_auth(request)
        source = (payload.source if payload else "") or "models_dev"
        result = await asyncio.to_thread(app.sync_pricing, source)
        active = app.llm_registry.active
        model = app.llm_registry.get_active_provider_settings().get("model", "")
        return {
            **result,
            "pricing": pricing_payload(app.resolve_pricing(active, model)),
        }

    return router
