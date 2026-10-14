# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""路由共享上下文：AgentApp / TaskManager / 鉴权的传递与公共校验助手。

各 APIRouter 经 ServerContext 注入依赖，可独立实例化测试。
"""
from __future__ import annotations

from dataclasses import dataclass
import hmac
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from ...app import AgentApp
from ..install_jobs import InstallJobRegistry
from ..push import PushManager
from ..tasks import TaskManager


class TokenAuth:
    def __init__(self, token: Optional[str]) -> None:
        self.token = token

    def check(self, request: Request) -> Optional[JSONResponse]:
        if not self.token:
            return None
        auth = request.headers.get("Authorization", "")
        # 常数时间比较（防时序侧信道逐字节猜令牌）。compare_digest 的 str
        # 重载要求双方 ASCII，请求头可含任意字符，统一走 bytes 最稳。
        expected = f"Bearer {self.token}".encode("utf-8")
        if hmac.compare_digest(auth.encode("utf-8"), expected):
            return None
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)


@dataclass
class ServerContext:
    """路由层依赖集合（由 create_app 组装，传入各 router 工厂）。"""

    app: AgentApp
    tasks: TaskManager
    auth: TokenAuth
    jobs: InstallJobRegistry
    push: PushManager

    def check_auth(self, request: Request) -> None:
        denied = self.auth.check(request)
        if denied is not None:
            raise HTTPException(status_code=401, detail="Unauthorized")

    def require_workspace(self) -> str:
        if not self.app.workspace:
            raise HTTPException(status_code=409, detail="请先打开项目后再创建会话或执行任务")
        return self.app.workspace


def _session_title(snapshot: dict) -> str:
    """从会话消息推导标题：优先用户自定义 name，其次首条用户消息，空会话兜底。"""
    meta = snapshot.get("metadata") or {}
    if meta.get("name"):
        return meta["name"]
    for m in snapshot.get("messages", []):
        if m.get("role") == "user":
            text = (m.get("content") or "").strip().replace("\n", " ")
            return text[:40] or "未命名会话"
    return "新会话"
