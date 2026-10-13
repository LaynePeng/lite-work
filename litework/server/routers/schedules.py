# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""盯梢 / 定时计划的 REST 接口。

端点：

- `GET    /api/schedules`           列出计划（含运行历史与下次到期）
- `POST   /api/schedules`           新建（缺字段用 Schedule 默认值）
- `GET    /api/schedules/{id}`      单条
- `PATCH  /api/schedules/{id}`      局部更新（改完自动重算到期）
- `DELETE /api/schedules/{id}`      删除
- `POST   /api/schedules/{id}/run`  立即跑一次（无视到期与 enabled）
- `GET    /api/watch/sources`       可用数据源（内置 + 插件），供计划编辑器下拉

「立即跑一次」同样走触发租约：多窗口下点两次不会跑两遍。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .context import ServerContext
from ...watch.schedule_store import RunRecord, Schedule


class ScheduleCreate(BaseModel):
    name: str = ""
    enabled: bool = True
    schedule_mode: str = "interval"
    interval_seconds: int = 3600
    time_of_day: str = "09:00"
    cron: str = ""
    session_id: Optional[str] = None
    prompt: str = ""
    source: str = ""
    probe_config: Dict[str, Any] = Field(default_factory=dict)
    permissions: str = "read"
    notify: List[str] = Field(default_factory=lambda: ["in_app"])
    catch_up: str = "once"


class SchedulePatch(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    schedule_mode: Optional[str] = None
    interval_seconds: Optional[int] = None
    time_of_day: Optional[str] = None
    cron: Optional[str] = None
    session_id: Optional[str] = None
    prompt: Optional[str] = None
    source: Optional[str] = None
    probe_config: Optional[Dict[str, Any]] = None
    permissions: Optional[str] = None
    notify: Optional[List[str]] = None
    catch_up: Optional[str] = None


#: 无人值守允许的权限档。刻意收窄：没人点审批，写/执行类权限不能给。
ALLOWED_PERMISSIONS = {"read", "read+web"}

#: 支持的调度模式（与 scheduler.compute_next_run 对应）
ALLOWED_MODES = {"interval", "daily", "cron"}


def _validate(mode: str, permissions: str, catch_up: str) -> None:
    if mode not in ALLOWED_MODES:
        raise HTTPException(status_code=400, detail=f"schedule_mode 仅支持 {sorted(ALLOWED_MODES)}")
    if permissions not in ALLOWED_PERMISSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"无人值守权限只允许 {sorted(ALLOWED_PERMISSIONS)}（没人点审批，写/执行不给）",
        )
    if catch_up not in {"once", "all", "skip"}:
        raise HTTPException(status_code=400, detail="catch_up 仅支持 once / all / skip")


