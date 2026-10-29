# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""FastAPI 服务：REST API + SSE 事件流 + 静态前端托管 + 可选 Bearer 鉴权。

路由拆分至 routers/ 包，本模块只保留应用工厂、CORS、lifespan 与静态托管。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Dict, Optional

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
    create_ppt_router,
    create_push_router,
    create_schedules_router,
    create_security_router,
    create_sessions_router,
    create_skills_router,
    create_workspace_router,
)
from .routers.context import ServerContext, TokenAuth
from .install_jobs import InstallJobRegistry
from .push import PushManager
from .tasks import TaskManager, WorkspaceBusyError

logger = logging.getLogger("litework.server")

VERSION = __version__

# 已完成任务的重对象回收间隔（秒）：任务结束后的 kernel/registry/loop 会占到
# 几十 MB，没有活跃 SSE 订阅者时靠这个循环放掉（见 TaskManager.reap）
_TASK_REAP_INTERVAL_SECONDS = 30.0

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
        # 模型元数据与官方定价：**启动不联网**。查询侧只读本地缓存（models.dev
        # / 官方定价 / 内置快照），任何时刻都不阻塞启动；缓存过期/缺失由前端
        # 启动横幅提示「立即同步」，用户确认后才联网（见 meta 路由）。
        # 这里只记一条日志快照——「窗口掉回 128K」类报障可直接从日志定位。
        md = app.model_meta_status()
        age = md.get("age_seconds")
        if not md.get("cached"):
            logger.warning(
                "[Startup] models.dev 元数据无缓存：上下文窗口将按供应商默认值"
                "（自定义中转通常 128K）估算，建议在设置页同步")
        elif age is not None and age > 7 * 86400:
            logger.warning(
                "[Startup] models.dev 缓存已过期 %.1f 天（仍作兜底使用），"
                "建议在设置页同步", age / 86400)
        await app.mcp_manager.start()
        # 盯梢 / 定时调度器：探针 → 指纹 → 按需唤 LLM。
        # 启动时按各计划的 catch_up 策略补跑错过的触发（默认只补一次）。
        async def _start_watch_task(sched, prompt):
            """把一条计划变成真正的一次 Agent 任务（调 TaskManager）。

            权限档钉在计划上（read / read+web）并**真正下传**给工具注册表：
            无人值守时没人点审批，写/执行类工具必须从工具面移除，否则 Agent
            会调用它、卡在审批门 600s、然后被自动拒绝。
            返回 task_id；失败返回 None（由调度器记为 error 并推送通知）。
            """
            try:
                handle = tasks.start(sched.session_id or "", prompt, agent_id=None,
                                     unattended_profile=sched.permissions or "read")
                return handle.task_id
            except WorkspaceBusyError as exc:
                # 同项目互斥（多端视图分离）：无人值守任务撞上在跑任务，
                # 明确记入计划历史，而不是笼统的「启动失败」
                logger.warning("[Watch] 计划任务被同项目互斥拒绝: %s", exc)
                return None
            except Exception:  # noqa: BLE001 - 启动失败记入计划历史即可
                logger.warning("[Watch] 启动计划任务失败", exc_info=True)
                return None

        try:
            await app.start_watch_scheduler(_start_watch_task)
        except Exception:  # noqa: BLE001 - 调度器起不来不该挡住整个服务
            logger.warning("[Startup] 盯梢调度器启动失败", exc_info=True)
        # 回收上一次运行遗留的后台命令（服务端 TTL 兜底：后端崩溃/断连时子进程
        # 会失去管理者，继续占用 CPU/端口/文件锁）。判定保守，只杀日志里记录过
        # 且"存活时间与记录吻合"的 pid（防 pid 复用误杀）；失败不影响启动。
        try:
            app.reclaim_orphan_tasks()
        except Exception:  # noqa: BLE001 - 启动路径绝不允许因此中断
            logger.warning("[Startup] 回收遗留后台命令失败", exc_info=True)
        # 已完成任务的定期回收：没有新任务启动时（界面关掉、断网、盯梢任务结束后
        # 长时间空闲）也要把已结束 handle 的重对象放掉，否则内存只增不减。
        # 与 TaskManager.start 里的顺手回收互补，异常不影响服务。
        async def _reap_finished_tasks() -> None:
            while True:
                await asyncio.sleep(_TASK_REAP_INTERVAL_SECONDS)
                try:
                    tasks.reap()
                except Exception:  # noqa: BLE001 - 回收失败不该杀掉循环
                    logger.warning("[Reap] 回收已完成任务失败", exc_info=True)
                # 会话级：子 Agent 的消息链是「关不掉的对话」里最大的一块。
                # 放在同一循环里做，是因为 trimmed 的判定依赖时间流逝（30 分钟），
                # 而 /api/status 不是轮询接口——不能指望有请求来触发。
                try:
                    app.trim_finished_agent_messages()
                except Exception:  # noqa: BLE001
                    logger.warning("[Reap] 裁剪子 Agent 消息链失败", exc_info=True)

        reap_task = asyncio.create_task(_reap_finished_tasks())
        try:
            yield
        finally:
            reap_task.cancel()
            try:
                await reap_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            # 停机必须显式回收调度循环：否则 asyncio 任务残留会让
            # 「服务已退出」与「任务仍在跑」并存（测试里表现为调度器未回收）。
            try:
                await app.stop_watch_scheduler()
            except Exception:  # noqa: BLE001 - 停机路径绝不抛
                logger.warning("[Shutdown] 停止盯梢调度器失败", exc_info=True)
            try:
                push.close()
            except Exception:  # noqa: BLE001
                logger.warning("[Shutdown] 关闭推送线程池失败", exc_info=True)

    fast_app = FastAPI(title="lite-work", version=VERSION, lifespan=_lifespan)
    auth = TokenAuth(token)
    tasks = TaskManager(app)
    push = PushManager(app.config_dir)
    ctx = ServerContext(app=app, tasks=tasks, auth=auth, jobs=InstallJobRegistry(),
                        push=push)
    # 供测试与内省取用（路由闭包持有同一实例）：验证「有任务在跑时不释放」
    # 这类跨层约定时，需要从外部访问 TaskManager
    fast_app.state.tasks = tasks
    fast_app.state.ctx = ctx
    # 盯梢通知的设备推送通道（PWA）：门槛判断在 PushManager.on_watch_note，
    # 回调异常由总线自吞，不影响调度器
    app.watch_notifications.on_push = push.on_watch_note

    # 审批门统一 resolve 回调：无论用户确认还是超时拒绝都广播 approval:resolved。
    # 此前超时路径只打日志，前端审批卡会永远挂着（「干一会就停止」的表象之一）。
    def _broadcast_gate_resolve(approval_id: str, info: Dict) -> None:
        for handle in list(tasks.tasks.values()):
            try:
                if handle.kernel is None or handle.done:
                    continue
                coro = handle.kernel.events.emit("approval:resolved", dict(info))
                try:
                    asyncio.get_running_loop().create_task(coro)
                except RuntimeError:
                    pass  # 无运行中事件循环（罕见）：跳过广播，不影响 resolve
            except Exception:
                continue

    app.approval_gate.on_resolve = _broadcast_gate_resolve

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
    fast_app.include_router(create_ppt_router(ctx))
    fast_app.include_router(create_schedules_router(ctx))
    fast_app.include_router(create_push_router(ctx))
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
    # Docker 等部署形态：镜像把 dist 打在源码树之外，经环境变量显式指定
    env_dist = os.environ.get("LITEWORK_WEB_DIST")
    if env_dist:
        candidates.append(env_dist)
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
