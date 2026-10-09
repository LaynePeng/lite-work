# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""服务元信息与全局配置：/api/status、/api/config。"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
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
            # 仍在运行的后台命令数（execute_command background=true）：
            # 切换项目/重启后据此发现上次遗留的孤儿任务
            "running_background": app.running_background_count(),
            # 后台 Agent 概况（Agents 看板 / GC 监控用）
            "background_agents": app.background_agent_count(),
            "background_agents_stale": sum(
                1
                for manager in app.agent_managers.values()
                for r in manager.agents.values()
                if getattr(r, "status", "") in ("closed", "completed", "errored")
            ),
            "token_auth": bool(token),
        }

    @router.get("/api/metrics/tools")
    async def tool_metrics(request: Request):
        """工具/技能指标聚合：命中与成功率视角，用于改进工具描述与技能边界。

        读的是 `core/metrics.py` 落盘的 JSONL（只含结构化计数，无参数与输出正文）。
        数据缺失（未运行过任务 / 指标被关）时返回空表，不报错。
        """
        ctx.check_auth(request)
        from ...core.metrics import read_events, summarize

        path = app.tool_metrics_path()
        events = read_events(path)
        data = summarize(events)
        data["path"] = path
        data["enabled"] = bool(app.config.get("tool_metrics", True))
        return data

    @router.get("/api/config")
    async def get_config(request: Request):
        ctx.check_auth(request)
        return {
            k: app.config.get(k) for k in (
                "max_steps", "token_budget", "tool_timeout",
                "auto_approve", "approval_timeout", "pricing", "pricing_check_ttl_days",
                "context_full_turns", "llm_timeout", "llm_idle_timeout",
                "llm_retries", "skill_permissions", "subagent_timeout",
                # 效率机制（v1.6.0）：观察打包 / 压缩经济学 / 证据收据小模型
                "observation_pack", "compaction_economics", "reducer_model", "reducer_provider",
                # 完成证据门禁：off / advisory / enforced + 补证据轮数上限
                "completion_gate", "completion_gate_retries",
                # 工具/技能指标：只记结构化计数
                "tool_metrics",
                # Agent 轨迹：默认关闭，设置页开启
                "trajectory_enabled",
                # 聊天区展示折叠阈值（轮数 / 消息数，任一超限即折叠）
                "chat_fold_turns", "chat_fold_messages",
                # 多智能体（docs/multi-agent-design.md §3 配置面）
                "max_parallel_agents", "agent_total_limit", "agent_max_steps",
                "agent_max_steps_cap", "agent_spawn_depth", "agent_message_max_chars",
                "agent_meeting_rounds", "agent_ledger_interval", "agent_persist_max",
                "agent_collab_mode",
                # 协作模式（配方文本 / 模式名）
                "collab_policy", "collab_recipe",
                # 界面偏好（侧栏 / 右栏宽度 px、侧栏页签）：前端读回后应用；跨 origin 稳定
                # （桌面端 Core 端口随机，localStorage 按 origin 隔离会读不回）
                "ui_prefs",
            )
        }

    # POST /api/config 的可写键白名单：与 GET 的展示白名单对齐，加上插件配置
    # （任意键）——插件配置是合法的全量写入场景（savePluginConfig 走这里）
    _WRITABLE_KEYS = frozenset({
        "max_steps", "token_budget", "tool_timeout", "auto_approve",
        "context_full_turns", "llm_timeout", "llm_idle_timeout", "llm_retries", "llm",
        "skill_permissions", "subagent_timeout", "observation_pack",
        "compaction_economics", "reducer_model", "reducer_provider",
        "completion_gate", "completion_gate_retries", "tool_metrics",
        "trajectory_enabled", "chat_fold_turns", "chat_fold_messages",
        "max_parallel_agents", "agent_total_limit", "agent_max_steps",
        "agent_max_steps_cap", "agent_spawn_depth", "agent_message_max_chars",
        "agent_meeting_rounds", "agent_ledger_interval", "agent_persist_max",
        "agent_collab_mode", "collab_policy", "collab_recipe",
        "ui_prefs", "mcp_servers", "skill_trigger_mode", "parallel_tool_calls",
        "pricing", "pricing_overrides", "pricing_check_ttl_days",
        "max_zip_size_mb", "auto_approve", "approval_timeout",
        # 插件配置（任意插件名的顶层键——savePluginConfig 的合法通道）
        # 由 app.save_config 内部处理，此处不逐一列举
    })

    @router.post("/api/config")
    async def update_config(payload: ConfigUpdateRequest, request: Request):
        """更新配置（POST）：可写键白名单校验——防止通过 API 塞任意键。

        白名单外的键（尤其内部状态键）拒绝写入。插件配置走「插件名」作为
        顶层键，属于合法通道（savePluginConfig），不在此拦截。
        """
        ctx.check_auth(request)
        # 插件配置：顶层键在已安装插件名列表里 → 放行
        from ...tools.plugin_loader import read_installed
        _plugin_names = set(read_installed(app.config_dir).keys())
        _plugin_names.update({"builtin", "local"})  # 内置/本地前缀

        rejected = [k for k in payload.updates
                    if k not in _WRITABLE_KEYS and k not in _plugin_names]
        if rejected:
            raise HTTPException(
                status_code=400,
                detail=f"不允许写入的配置键: {', '.join(rejected[:5])}"
                       f"{'…' if len(rejected) > 5 else ''}（可写键见 GET /api/config）"
            )
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