def build_router(ctx: ServerContext) -> APIRouter:
    router = APIRouter()
    app = ctx.app

    def _view(sched: Schedule) -> Dict[str, Any]:
        """给前端的视图：计划字段 + 下次到期时间（调度器运行时才有）。"""
        d = sched.to_dict()
        schedr = app.watch_scheduler
        if schedr is not None:
            d["next_run"] = schedr._next.get(sched.id)  # noqa: SLF001 - 同包内读取调度状态
        else:
            d["next_run"] = None
        return d

    @router.get("/api/schedules")
    async def list_schedules(request: Request):
        ctx.check_auth(request)
        ctx.require_workspace()
        items = [_view(s) for s in app.schedule_store.all()]
        now = time.time()
        items.sort(key=lambda d: (not d.get("enabled"), d.get("next_run") or (now + 1e12)))
        return {"schedules": items}

    @router.get("/api/watch/sources")
    async def list_sources(request: Request):
        ctx.check_auth(request)
        ctx.require_workspace()
        return {"sources": app.watch_registry.describe_all()}

    @router.get("/api/watch/notifications")
    async def list_notifications(request: Request, after: int = 0, limit: int = 50):
        """历史通知（断线重连后补齐缺口用）。`after` = 已收到的最大通知 id。"""
        ctx.check_auth(request)
        ctx.require_workspace()
        items = app.watch_notifications.recent(after=after or None, limit=max(1, min(limit, 200)))
        return {"notifications": items}

    @router.post("/api/watch/notifications/clear")
    async def clear_notifications(request: Request):
        """清空通知历史（不影响已订阅的 SSE 连接）。"""
        ctx.check_auth(request)
        ctx.require_workspace()
        app.watch_notifications.clear()
        return {"ok": True}

    @router.get("/api/watch/events")
    async def watch_events(request: Request, after: int = 0):
        """应用级通知 SSE。

        与 `/api/tasks/{id}/events` 的区别：**不绑定任务**。盯梢通知可能在任何
        时刻产生（纯定时、指纹变化、探针失败），此时可能根本没有任务在跑。

        断线重连：浏览器 EventSource 会带**原始 URL** 重连（不重放我们加的
        query），所以游标由前端在 `onerror` 里自行带上 `after` 重订阅；
        这里只要支持 `after` 参数即能补齐缺口，不重复推送。
        """
        ctx.check_auth(request)
        ctx.require_workspace()
        bus = app.watch_notifications
        queue = bus.subscribe()
        # 订阅瞬间先回放缺口：订阅动作与历史快照之间可能已经有新通知，
        # 先取快照再订阅会把这段丢掉；故**先订阅**（已在上行）再读历史，
        # 两边都有的话前端按 id 去重即可（重复推送比丢通知可接受）。
        backlog = bus.recent(after=after or None, limit=50)

        async def _stream():
            try:
                for item in backlog:
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            finally:
                bus.unsubscribe(queue)

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @router.post("/api/schedules")
    async def create_schedule(payload: ScheduleCreate, request: Request):
        ctx.check_auth(request)
        workspace = ctx.require_workspace()
        _validate(payload.schedule_mode, payload.permissions, payload.catch_up)
        sched = Schedule(
            name=payload.name,
            enabled=payload.enabled,
            schedule_mode=payload.schedule_mode,
            interval_seconds=max(60, int(payload.interval_seconds)),
            time_of_day=payload.time_of_day,
            cron=payload.cron,
            session_id=payload.session_id,
            workspace=workspace,
            prompt=payload.prompt,
            source=payload.source,
            probe_config=dict(payload.probe_config),
            permissions=payload.permissions,
            notify=list(payload.notify) or ["in_app"],
            catch_up=payload.catch_up,
        )
        if sched.source and app.watch_registry.get(sched.source) is None:
            raise HTTPException(status_code=400, detail=f"未知的数据源: {sched.source}")
        app.schedule_store.upsert(sched)
        scheduler = app.watch_scheduler
        if scheduler is not None:
            scheduler.reschedule(sched.id)
        return {"schedule": _view(sched)}

    @router.get("/api/schedules/{schedule_id}")
    async def get_schedule(schedule_id: str, request: Request):
        ctx.check_auth(request)
        ctx.require_workspace()
        sched = app.schedule_store.get(schedule_id)
        if sched is None:
            raise HTTPException(status_code=404, detail="计划不存在")
        return {"schedule": _view(sched)}

    @router.patch("/api/schedules/{schedule_id}")
    async def patch_schedule(schedule_id: str, payload: SchedulePatch, request: Request):
        ctx.check_auth(request)
        ctx.require_workspace()
        sched = app.schedule_store.get(schedule_id)
        if sched is None:
            raise HTTPException(status_code=404, detail="计划不存在")

        data = payload.model_dump(exclude_unset=True)
        mode = str(data.get("schedule_mode", sched.schedule_mode))
        perms = str(data.get("permissions", sched.permissions))
        catch = str(data.get("catch_up", sched.catch_up))
        _validate(mode, perms, catch)
        if "source" in data and data["source"] and app.watch_registry.get(str(data["source"])) is None:
            raise HTTPException(status_code=400, detail=f"未知的数据源: {data['source']}")
        if "interval_seconds" in data and data["interval_seconds"] is not None:
            data["interval_seconds"] = max(60, int(data["interval_seconds"]))

        for key, value in data.items():
            if value is not None:
                setattr(sched, key, value)
        app.schedule_store.upsert(sched)
        scheduler = app.watch_scheduler
        if scheduler is not None:
            scheduler.reschedule(sched.id)
        return {"schedule": _view(sched)}

    @router.delete("/api/schedules/{schedule_id}")
    async def delete_schedule(schedule_id: str, request: Request):
        ctx.check_auth(request)
        ctx.require_workspace()
        if not app.schedule_store.delete(schedule_id):
            raise HTTPException(status_code=404, detail="计划不存在")
        scheduler = app.watch_scheduler
        if scheduler is not None:
            scheduler.reschedule(schedule_id)  # 已删 → 内部会清掉到期项
        return {"ok": True}

    @router.post("/api/schedules/{schedule_id}/run")
    async def run_now(schedule_id: str, request: Request):
        """立即触发一次（无论是否到期）。同样受触发租约保护，防多窗口重复跑。"""
        ctx.check_auth(request)
        ctx.require_workspace()
        sched = app.schedule_store.get(schedule_id)
        if sched is None:
            raise HTTPException(status_code=404, detail="计划不存在")
        scheduler = app.watch_scheduler
        if scheduler is None:
            raise HTTPException(status_code=503, detail="调度器未启动")
        slot = f"manual{int(time.time())}"
        if not scheduler.lease.acquire(schedule_id, slot):
            raise HTTPException(status_code=409, detail="该计划正在被另一个实例触发")
        rec = await scheduler.fire(sched, catch_up=False, slot=slot)
        return {"ok": True, "run": rec.to_dict()}

    @router.post("/api/schedules/{schedule_id}/reset-fingerprint")
    async def reset_fingerprint(schedule_id: str, request: Request):
        """清空指纹基线：下一次探测无论是否「有变化」都会触发一次（用于首次建基线）。"""
        ctx.check_auth(request)
        ctx.require_workspace()
        sched = app.schedule_store.get(schedule_id)
        if sched is None:
            raise HTTPException(status_code=404, detail="计划不存在")
        sched.last_hash = ""
        app.schedule_store.save()
        app.schedule_store.record_run(schedule_id, RunRecord(status="ok", detail="已重置指纹基线"))
        return {"ok": True}

    return router
