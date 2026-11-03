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
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

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
        def _resolve_watch_session(sched) -> str:
            """计划要写的会话 id：**必须是真实存在的会话文件**。

            历史缺陷（用户报「盯梢触发也显示有消息，但没有消息发出来」）：
            走 `sched.session_id or ""`，计划没绑会话时传空串进 TaskManager →
            任务在一个 session_id 为空的「隐形会话」里跑完，结果落进
            `sessions/.json`。而会话列表要求 metadata.workspace 非空且
            session_id 非空，这个文件**永远不会出现在界面上**，用户只能看到
            通知条说「已触发」，点开却什么也没有。

            这里的修法是：没绑会话时**惰性建一个真实会话**并回写计划，让
            结果有归属、能在「会话」里看到（前端通知的「打开会话」按钮也
            才拿得到 session_id）。
            """
            sid = (sched.session_id or "").strip()
            if sid and app.session_store.load(sid) is not None:
                return sid
            # 会话自唤醒（schedule_wakeup）：**绝不另建会话**。
            # 它的全部意义就是「回到原来那个对话接着干」；原会话被删了，
            # 新建一个空会话去跑这段 prompt 等于把上下文全丢掉——宁可失败，
            # 也不要产出一次「看起来跑了、其实什么都接不上」的唤醒。
            if (getattr(sched, "origin", "") or "") == "session_wakeup":
                raise RuntimeError(
                    f"唤醒目标会话 {sid or '(空)'} 不存在（可能已被删除），已跳过本次唤醒")
            new_id = f"session_{uuid.uuid4().hex}"
            metadata: Dict[str, Any] = {
                # 名字进 metadata.name → 会话标题直接可读，不必点进去才知道
                # 是哪个计划跑出来的
                "name": f"盯梢：{sched.name or sched.id}",
                "workspace": sched.workspace or app.workspace or "",
                # 标记来源：会话列表/详情可据此区分「人开的会话」与「计划跑出来的」
                "watch_schedule_id": sched.id,
            }
            app.session_store.save(new_id, [], metadata)
            sched.session_id = new_id
            app.schedule_store.upsert(sched)
            logger.info("[Watch] 为计划 %s 建立会话 %s（原会话不可用）", sched.id, new_id)
            return new_id

        async def _start_watch_task(sched, prompt):
            """把一条计划变成真正的一次 Agent 任务（调 TaskManager）。

            权限档钉在计划上（read / read+web）并**真正下传**给工具注册表：
            无人值守时没人点审批，写/执行类工具必须从工具面移除，否则 Agent
            会调用它、卡在审批门 600s、然后被自动拒绝。
            返回 task_id；失败返回 None（由调度器记为 error 并推送通知）。
            """
            def _note(sched_id: str, kind: str, detail: str) -> None:
                """把失败/跳过原因回传给调度器（它会写进该计划的运行记录）。

                不这样做的话，面板上只有笼统的「任务启动失败（会话或工作区
                不可用／模型未配置）」——真实原因被吞掉，用户只能猜。
                """
                scheduler = app.watch_scheduler
                if scheduler is not None:
                    try:
                        scheduler.note_start_failure(sched_id, kind=kind, detail=detail)
                    except Exception:  # noqa: BLE001 - 记原因失败不影响主流程
                        logger.debug("[Watch] 回传启动失败原因失败", exc_info=True)
            try:
                session_id = _resolve_watch_session(sched)
            except Exception as exc:  # noqa: BLE001 - 建会话失败按启动失败处理
                # 把原因带回调度器：此前只记 None，面板上永远显示笼统的
                # 「任务启动失败（会话或工作区不可用／模型未配置）」——
                # 真实原因（如「唤醒目标会话已被删除」）被吞掉了。
                logger.warning("[Watch] 为计划 %s 准备会话失败: %s", sched.id, exc)
                _note(sched.id, "error", f"准备会话失败：{exc}")
                return None
            try:
                handle = tasks.start(session_id, prompt, agent_id=None,
                                     unattended_profile=sched.permissions or "read",
                                     # 结论转投的主会话（空 = 任务层回退到 personal 个人区）
                                     watch_main_session_id=sched.main_session_id or "",
                                     watch_plan_name=sched.name or sched.id,
                                     # 无人值守不排队：撞车就记一次跳过，等下一个周期
                                     # 再来。排队会让一条本该错过就错过的计划在用户
                                     # 早已离开时突然开跑。
                                     queue_if_busy=False)
                return handle.task_id
            except WorkspaceBusyError as exc:
                # 同项目互斥（多端视图分离）：无人值守任务撞上在跑任务。
                # 这不是故障，是**正常的并发拒绝**——用户正在该项目里干活，
                # 计划的节奏本就该让位。此前它和真正的启动失败共用同一条
                # error 记录，面板上显示成「任务启动失败」，用户以为盯梢坏了。
                logger.warning("[Watch] 计划任务被同项目互斥拒绝（本轮跳过）: %s", exc)
                _note(sched.id, "skip", f"跳过本轮：该项目已有任务在跑")
                return None
            except Exception as exc:  # noqa: BLE001 - 启动失败记入计划历史即可
                logger.warning("[Watch] 启动计划任务失败", exc_info=True)
                _note(sched.id, "error", f"启动失败：{type(exc).__name__}: {exc}")
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
    # TaskHandle.run 结束时需要触发「同项目排队」的排空（C 项）：
    # 那里必须等 running=False 之后才能放行下一个，所以不能从 start 内部递归。
    app._task_manager = tasks  # type: ignore[attr-defined]
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
