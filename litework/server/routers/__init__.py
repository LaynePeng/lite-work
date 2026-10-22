# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""APIRouter 集合：按领域分组，各自暴露 create_router(ctx) 工厂。"""
from .agents import create_router as create_agents_router
from .chat import create_router as create_chat_router
from .llm import create_router as create_llm_router
from .meta import create_router as create_meta_router
from .office import create_router as create_office_router
from .ppt import create_router as create_ppt_router
from .push import create_router as create_push_router
from .schedules import build_router as build_schedules_router
from .security import create_router as create_security_router
from .sessions import create_router as create_sessions_router
from .skills import create_router as create_skills_router
from .workspace import create_router as create_workspace_router


def create_schedules_router(ctx):
    """盯梢 / 定时计划路由（本模块的工厂名与其余路由保持一致）。

    委托到 `schedules.build_router`——该模块按需求用 `build_router` 命名，
    这里做一层薄适配，避免 rest 路由工厂命名出现两套约定。
    """
    return build_schedules_router(ctx)


__all__ = [
    "create_agents_router",
    "create_chat_router",
    "create_llm_router",
    "create_meta_router",
    "create_office_router",
    "create_ppt_router",
    "create_push_router",
    "create_schedules_router",
    "create_security_router",
    "create_sessions_router",
    "create_skills_router",
    "create_workspace_router",
]
