# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""路由共享上下文：AgentApp / TaskManager / 鉴权的传递与公共校验助手。

各 APIRouter 经 ServerContext 注入依赖，可独立实例化测试。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hmac
import os
from typing import Dict, Optional

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

    # 客户端视图注册表：client-id → 该端当前项目（单用户多端共享一个 Core，
    # 各端文件树/会话列表/工具列表按自己的项目解析；任务启动时钉会话的项目，
    # 全局 app.workspace 降级为 CLI --workspace 启动值 + 兜底默认）。
    client_views: Dict[str, str] = field(default_factory=dict)

    def check_auth(self, request: Request) -> None:
        denied = self.auth.check(request)
        if denied is not None:
            raise HTTPException(status_code=401, detail="Unauthorized")

    @staticmethod
    def client_id_of(request: Request) -> str:
        """请求所属客户端（X-Client-Id 头；缺省 = 匿名单端，兼容旧客户端）。"""
        return (request.headers.get("X-Client-Id") or "").strip()

    def resolve_workspace(self, request: Request) -> Optional[str]:
        """解析该请求应看到的工作区：客户端视图优先，全局兜底。"""
        cid = self.client_id_of(request)
        if cid and cid in self.client_views:
            return self.client_views[cid]
        return self.app.workspace

    def set_client_view(self, request: Request, workspace: str) -> None:
        """登记客户端的项目视图（POST /api/workspace）。无 client-id 的旧客户端
        退回全局语义（保持兼容）。"""
        cid = self.client_id_of(request)
        if cid:
            self.client_views[cid] = workspace

    def require_workspace(self, request: Optional[Request] = None) -> str:
        ws = self.resolve_workspace(request) if request is not None else self.app.workspace
        if not ws:
            raise HTTPException(status_code=409, detail="请先打开项目后再创建会话或执行任务")
        return ws


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
