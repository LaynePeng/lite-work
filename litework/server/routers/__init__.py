# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""APIRouter 拆分集合（P0-1）：server/app.py 只保留应用工厂与静态托管。

路由按领域分组，各自暴露 create_router(ctx) 工厂，经 ServerContext 注入
AgentApp / TaskManager / TokenAuth——路由处理器可独立实例化测试，
解决原先 50+ 路由闭包内联无法单测的问题。
"""
from .agents import create_router as create_agents_router
from .chat import create_router as create_chat_router
from .llm import create_router as create_llm_router
from .meta import create_router as create_meta_router
from .office import create_router as create_office_router
from .security import create_router as create_security_router
from .sessions import create_router as create_sessions_router
from .skills import create_router as create_skills_router
from .workspace import create_router as create_workspace_router

__all__ = [
    "create_agents_router",
    "create_chat_router",
    "create_llm_router",
    "create_meta_router",
    "create_office_router",
    "create_security_router",
    "create_sessions_router",
    "create_skills_router",
    "create_workspace_router",
]
