# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""会话管理：/api/sessions*、/api/todos、/api/compact。"""
from __future__ import annotations

import os
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .context import ServerContext, _session_title


class SessionCreateRequest(BaseModel):
    name: Optional[str] = None
    workspace: Optional[str] = None


class CompactRequest(BaseModel):
    session_id: str
    focus: str = ""


class SessionModelRequest(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None


class SessionGoalRequest(BaseModel):
    """会话目标（/goal）：持久化到 metadata，注入每个任务的 system prompt。"""
    goal: Optional[str] = None


class SessionCollabRequest(BaseModel):
    """会话协作模式（对话框选择器）：持久化到 metadata，覆盖全局 collab_policy。"""
    mode: Optional[str] = None


async def _shutdown_session_agents(app, session_id: str) -> None:
    """停止该会话全部后台子 Agent（会话删除时防泄漏）。"""
    try:
        manager = app.agent_manager(session_id, create=False)
        if manager is not None:
            await manager.shutdown()
            app.agent_managers.pop(session_id, None)
    except Exception:
        pass


def create_router(ctx: ServerContext) -> APIRouter:
    router = APIRouter()
    app, tasks = ctx.app, ctx.tasks

    @router.get("/api/sessions")
    async def list_sessions(request: Request, workspace: str = ""):
        ctx.check_auth(request)
        snapshots = app.session_store.list()
        result = []
        for s in snapshots:
            # 严格按 workspace 过滤：会话列表只显示当前项目的会话。
            # 点击会话不会切换项目（工具/文件树仍停在当前工作区），
            # 因此无 workspace 绑定的旧会话不显示——显示语义错位的内容比隐藏更糟。
            s_ws = (s.get("metadata") or {}).get("workspace", "")
            if not s_ws:
                continue
            # 如果会话关联的项目目录已不存在，则不显示该会话
            if not os.path.isdir(s_ws):
                continue
            # Windows 文件系统大小写不敏感：normcase 归一后比较
            # （C:\Users\proj 与 c:\users\PROJ 是同一项目）
            if workspace and os.path.normcase(os.path.abspath(s_ws)) != os.path.normcase(os.path.abspath(workspace)):
                continue
            messages = s.get("messages", [])
            if not any(m.get("role") == "user" for m in messages):
                # 列表接口必须是纯读取；创建与首条消息之间存在短暂空窗。
                continue
            result.append({
                "session_id": s.get("session_id"),
                "created_at": s.get("created_at"),
                "updated_at": s.get("updated_at"),
                "message_count": len(messages),
                "title": _session_title(s),
                "metadata": s.get("metadata", {}),
            })
        return result

    @router.post("/api/sessions")
    async def create_session(payload: SessionCreateRequest, request: Request):
        ctx.check_auth(request)
        ctx.require_workspace()

        # 毫秒时间戳在快速连续创建会话时会碰撞，导致新会话覆盖旧会话。
        session_id = f"session_{uuid.uuid4().hex}"
        # metadata 记录 workspace，用于按项目过滤会话列表；name 仅在显式提供时保存
        metadata: Dict[str, Any] = {}
        if payload.name:
            metadata["name"] = payload.name
        workspace = payload.workspace or app.workspace
        if workspace:
            metadata["workspace"] = workspace
        app.session_store.save(session_id, [], metadata)
        return {"session_id": session_id}

    @router.get("/api/sessions/{session_id}")
    async def get_session(session_id: str, request: Request):
        ctx.check_auth(request)
        snap = app.session_store.load(session_id)
        if snap is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return snap.to_dict()

    @router.delete("/api/sessions/cleanup")
    async def cleanup_sessions(request: Request):
        """一键清理孤儿会话：删除所有关联项目目录已不存在的会话。"""
        ctx.check_auth(request)
        snapshots = app.session_store.list()
        deleted = 0
        for s in snapshots:
            s_ws = (s.get("metadata") or {}).get("workspace", "")
            if not s_ws:
                continue
            if not os.path.isdir(s_ws):
                sid = s.get("session_id", "")
                if not sid:
                    continue
                await _shutdown_session_agents(app, sid)
                app.session_store.delete(sid)
                deleted += 1
                # 附带清理该会话的 TODO 看板（内存 + 磁盘）
                try:
                    app.todo_plugin.delete_board(sid)
                except Exception:
                    pass
        return {"ok": True, "deleted": deleted}

    @router.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str, request: Request):
        ctx.check_auth(request)
        ok = app.session_store.delete(session_id)
        if not ok:
            raise HTTPException(status_code=404, detail="会话不存在")
        await _shutdown_session_agents(app, session_id)
        # 附带清理该会话的 TODO 看板（内存 + 磁盘）
        try:
            app.todo_plugin.delete_board(session_id)
        except Exception:
            pass
        return {"ok": True}

    @router.get("/api/todos")
    async def get_todos(session_id: str = "", request: Request = None):
        """读取会话 TODO 看板（内存命中优先，未命中从磁盘恢复；刷新/重启后可还原）。"""
        if request:
            ctx.check_auth(request)
        if not session_id:
            return {"todos": []}
        return {"todos": app.todo_plugin.get(session_id.strip())}

    @router.get("/api/sessions/{session_id}/model")
    async def get_session_model(session_id: str, request: Request):
        ctx.check_auth(request)
        snapshot = app.session_store.load(session_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        override = snapshot.metadata.get("model")
        default = app.llm_registry.get_active_provider_settings()
        if not isinstance(override, dict):
            override = None
        return {
            "override": override,
            "effective": {
                "provider": override.get("provider") if override else app.llm_registry.active,
                "model": override.get("model") if override else default.get("model", ""),
            },
        }

    @router.post("/api/sessions/{session_id}/model")
    async def set_session_model(session_id: str, payload: SessionModelRequest, request: Request):
        ctx.check_auth(request)
        snapshot = app.session_store.load(session_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        metadata = dict(snapshot.metadata)
        if not payload.provider and not payload.model:
            metadata.pop("model", None)
            override = None
        else:
            provider = (payload.provider or "").strip()
            model = (payload.model or "").strip()
            settings = app.llm_registry.providers.get(provider)
            configured_models = (settings or {}).get("models") or []
            if not provider or not model or not settings or not settings.get("api_key"):
                raise HTTPException(status_code=400, detail="只能选择已配置 API Key 的供应商和模型")
            if model not in configured_models:
                raise HTTPException(status_code=400, detail="只能选择该供应商已配置的模型")
            override = {"provider": provider, "model": model}
            metadata["model"] = override
        app.session_store.save(session_id, snapshot.messages, metadata)
        default = app.llm_registry.get_active_provider_settings()
        return {"override": override, "effective": {
            "provider": override["provider"] if override else app.llm_registry.active,
            "model": override["model"] if override else default.get("model", ""),
        }}

    @router.post("/api/sessions/{session_id}/goal")
    async def set_session_goal(session_id: str, payload: SessionGoalRequest, request: Request):
        """设置/清除会话目标（/goal）：存入 metadata，任务启动时注入 system prompt。"""
        ctx.check_auth(request)
        snapshot = app.session_store.load(session_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        metadata = dict(snapshot.metadata)
        goal = (payload.goal or "").strip()
        if goal:
            metadata["goal"] = goal[:2000]
        else:
            metadata.pop("goal", None)
        app.session_store.save(session_id, snapshot.messages, metadata)
        return {"ok": True, "goal": goal or None}

    @router.post("/api/sessions/{session_id}/collab")
    async def set_session_collab(session_id: str, payload: SessionCollabRequest, request: Request):
        """设置/清除会话协作模式（对话框选择器写入；覆盖全局 collab_policy）。

        mode 为已安装模式的 mode_name（或 default/review）；null/空清除覆盖。
        """
        ctx.check_auth(request)
        snapshot = app.session_store.load(session_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        mode = (payload.mode or "").strip()
        if mode:
            # 校验：只接受已知模式（内置策略或已安装模式插件）
            from ...orchestration.collab_policy import list_collab_modes

            known = {m["name"] for m in list_collab_modes(app)}
            if mode not in known:
                raise HTTPException(status_code=400, detail=f"未知的协作模式: {mode}")
        metadata = dict(snapshot.metadata)
        if mode:
            metadata["collab_mode"] = mode
        else:
            metadata.pop("collab_mode", None)
        app.session_store.save(session_id, snapshot.messages, metadata)
        return {"ok": True, "mode": mode or None}

    @router.get("/api/sessions/{session_id}/agents")
    async def list_session_agents(session_id: str, request: Request):
        """会话级子 Agent 状态查询（主任务结束后前端同步 Agents 看板用）。

        背景子 Agent 的生命周期超出主任务 SSE 连接：主任务结束后 EventSource
        关闭，subagent:completed 事件无法再到达前端，看板卡片会永久卡在
        "running"。前端在收到 [DONE] 后调用此端点同步最终状态。
        """
        ctx.check_auth(request)
        manager = app.agent_manager(session_id, create=False)
        if manager is None:
            return {"agents": []}
        return {"agents": manager.list_agents(include_closed=True)}

    @router.post("/api/compact")
    async def compact_session(payload: CompactRequest, request: Request):
        """手动压缩会话上下文：旧轮次摘要化，最近 N 轮原样保留，立即落盘。"""
        ctx.check_auth(request)
        ctx.require_workspace()
        session_id = payload.session_id.strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id 不能为空")
        # 运行中任务的内存历史会在下轮落盘时覆盖压缩结果，必须拒绝
        if tasks.active_for_session(session_id) is not None:
            raise HTTPException(status_code=409, detail="任务运行中无法压缩，请先停止任务")
        result = await app.compact_session(session_id, focus=(payload.focus or "").strip())
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("reason", "压缩失败"))
        return result

    return router
