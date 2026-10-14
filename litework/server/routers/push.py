# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""PWA 推送订阅：/api/push/*（VAPID 公钥下发、订阅登记与注销）。"""
from __future__ import annotations

from typing import Dict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .context import ServerContext


class SubscribeRequest(BaseModel):
    """浏览器 PushSubscription 的精简形态（Service Worker 里 toJSON 的子集）。"""
    endpoint: str
    keys: Dict[str, str]
    # 设备备注名（设置页展示用，纯装饰）
    device: str = ""


class UnsubscribeRequest(BaseModel):
    endpoint: str


def create_router(ctx: ServerContext) -> APIRouter:
    router = APIRouter()

    @router.get("/api/push/vapid-key")
    async def vapid_key(request: Request):
        ctx.check_auth(request)
        return {"public_key": ctx.push.public_key()}

    @router.post("/api/push/subscribe")
    async def subscribe(payload: SubscribeRequest, request: Request):
        ctx.check_auth(request)
        try:
            devices = ctx.push.subscribe(payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "devices": devices}

    @router.post("/api/push/unsubscribe")
    async def unsubscribe(payload: UnsubscribeRequest, request: Request):
        ctx.check_auth(request)
        return {"ok": ctx.push.unsubscribe(payload.endpoint)}

    return router
