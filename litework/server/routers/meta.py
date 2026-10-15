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

# 低于该版本的前端按不兼容处理（连接前经 /api/health 或 /api/status 协商）
MIN_CLIENT_VERSION = "2.0.0"


class ConfigUpdateRequest(BaseModel):
    updates: Dict[str, Any]


class ProxyTestRequest(BaseModel):
    """代理连通性测试：前端输入框里的（已归一化）代理地址。"""
    url: str = ""


class SyncRequest(BaseModel):
    """手动同步的数据源：models_dev 或定价插件声明的官方源（deepseek/kimi…）。"""
    source: str = "models_dev"


def create_router(ctx: ServerContext) -> APIRouter:
    router = APIRouter()
    app, tasks, token = ctx.app, ctx.tasks, ctx.auth.token

    @router.get("/api/health")
    async def health():
        """未鉴权存活探针：Docker healthcheck 与客户端连接前的版本协商。

        刻意不过 check_auth——容器编排与健康检查带不了 Bearer 头；只暴露
        版本号，不含 workspace 等任何实例信息。
        """
        return {"status": "ok", "version": VERSION, "min_client": MIN_CLIENT_VERSION}

    @router.get("/api/pairing")
    async def pairing(request: Request):
        """配对信息（已鉴权）：本 Core 的连接地址 + 令牌，供前端生成二维码。

        手机扫码配对 = 打开 `{origin}/#litework-token={token}`，前端
        consumeTokenFromUrl 自动写 localStorage 并剥除 hash。桌面「手机」
        面板走 Electron IPC（get-lan-share），这里服务浏览器/PWA 形态
        （Linux Core 直连浏览器时也能展示二维码）。
        """
        ctx.check_auth(request)
        origin = str(request.base_url).rstrip("/")
        forwarded_host = request.headers.get("x-forwarded-host", "")
        forwarded_proto = request.headers.get("x-forwarded-proto", "")
        if forwarded_host:
            origin = f"{forwarded_proto or 'http'}://{forwarded_host}"
        return {"origin": origin, "token": token or ""}

    @router.get("/api/status")
    async def status(request: Request):
        ctx.check_auth(request)
        llm_active = app.llm_registry.active
        settings = app.llm_registry.get_active_provider_settings()
        return {
            "version": VERSION,
            "min_client": MIN_CLIENT_VERSION,
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
                # 出网代理（分流）：设置页「网络与代理」回显
                "proxy",
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
        # 出网代理（分流：llm/webfetch/meta/market/mcp/shell）
        "proxy",
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

    @router.get("/api/proxy/detect")
    async def detect_proxy(request: Request):
        """检测系统级代理（macOS scutil / Windows 注册表 / 环境变量）。

        设置页打开时调用：检测到系统代理 → 地址栏预填显示，用户只需
        勾选哪些流量走它（「让已有代理直接显示，控制范围即可」）。
        """
        ctx.check_auth(request)
        from ...core.proxy import detect_system_proxy
        return detect_system_proxy()

    @router.post("/api/proxy/test")
    async def test_proxy(payload: ProxyTestRequest, request: Request):
        """代理连通性测试：经指定代理 GET api.github.com，回显延迟/失败原因。

        不写配置——测试的是「用户输入框里的地址」（前端归一化后传入），
        与是否已保存无关。
        """
        ctx.check_auth(request)
        import time as _time
        import httpx as _httpx
        from ...core.proxy import ensure_system_ca, normalize_proxy_url, ssl_verify_for

        url = normalize_proxy_url(payload.url or "")
        if not url:
            return {"ok": False, "text": "代理地址格式无法识别"}
        # 测试环境必须与真实流量一致，否则测试结果误导真实可用性：
        # ① 系统证书注入（truststore：MITM 代理根证书在系统钥匙串里——
        #    业务适配器已注入，测试不注入会「测试失败但实际能连」）；
        # ② ssl_verify 跟随用户配置（关了校验的测试也不校验）
        ensure_system_ca()
        verify = ssl_verify_for(app.config)
        if not verify:
            import logging
            logging.getLogger("litework.proxy").warning(
                "[Proxy] 测试连接在 ssl_verify=false 下进行（证书未校验）")
        start = _time.monotonic()
        try:
            # trust_env=False：只测用户填的代理，不受 shell 环境变量干扰
            resp = await _httpx.AsyncClient(proxy=url, timeout=8.0, trust_env=False,
                                            verify=verify).get(
                "https://api.github.com", headers={"User-Agent": "lite-work-proxy-test"})
            ms = int((_time.monotonic() - start) * 1000)
            if resp.status_code < 400:
                return {"ok": True, "text": f"连通（HTTP {resp.status_code} · {ms}ms）"}
            return {"ok": False, "text": f"代理可达但目标返回 HTTP {resp.status_code}（{ms}ms）"}
        except Exception as exc:  # noqa: BLE001 - 测试端点：任何失败都要回显人话
            # 按异常类型给人话提示：用户可自行判断是协议填错还是地址不通
            name = type(exc).__name__
            hints = {
                "ConnectTimeout": "连接超时（地址/端口不通，或防火墙拦截）",
                "ConnectError": "无法建立连接（检查地址与端口）",
                "TLSSError": "TLS 握手失败（https 代理协议不匹配？试着把 https:// 换成 http://）",
                "ProxyError": "代理拒绝连接（认证失败——检查用户名密码；或代理不接受此协议）",
                "ReadTimeout": "代理响应超时（能连上但很慢）",
            }
            hint = hints.get(name) or ("TLS 握手失败（https 代理协议不匹配？试着把 https:// 换成 http://）"
                                       if "TLS" in name or "tls" in str(exc).lower()
                                       else f"{name}")
            return {"ok": False, "text": f"连接失败：{hint}"}

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
