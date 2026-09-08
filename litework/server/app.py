"""FastAPI 服务：REST API + SSE 事件流 + 静态前端托管 + 可选 Bearer 鉴权。"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .. import __version__
from ..app import AgentApp
from .tasks import TaskManager

logger = logging.getLogger("litework.server")

VERSION = __version__


# ---------------------------------------------------------------- 请求模型


class ChatRequest(BaseModel):
    session_id: str
    prompt: str
    task_id: Optional[str] = None
    agent_id: Optional[str] = None
    reasoning_effort: Optional[str] = None


class CompactRequest(BaseModel):
    session_id: str
    focus: str = ""


class StopRequest(BaseModel):
    task_id: str


class ApproveRequest(BaseModel):
    approval_id: str
    approved: bool


class SessionCreateRequest(BaseModel):
    name: Optional[str] = None
    workspace: Optional[str] = None


class SecurityUpdateRequest(BaseModel):
    rules: Dict[str, Any]


class MCPServersUpdateRequest(BaseModel):
    servers: Dict[str, Any]


class ConfigUpdateRequest(BaseModel):
    updates: Dict[str, Any]


class WorkspaceUpdateRequest(BaseModel):
    path: str


class ProjectCreateRequest(BaseModel):
    """新建项目：parent 下创建 name 目录，git=True 时初始化 git 仓库。"""
    parent: str
    name: str
    git: bool = True


class SessionModelRequest(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None


class QuestionAnswerRequest(BaseModel):
    question_id: str
    answer: str


class LLMConfigRequest(BaseModel):
    active: Optional[str] = None
    providers: Optional[Dict[str, Dict[str, Any]]] = None


class LLMTestRequest(BaseModel):
    provider_id: Optional[str] = None
    overrides: Optional[Dict[str, Any]] = None


class SkillCreateRequest(BaseModel):
    name: str
    description: str
    scope: str = "workspace"

class SkillImportRequest(BaseModel):
    source: Optional[str] = None          # 本地目录 / zip 文件路径 / GitHub URL
    zip_base64: Optional[str] = None      # 前端上传的 zip（base64）
    scope: str = "workspace"
    name: Optional[str] = None            # 覆盖技能名（单技能来源时）
    overwrite: bool = False               # 同名技能覆盖更新（保留本地 .env）

class SkillUpdateRequest(BaseModel):
    description: str                      # 新的技能描述
    scope: str = "workspace"


class PluginImportRequest(BaseModel):
    source: Optional[str] = None          # 本地目录 / zip 文件路径 / GitHub URL（含子目录）
    zip_base64: Optional[str] = None      # 前端上传的 zip（base64）
    name: Optional[str] = None            # 覆盖插件名（单插件来源时）
    overwrite: bool = False               # 同名插件已存在时覆盖安装（更新）
    version: Optional[str] = None         # 来源版本（用于 installed.json 记录）


class AgentSaveRequest(BaseModel):
    profile: Dict[str, Any]               # AgentProfile 字段（id/description/prompt/tools/permissions）


# ---------------------------------------------------------------- 鉴权


class TokenAuth:
    def __init__(self, token: Optional[str]) -> None:
        self.token = token

    def check(self, request: Request) -> Optional[JSONResponse]:
        if not self.token:
            return None
        auth = request.headers.get("Authorization", "")
        if auth == f"Bearer {self.token}":
            return None
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)


# ---------------------------------------------------------------- 应用工厂


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


def create_app(app: AgentApp, token: Optional[str] = None) -> FastAPI:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(_fast_app: FastAPI):
        # models.dev 元数据同步：启动零网络等待，进入系统后在后台线程刷新。
        # 缓存未过期（7 天）时后台任务也只是读盘；查询侧永远走缓存/内置表，
        # 刷新结果落盘后自然生效——任何时刻都不阻塞启动与用户操作。
        import asyncio as _asyncio

        def _bg_refresh():
            try:
                app.refresh_model_meta()
            except Exception:
                pass

        _refresh_task = _asyncio.create_task(_asyncio.to_thread(_bg_refresh))
        await app.mcp_manager.start()
        yield
        # 应用关闭时回收后台任务（to_thread 的线程无法强杀，仅标记取消）
        _refresh_task.cancel()

    fast_app = FastAPI(title="lite-work", version=VERSION, lifespan=_lifespan)
    auth = TokenAuth(token)
    tasks = TaskManager(app)

    fast_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _check_auth(request: Request) -> None:
        denied = auth.check(request)
        if denied is not None:
            raise HTTPException(status_code=401, detail="Unauthorized")

    def _require_workspace() -> str:
        if not app.workspace:
            raise HTTPException(status_code=409, detail="请先打开项目后再创建会话或执行任务")
        return app.workspace

    # ------------------------------------------------------------ 状态与配置

    @fast_app.get("/api/status")
    async def status(request: Request):
        _check_auth(request)
        llm_active = app.llm_registry.active
        settings = app.llm_registry.get_active_provider_settings()
        return {
            "version": VERSION,
            "workspace": app.workspace,
            "model": settings.get("model", ""),
            "base_url": settings.get("base_url", ""),
            "active_provider": llm_active,
            "api_key_configured": bool(settings.get("api_key")),
            "active_tasks": tasks.active_count(),
            "sessions_count": len(app.session_store.list()),
            "token_auth": bool(token),
        }

    @fast_app.get("/api/config")
    async def get_config(request: Request):
        _check_auth(request)
        return {
            k: app.config.get(k) for k in (
                "max_steps", "token_budget", "tool_timeout",
                "auto_approve", "pricing", "context_full_turns", "llm_timeout",
                "llm_retries", "skill_permissions", "subagent_timeout",
                # 多智能体（docs/multi-agent-design.md §3 配置面）
                "max_parallel_agents", "agent_total_limit", "agent_max_steps",
                "agent_max_steps_cap", "agent_spawn_depth", "agent_message_max_chars",
                "agent_meeting_rounds", "agent_ledger_interval", "agent_persist_max",
                "agent_collab_mode",
            )
        }

    @fast_app.post("/api/config")
    async def update_config(payload: ConfigUpdateRequest, request: Request):
        _check_auth(request)
        app.save_config(payload.updates)
        return {"ok": True}

    # ------------------------------------------------------------ LLM 配置

    @fast_app.get("/api/llm/providers")
    async def llm_providers(request: Request):
        _check_auth(request)
        return app.llm_provider_meta()

    @fast_app.get("/api/llm/config")
    async def llm_config(request: Request):
        _check_auth(request)
        return app.get_llm_config()

    @fast_app.post("/api/llm/config")
    async def update_llm_config(payload: LLMConfigRequest, request: Request):
        _check_auth(request)
        return app.update_llm_config(
            active=payload.active,
            providers=payload.providers,
        )

    @fast_app.post("/api/llm/test")
    async def test_llm(payload: LLMTestRequest, request: Request):
        _check_auth(request)
        result = await app.test_llm(
            provider_id=payload.provider_id or app.llm_registry.active,
            overrides=payload.overrides,
        )
        return result

    @fast_app.get("/api/context/stats")
    async def context_stats(session_id: str = "", request: Request = None):
        if request:
            _check_auth(request)
        if not session_id:
            return {"session": {}}
        # 会话生效模型（会话覆盖 > 全局默认）及对应上下文窗口，
        # 让前端在切换模型/重开会话时立即刷新「上下文情况」面板，而非等下一轮 SSE
        snapshot = app.session_store.load(session_id)
        override = snapshot.metadata.get("model") if snapshot else None
        if not isinstance(override, dict):
            override = None
        provider_id = override.get("provider") if override else app.llm_registry.active
        if override and override.get("model"):
            model_name = override["model"]
        else:
            model_name = app.llm_registry.get_active_provider_settings().get("model", "")
        return {
            "model": model_name,
            "context_window": app.llm_registry.get_context_window(provider_id, model_name),
            "session": app.get_context_session_stats(session_id),
        }

    # ------------------------------------------------------------ 安全规则

    @fast_app.get("/api/security")
    async def get_security(request: Request):
        _check_auth(request)
        return app.guard.to_dict()

    @fast_app.post("/api/security")
    async def update_security(payload: SecurityUpdateRequest, request: Request):
        _check_auth(request)
        app.update_security_rules(payload.rules)
        return {"ok": True}

    # ------------------------------------------------------------ MCP Server 配置

    @fast_app.get("/api/mcp")
    async def mcp_status(request: Request):
        _check_auth(request)
        return app.mcp_status()

    @fast_app.post("/api/mcp")
    async def update_mcp(payload: MCPServersUpdateRequest, request: Request):
        _check_auth(request)
        # 任务运行时工具集不可热变（运行中任务的 registry 已装配完成）
        if tasks.active_count() > 0:
            raise HTTPException(status_code=409, detail="当前有任务运行，请等待任务结束后再更新 MCP 配置")
        status = await app.update_mcp_servers(payload.servers)
        return {"ok": True, **status}

    # ------------------------------------------------------------ 会话管理

    @fast_app.get("/api/sessions")
    async def list_sessions(request: Request, workspace: str = ""):
        _check_auth(request)
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

    @fast_app.post("/api/sessions")
    async def create_session(payload: SessionCreateRequest, request: Request):
        _check_auth(request)
        _require_workspace()

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

    @fast_app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str, request: Request):
        _check_auth(request)
        snap = app.session_store.load(session_id)
        if snap is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return snap.to_dict()

    @fast_app.delete("/api/sessions/cleanup")
    async def cleanup_sessions(request: Request):
        """一键清理孤儿会话：删除所有关联项目目录已不存在的会话。"""
        _check_auth(request)
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
                app.session_store.delete(sid)
                deleted += 1
                # 附带清理该会话的 TODO 看板（内存 + 磁盘）
                try:
                    app.todo_plugin.delete_board(sid)
                except Exception:
                    pass
        return {"ok": True, "deleted": deleted}

    @fast_app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str, request: Request):
        _check_auth(request)
        ok = app.session_store.delete(session_id)
        if not ok:
            raise HTTPException(status_code=404, detail="会话不存在")
        # 附带清理该会话的 TODO 看板（内存 + 磁盘）
        try:
            app.todo_plugin.delete_board(session_id)
        except Exception:
            pass
        return {"ok": True}

    @fast_app.get("/api/todos")
    async def get_todos(session_id: str = "", request: Request = None):
        """读取会话 TODO 看板（内存命中优先，未命中从磁盘恢复；刷新/重启后可还原）。"""
        if request:
            _check_auth(request)
        if not session_id:
            return {"todos": []}
        return {"todos": app.todo_plugin.get(session_id.strip())}

    @fast_app.get("/api/sessions/{session_id}/model")
    async def get_session_model(session_id: str, request: Request):
        _check_auth(request)
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

    @fast_app.post("/api/sessions/{session_id}/model")
    async def set_session_model(session_id: str, payload: SessionModelRequest, request: Request):
        _check_auth(request)
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

    # ------------------------------------------------------------ Skills 与命令

    @fast_app.get("/api/skills")
    async def list_skills(request: Request):
        _check_auth(request)
        try:
            return {"skills": app.skills_list()}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @fast_app.get("/api/skills/{name}")
    async def read_skill(name: str, request: Request):
        _check_auth(request)
        content = app.skills_read(name)
        if content is None:
            raise HTTPException(status_code=404, detail=f"技能 {name!r} 不存在")
        return {"name": name, "content": content}

    @fast_app.post("/api/skills/create")
    async def create_skill(payload: SkillCreateRequest, request: Request):
        _check_auth(request)
        try:
            return app.skills_create(payload.name, payload.description, payload.scope)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @fast_app.post("/api/skills/import")
    async def import_skill(payload: SkillImportRequest, request: Request):
        _check_auth(request)
        try:
            if payload.zip_base64:
                # 检查 zip 大小（base64 编码膨胀约 1.33 倍）
                max_bytes = app.config.get("max_zip_size_mb", 20) * 1024 * 1024
                raw_size = len(payload.zip_base64) * 3 // 4
                if raw_size > max_bytes:
                    max_mb = max_bytes // (1024 * 1024)
                    raise ValueError(f"zip 文件过大（{raw_size / 1024 / 1024:.1f}MB），上限为 {max_mb}MB")
                data = base64.b64decode(payload.zip_base64)
                return {"skills": app.skills_import_zip(data, payload.scope, payload.name, payload.overwrite)}
            if payload.source:
                return {"skills": app.skills_import(payload.source, payload.scope, payload.name, payload.overwrite)}
            raise ValueError("需要 source（目录/zip/GitHub URL）或 zip_base64")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @fast_app.put("/api/skills/{name}")
    async def update_skill(name: str, payload: SkillUpdateRequest, request: Request):
        _check_auth(request)
        try:
            return app.skills_update(name, payload.description, payload.scope)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @fast_app.delete("/api/skills/{name}")
    async def delete_skill(name: str, request: Request, scope: str = "workspace"):
        _check_auth(request)
        try:
            return app.skills_delete(name, scope)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # ------------------------------------------------------------ Plugins 管理

    @fast_app.get("/api/plugins")
    async def list_plugins(request: Request):
        _check_auth(request)
        try:
            return {"plugins": app.plugins_list()}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @fast_app.get("/api/plugins/builtin")
    async def list_builtin_plugins(request: Request):
        _check_auth(request)
        try:
            return {"plugins": app.plugins_builtin()}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @fast_app.get("/api/plugins/community")
    async def community_plugins(url: str = "", request: Request = None):
        if request:
            _check_auth(request)
        try:
            return app.plugins_community(url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @fast_app.post("/api/plugins/import")
    async def import_plugin(payload: PluginImportRequest, request: Request):
        _check_auth(request)
        try:
            if payload.zip_base64:
                import base64 as _base64
                raw = _base64.b64decode(payload.zip_base64)
                return {"plugins": app.plugins_import_zip(raw, payload.name, payload.overwrite)}
            if payload.source:
                return {"plugins": app.plugins_install(
                    payload.source, payload.name, payload.overwrite, payload.version)}
            raise ValueError("需要 source（目录/zip/GitHub URL）或 zip_base64")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @fast_app.delete("/api/plugins/{name}")
    async def delete_plugin(name: str, request: Request):
        _check_auth(request)
        try:
            return app.plugins_delete(name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @fast_app.get("/api/commands")
    async def list_commands(request: Request):
        _check_auth(request)
        return {"commands": app.commands_list()}

    @fast_app.post("/api/compact")
    async def compact_session(payload: CompactRequest, request: Request):
        """手动压缩会话上下文：旧轮次摘要化，最近 N 轮原样保留，立即落盘。"""
        _check_auth(request)
        _require_workspace()
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

    # ------------------------------------------------------------ 工具与工作区

    @fast_app.get("/api/tools")
    async def list_tools(request: Request, agent_id: str = ""):
        _check_auth(request)
        _require_workspace()
        # 按 Agent 裁剪工具集：前端「工具」Tab 展示当前 Agent 可用工具，随 Agent 切换刷新
        registry = (
            app.create_agent_registry(agent_id)
            if agent_id
            else app.build_registry()
        )
        return [
            {"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in registry.get_tools()
        ]

    @fast_app.get("/api/workspace/tree")
    async def workspace_tree(depth: int = 3, request: Request = None):
        if request:
            _check_auth(request)
        from ..tools.filesystem import FileSystemTools

        workspace = _require_workspace()
        fs = FileSystemTools(workspace)
        return {"workspace": workspace, "tree": fs._file_tree({"maxDepth": depth})}

    @fast_app.get("/api/workspace/tree-json")
    async def workspace_tree_json(path: str = "", request: Request = None):
        """结构化目录树（侧边栏文件页签）：按路径懒加载 + git 状态字母。"""
        if request:
            _check_auth(request)
        from .tree import list_tree

        workspace = _require_workspace()
        rel = path.strip().lstrip("/\\") or ""
        try:
            data = await asyncio.to_thread(list_tree, workspace, rel)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {
            "workspace": workspace,
            "path": rel,
            "git": {"branch": data["branch"], "has_repo": data["has_repo"]},
            "entries": data["entries"],
        }

    # ------------------------------------------------------------ Agent 配置

    @fast_app.get("/api/agents")
    async def list_agents(request: Request):
        _check_auth(request)
        return app.agents_meta()

    @fast_app.get("/api/agents/tools")
    async def agents_tools(request: Request):
        """返回全部可用工具 + 各 agent 的工具白名单（供设置页配置）。"""
        _check_auth(request)
        return app.agents_available_tools()

    @fast_app.post("/api/agents/save")
    async def save_agent(payload: AgentSaveRequest, request: Request):
        _check_auth(request)
        try:
            return app.agent_save(payload.profile)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @fast_app.delete("/api/agents/{agent_id}")
    async def delete_agent(agent_id: str, request: Request):
        _check_auth(request)
        try:
            return app.agent_delete(agent_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # ------------------------------------------------------------ 工作区

    @fast_app.post("/api/workspace")
    async def set_workspace(payload: WorkspaceUpdateRequest, request: Request):
        _check_auth(request)
        import os as _os
        path = _os.path.abspath(_os.path.expanduser(payload.path))
        if not _os.path.isdir(path):
            raise HTTPException(status_code=400, detail=f"目录不存在: {path}")
        if tasks.active_count() > 0:
            raise HTTPException(status_code=409, detail="当前有任务运行，请停止或等待任务结束后再切换项目")
        app.workspace = path
        # 切换工作区即记录到最近项目（含目录选择器/子 Agent 场景）
        try:
            kind = "code" if _os.path.isdir(_os.path.join(path, ".git")) else "project"
            app.remember_project(path, kind=kind)
        except Exception:
            pass
        return {"ok": True, "workspace": app.workspace}

    @fast_app.post("/api/projects/create")
    async def create_project(payload: ProjectCreateRequest, request: Request):
        """新建项目：在 parent 下创建 name 目录；git=True（默认）时初始化 git 仓库。

        用于「新建项目 / 新建代码」入口：新项目默认是 git 仓库，侧边栏文件树
        即可直接展示分支与状态（git tree）。
        """
        _check_auth(request)
        import os as _os
        import re as _re
        import subprocess as _sp

        name = (payload.name or "").strip()
        if not name or not _re.fullmatch(r"[\w.\-\u4e00-\u9fff]+", name):
            raise HTTPException(status_code=400, detail="项目名仅支持字母/数字/中文/._- 且不含空格")
        if name.startswith("."):
            raise HTTPException(status_code=400, detail="项目名不能以 . 开头")

        parent = _os.path.abspath(_os.path.expanduser(payload.parent))
        if not _os.path.isdir(parent):
            raise HTTPException(status_code=400, detail=f"父目录不存在: {parent}")

        target = _os.path.join(parent, name)
        if _os.path.exists(target):
            raise HTTPException(status_code=409, detail=f"目录已存在: {target}")

        _os.makedirs(target, exist_ok=False)

        git_initialized = False
        if payload.git:
            try:
                proc = _sp.run(
                    ["git", "init", "-b", "main"],
                    cwd=target, capture_output=True, text=True, timeout=30,
                )
                # 旧版 git 不支持 -b，退回普通 init
                if proc.returncode != 0:
                    proc = _sp.run(
                        ["git", "init"],
                        cwd=target, capture_output=True, text=True, timeout=30,
                    )
                git_initialized = proc.returncode == 0 and _os.path.isdir(_os.path.join(target, ".git"))
            except (OSError, _sp.TimeoutExpired):
                git_initialized = False

        # 新建即记住（代码仓库优先按 git 判定 kind）
        try:
            app.remember_project(target, kind="code" if git_initialized else "project")
        except Exception:
            pass
        return {"ok": True, "path": target, "name": name, "git_initialized": git_initialized}

    @fast_app.get("/api/projects/recent")
    async def list_recent_projects(request: Request = None):
        """最近打开的项目（侧边栏「项目」页签）。"""
        if request:
            _check_auth(request)
        return {"items": app.list_recent_projects()}

    @fast_app.post("/api/projects/recent")
    async def open_recent_project(payload: WorkspaceUpdateRequest, request: Request):
        """打开（切换到）一个最近项目：切换工作区并置顶记录。"""
        _check_auth(request)
        import os as _os

        path = _os.path.abspath(_os.path.expanduser(payload.path))
        if not _os.path.isdir(path):
            raise HTTPException(status_code=400, detail=f"目录不存在: {path}")
        if tasks.active_count() > 0:
            raise HTTPException(status_code=409, detail="当前有任务运行，请停止或等待任务结束后再切换项目")
        # kind 由前端按入口传入（打开代码=code / 打开项目=project），缺省按 git 判定
        kind = "code" if _os.path.isdir(_os.path.join(path, ".git")) else "project"
        app.remember_project(path, kind=kind)
        app.workspace = path
        return {"ok": True, "workspace": path, "kind": kind}

    @fast_app.delete("/api/projects/recent")
    async def remove_recent_project(path: str, request: Request):
        """从最近列表移除一个项目（不删除磁盘文件）。"""
        _check_auth(request)
        app.forget_project(path)
        return {"ok": True}

    @fast_app.post("/api/projects/pin")
    async def toggle_project_pin(payload: WorkspaceUpdateRequest, request: Request):
        """置顶/取消置顶一个最近项目。返回翻转后的 pinned 状态。"""
        _check_auth(request)
        try:
            pinned = app.toggle_project_pin(payload.path)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"ok": True, "path": payload.path, "pinned": pinned}

    @fast_app.get("/api/fs/list")
    async def fs_list(path: str = "", show_hidden: bool = False, request: Request = None):
        """浏览任意目录（用于「打开项目」目录树选择）。

        - show_hidden=False（默认）过滤 . 开头的隐藏文件/目录
        - Windows：path 为空或盘符根时列出所有可用盘符（目录选择需先选盘）
        """
        if request:
            _check_auth(request)
        import os as _os
        import string as _string
        import sys as _sys

        base = _os.path.expanduser(path) if path else (app.workspace or _os.path.expanduser("~"))
        base = _os.path.abspath(base)
        if not _os.path.isdir(base):
            raise HTTPException(status_code=400, detail=f"目录不存在: {base}")

        is_win = _sys.platform == "win32"

        def list_drives():
            drives = []
            for letter in _string.ascii_uppercase:
                d = f"{letter}:\\"
                if _os.path.isdir(d):
                    drives.append(d)
            return drives

        # Windows 盘符根（如 C:\）：parent 置空并在目录里列出全部盘符
        at_drive_root = is_win and _os.path.splitdrive(base)[1] in ("\\", "/", "")
        parent = None
        if at_drive_root:
            parent = None
        else:
            parent = _os.path.dirname(base) or None
            # POSIX 根目录 / 的 parent 为 None
            if base == _os.path.sep:
                parent = None

        dirs, files = [], []
        try:
            entries = _os.listdir(base)
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"无法读取: {exc}")

        for name in entries:
            full = _os.path.join(base, name)
            if not show_hidden and (name.startswith(".") or name == "Thumbs.db" or name == "desktop.ini"):
                continue
            try:
                if _os.path.isdir(full):
                    dirs.append(name)
                else:
                    files.append(name)
            except OSError:
                continue

        # Windows 盘符根：把其他盘符也列为可进入的"目录"
        if at_drive_root:
            cur_drive = _os.path.splitdrive(base)[0].upper()
            for d in list_drives():
                if d[0] != cur_drive[0]:
                    dirs.insert(0, d)

        def key(n: str):
            return n.lower()

        dirs.sort(key=key)
        files.sort(key=key)
        cap = 500
        return {
            "path": base,
            "parent": parent,
            "home": _os.path.expanduser("~"),
            "is_workspace": base == app.workspace,
            "dirs": dirs[:cap],
            "files": files[:cap],
            "truncated": len(dirs) + len(files) > cap,
        }

    @fast_app.get("/api/fs/read")
    async def fs_read(path: str, request: Request = None):
        """读取工作区内的文件，返回内容、语言、行数和 git diff。"""
        if request:
            _check_auth(request)
        import os as _os

        workspace = _require_workspace()
        target = _os.path.abspath(_os.path.join(workspace, path))
        if not (target == workspace or target.startswith(workspace + _os.sep)):
            raise HTTPException(status_code=403, detail="路径越界")
        if not _os.path.isfile(target):
            raise HTTPException(status_code=404, detail=f"文件不存在: {path}")

        with open(target, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        lines = content.split("\n")
        ext = _os.path.splitext(path)[1].lower()

        # 简单语言检测
        lang_map = {
            ".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "tsx",
            ".jsx": "jsx", ".go": "go", ".rs": "rust", ".java": "java",
            ".c": "c", ".cpp": "cpp", ".h": "c", ".hpp": "cpp",
            ".css": "css", ".scss": "scss", ".less": "less",
            ".html": "html", ".htm": "html", ".xml": "xml", ".json": "json",
            ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
            ".md": "markdown", ".txt": "text", ".sh": "bash", ".bash": "bash",
            ".zsh": "bash", ".sql": "sql", ".rb": "ruby",
            ".swift": "swift", ".kt": "kotlin", ".svelte": "svelte",
            ".vue": "vue", ".astro": "astro",
        }
        language = lang_map.get(ext, "")

        # 获取 git diff（工作区 vs HEAD）
        diff_text = ""
        try:
            proc = __import__("subprocess").run(
                ["git", "-C", workspace, "diff", "HEAD", "--", path],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=10,
            )
            if proc.returncode == 0:
                diff_text = proc.stdout
        except Exception:
            pass

        return {
            "path": path,
            "content": content,
            "language": language,
            "lines": len(lines),
            "size": len(content),
            "diff": diff_text,
        }

    @fast_app.get("/api/workspace/diff")
    async def workspace_file_diff(path: str, request: Request = None):
        """获取单个文件的 git diff（工作区 vs HEAD）。"""
        if request:
            _check_auth(request)
        workspace = _require_workspace()
        try:
            proc = __import__("subprocess").run(
                ["git", "-C", workspace, "diff", "HEAD", "--", path],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=10,
            )
            diff_text = proc.stdout if proc.returncode == 0 else ""
        except Exception:
            diff_text = ""
        additions = len([l for l in diff_text.split("\n") if l.startswith("+") and not l.startswith("+++")])
        deletions = len([l for l in diff_text.split("\n") if l.startswith("-") and not l.startswith("---")])
        return {"path": path, "diff": diff_text, "additions": additions, "deletions": deletions}

    # ------------------------------------------------------------ 办公场景：文件上传 / 产出物下载（AGI 通用入口）

    @fast_app.post("/api/upload")
    async def upload_file(request: Request):
        """接收 multipart 文件上传，保存到工作区 .uploads/ 目录。

        办公场景入口：用户把 CSV/Excel/文档等素材丢给 Agent 处理。
        返回 {path: 工作区相对路径, size, name}，Agent 可直接用 read_file /
        data_analyze 读取。
        """
        if request:
            _check_auth(request)
        import os as _os

        workspace = _require_workspace()
        try:
            form = await request.form()
        except Exception:
            raise HTTPException(status_code=400, detail="无效的表单请求")
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(status_code=400, detail="缺少文件字段 file")

        filename = _os.path.basename(getattr(upload, "filename", "") or "upload.bin")
        # 清理文件名中的路径成分与危险字符
        filename = "".join(c for c in filename if c not in '\\/:*?"<>|').strip() or "upload.bin"
        size_limit = 50 * 1024 * 1024  # 50MB
        data = await upload.read()
        if len(data) > size_limit:
            raise HTTPException(status_code=413, detail="文件超过 50MB 上传限制")
        if not data:
            raise HTTPException(status_code=400, detail="空文件")

        uploads_dir = _os.path.join(workspace, ".uploads")
        _os.makedirs(uploads_dir, exist_ok=True)
        # 同名冲突：追加序号
        base, ext = _os.path.splitext(filename)
        target = _os.path.join(uploads_dir, filename)
        n = 1
        while _os.path.exists(target):
            target = _os.path.join(uploads_dir, f"{base}_{n}{ext}")
            n += 1
        with open(target, "wb") as f:
            f.write(data)

        rel_path = _os.path.relpath(target, workspace).replace("\\", "/")
        return {"path": rel_path, "name": _os.path.basename(target), "size": len(data)}

    @fast_app.get("/api/files/download")
    async def download_file(path: str, request: Request = None):
        """下载工作区内的文件（用于办公产出物：docx/xlsx/pptx/pdf/图片）。"""
        if request:
            _check_auth(request)
        import os as _os
        from urllib.parse import quote
        from fastapi.responses import FileResponse

        workspace = _require_workspace()
        target = _os.path.abspath(_os.path.join(workspace, path))
        if not (target == workspace or target.startswith(workspace + _os.sep)):
            raise HTTPException(status_code=403, detail="路径越界")
        if not _os.path.isfile(target):
            raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
        filename = _os.path.basename(target)
        return FileResponse(
            target,
            filename=filename,
            content_disposition_type="attachment",
            headers={"Access-Control-Expose-Headers": "Content-Disposition"},
        )

    @fast_app.get("/api/outputs")
    async def list_outputs(request: Request = None):
        """列出工作区 .outputs/（Agent 产出物）与 .uploads/（用户上传素材）下的文件。

        供侧边栏「产出物」Tab 展示：文件名/相对路径/大小/修改时间/来源。
        """
        if request:
            _check_auth(request)
        import os as _os
        from datetime import datetime

        workspace = _require_workspace()
        items = []
        for source, rel_dir in (("outputs", ".outputs"), ("uploads", ".uploads")):
            base = _os.path.join(workspace, rel_dir)
            if not _os.path.isdir(base):
                continue
            try:
                entries = sorted(_os.listdir(base))
            except OSError:
                continue
            for name in entries:
                full = _os.path.join(base, name)
                if not _os.path.isfile(full):
                    continue
                try:
                    stat = _os.stat(full)
                except OSError:
                    continue
                items.append({
                    "name": name,
                    "path": f"{rel_dir}/{name}".replace("\\", "/"),
                    "source": source,
                    "size": stat.st_size,
                    "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                })
        # 新产出的排前面
        items.sort(key=lambda x: x["mtime"], reverse=True)
        return {"items": items}

    @fast_app.get("/api/outputs/zip")
    async def download_outputs_zip(include_uploads: bool = False, request: Request = None):
        """把 .outputs/（可选含 .uploads/）打包为 zip 一键下载。"""
        if request:
            _check_auth(request)
        import io as _io
        import os as _os
        import zipfile as _zipfile

        workspace = _require_workspace()
        sources = [".outputs"] + ([".uploads"] if include_uploads else [])
        # 只收顶层常规文件（与 /api/outputs 列表口径一致）
        collected = []
        total = 0
        limit = 512 * 1024 * 1024  # 512MB 安全上限
        for rel_dir in sources:
            base = _os.path.join(workspace, rel_dir)
            if not _os.path.isdir(base):
                continue
            for name in sorted(_os.listdir(base)):
                full = _os.path.join(base, name)
                if not _os.path.isfile(full):
                    continue
                size = _os.path.getsize(full)
                if total + size > limit:
                    raise HTTPException(status_code=413, detail="产出物总大小超过 512MB 上限，请先清理部分文件")
                collected.append((rel_dir, name, full))
                total += size

        if not collected:
            raise HTTPException(status_code=404, detail="没有可下载的产出物")

        buf = _io.BytesIO()
        with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as zf:
            for rel_dir, name, full in collected:
                # 归档名带 outputs/ 前缀，避免与 uploads 混淆
                zf.write(full, f"{rel_dir.strip('.')}/{name}")
        buf.seek(0)
        from datetime import datetime as _dt

        stamp = _dt.now().strftime("%Y%m%d_%H%M")
        filename = f"lite-work-outputs-{stamp}.zip"
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Access-Control-Expose-Headers": "Content-Disposition",
            },
        )

    @fast_app.delete("/api/outputs")
    async def clear_outputs(scope: str = "outputs", request: Request = None):
        """清空产出目录：scope = outputs（默认）/ uploads / all。

        只删除 .outputs/.uploads 下的顶层文件，不影响代码与其他数据。
        """
        if request:
            _check_auth(request)
        import os as _os

        if scope not in ("outputs", "uploads", "all"):
            raise HTTPException(status_code=400, detail="scope 仅支持 outputs / uploads / all")
        workspace = _require_workspace()
        dirs = [".outputs", ".uploads"] if scope == "all" else [f".{scope}"]
        deleted = 0
        for rel_dir in dirs:
            base = _os.path.join(workspace, rel_dir)
            if not _os.path.isdir(base):
                continue
            for name in _os.listdir(base):
                full = _os.path.join(base, name)
                if not _os.path.isfile(full):
                    continue
                try:
                    _os.remove(full)
                    deleted += 1
                except OSError:
                    continue
        return {"ok": True, "scope": scope, "deleted": deleted}

    @fast_app.delete("/api/files")
    async def delete_file(path: str, request: Request = None):
        """删除单个产出物/素材文件（仅限 .outputs/.uploads 内，防误删代码）。"""
        if request:
            _check_auth(request)
        import os as _os

        workspace = _require_workspace()
        rel = (path or "").strip().lstrip("/\\").replace("\\", "/")
        if not rel.split("/")[0] in (".outputs", ".uploads"):
            raise HTTPException(status_code=403, detail="仅支持删除 .outputs/.uploads 内的文件")
        target = _os.path.abspath(_os.path.join(workspace, rel))
        if not (target.startswith(workspace + _os.sep) and rel.split("/")[0] in (".outputs", ".uploads")):
            raise HTTPException(status_code=403, detail="路径越界")
        if not _os.path.isfile(target):
            raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
        try:
            _os.remove(target)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"删除失败: {exc}")
        return {"ok": True, "path": rel}

    @fast_app.get("/api/files/raw")
    async def serve_file_raw(path: str, request: Request = None):
        """内联返回工作区文件（Content-Disposition: inline）。

        用于浏览器直接渲染预览：图片（png/jpg/svg）与 PDF。
        """
        if request:
            _check_auth(request)
        import os as _os
        from fastapi.responses import FileResponse

        workspace = _require_workspace()
        target = _os.path.abspath(_os.path.join(workspace, path))
        if not (target == workspace or target.startswith(workspace + _os.sep)):
            raise HTTPException(status_code=403, detail="路径越界")
        if not _os.path.isfile(target):
            raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
        return FileResponse(target, media_type=_guess_media_type(target),
                            headers={"Cache-Control": "no-store"})

    @fast_app.get("/api/files/preview")
    async def preview_file(path: str, request: Request = None):
        """结构化预览办公产出物。

        - 图片（png/jpg/jpeg/svg/gif/webp）与 PDF → kind="media"，前端用 /api/files/raw 渲染
        - xlsx → kind="table"，解析前 N 行返回表头+行数据（多 sheet 返回首个，附 sheet 名列表）
        - docx → kind="text"，提取段落与表格文字
        - pptx → kind="slides"，提取每页标题与要点
        - 其他文本类 → kind="text"，直接读前 20000 字符
        """
        if request:
            _check_auth(request)
        import os as _os

        workspace = _require_workspace()
        target = _os.path.abspath(_os.path.join(workspace, path))
        if not (target == workspace or target.startswith(workspace + _os.sep)):
            raise HTTPException(status_code=403, detail="路径越界")
        if not _os.path.isfile(target):
            raise HTTPException(status_code=404, detail=f"文件不存在: {path}")

        ext = _os.path.splitext(target)[1].lower()
        name = _os.path.basename(target)

        if ext in (".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp", ".pdf"):
            from urllib.parse import quote
            return {"name": name, "kind": "media",
                    "media_type": _guess_media_type(target),
                    "raw_url": f"/api/files/raw?path={quote(path)}"}

        if ext in (".xlsx", ".xlsm"):
            try:
                import openpyxl
                wb = openpyxl.load_workbook(target, read_only=True, data_only=True)
            except Exception as exc:
                raise HTTPException(status_code=422, detail=f"解析 Excel 失败: {exc}")
            sheet_name = wb.sheetnames[0] if wb.sheetnames else ""
            ws = wb[sheet_name] if sheet_name else None
            rows: list[list] = []
            max_rows, max_cols = 100, 30
            if ws is not None:
                for row in ws.iter_rows(max_row=max_rows, max_col=max_cols, values_only=True):
                    cells = ["" if v is None else str(v) for v in row]
                    # read_only 模式会按 ws 尺寸填充空单元格，裁掉行尾空白
                    while cells and cells[-1] == "":
                        cells.pop()
                    rows.append(cells)
            return {"name": name, "kind": "table", "sheet": sheet_name,
                    "sheets": wb.sheetnames, "rows": rows,
                    "truncated": (ws is not None and ws.max_row > max_rows)}

        if ext == ".docx":
            try:
                from docx import Document
                doc = Document(target)
            except Exception as exc:
                raise HTTPException(status_code=422, detail=f"解析 Word 失败: {exc}")
            parts = []
            for p in doc.paragraphs:
                if p.text.strip():
                    style = (p.style.name or "").lower()
                    prefix = "#" * min(4, 1 + sum(1 for c in style if c.isdigit() and c != "0")) \
                        if "heading" in style else ""
                    parts.append(f"{prefix} {p.text.strip()}".strip())
            for ti, table in enumerate(doc.tables):
                parts.append(f"[表格 {ti + 1}]")
                for row in table.rows[:20]:
                    cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                    parts.append("| " + " | ".join(cells) + " |")
            text = "\n\n".join(parts)
            return {"name": name, "kind": "text", "text": text[:20000],
                    "truncated": len(text) > 20000}

        if ext == ".pptx":
            try:
                from pptx import Presentation
                prs = Presentation(target)
            except Exception as exc:
                raise HTTPException(status_code=422, detail=f"解析 PPT 失败: {exc}")
            slides = []
            for slide in prs.slides:
                title, bullets = "", []
                for shape in slide.shapes:
                    if not shape.has_text_frame:
                        continue
                    tf = shape.text_frame
                    is_title = shape == getattr(slide.shapes, "title", None)
                    for para in tf.paragraphs:
                        t = "".join(run.text for run in para.runs).strip()
                        if not t:
                            continue
                        if is_title and not title:
                            title = t
                        else:
                            bullets.append(t)
                slides.append({"title": title, "bullets": bullets[:12]})
            return {"name": name, "kind": "slides", "slides": slides}

        # 其他文件按文本读取
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                text = f.read(20000)
        except OSError as exc:
            raise HTTPException(status_code=422, detail=f"读取失败: {exc}")
        return {"name": name, "kind": "text", "text": text,
                "truncated": False}

    def _guess_media_type(target: str) -> str:
        import mimetypes
        mt, _ = mimetypes.guess_type(target)
        return mt or "application/octet-stream"

    # ------------------------------------------------------------ 聊天任务

    @fast_app.post("/api/chat")
    async def chat(payload: ChatRequest, request: Request):
        _check_auth(request)
        _require_workspace()
        session_id = payload.session_id.strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id 不能为空")
        prompt = payload.prompt.strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt 不能为空")

        handle = tasks.active_for_session(session_id)
        if handle is not None:
            # 会话已有任务在跑：本次输入作为补充指令排队，下一回合注入对话
            handle.queue_input(prompt)
            return {"task_id": handle.task_id, "queued": True}
        handle = tasks.start(session_id, prompt, agent_id=payload.agent_id,
                               reasoning_effort=payload.reasoning_effort)
        return {"task_id": handle.task_id}

    @fast_app.get("/api/tasks/{task_id}/events")
    async def task_events(task_id: str, request: Request):
        _check_auth(request)
        handle = tasks.get(task_id)
        if handle is None:
            raise HTTPException(status_code=404, detail="任务不存在")

        async def _stream():
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(handle.queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if item is None:
                        yield "data: [DONE]\n\n"
                        break
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            except asyncio.CancelledError:
                # 客户端断连：任务仍可继续，后台任务会保留状态，不在此清理
                try:
                    if not handle.queue.empty() and not handle.done:
                        handle.queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
                raise
            finally:
                # 仅在任务真正结束时（已收到 [DONE]）清理，避免断线重连 404
                if handle.done and handle.queue.empty():
                    tasks.cleanup(task_id)

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @fast_app.post("/api/tasks/{task_id}/stop")
    async def stop_task(task_id: str, request: Request):
        _check_auth(request)
        ok = tasks.stop(task_id)
        if not ok:
            raise HTTPException(status_code=404, detail="任务不存在")
        return {"ok": True}

    @fast_app.get("/api/tasks/background")
    async def list_background_tasks(request: Request):
        _check_auth(request)
        return {"tasks": app.background_tasks()}

    @fast_app.post("/api/tasks/background/{task_id}/kill")
    async def kill_background_task(task_id: str, request: Request):
        _check_auth(request)
        return {"ok": app.kill_background_task(task_id)}

    # ------------------------------------------------------------ 审批

    @fast_app.post("/api/approve")
    async def approve(payload: ApproveRequest, request: Request):
        _check_auth(request)
        ok = app.approval_gate.resolve(payload.approval_id, payload.approved, by="user")
        if not ok:
            raise HTTPException(status_code=404, detail="审批请求不存在或已处理")
        return {"ok": True, "approved": payload.approved}

    @fast_app.post("/api/question")
    async def answer_question(payload: QuestionAnswerRequest, request: Request):
        _check_auth(request)
        ok = app.question_gate.resolve(payload.question_id, payload.answer, by="user")
        if not ok:
            raise HTTPException(status_code=404, detail="问题不存在或已回答")
        return {"ok": True, "answer": payload.answer}

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
                from fastapi.responses import FileResponse

                return FileResponse(file_path)
            return await _serve_index(dist_dir)

    return fast_app


async def _serve_index(dist_dir: str):
    from fastapi.responses import FileResponse

    index_path = os.path.join(dist_dir, "index.html")
    if os.path.isfile(index_path):
        return FileResponse(index_path)
    return JSONResponse({"message": "lite-work 后端已就绪（前端未构建，运行 npm run build:web）"},
                        status_code=200)


def _resolve_dist_dir() -> str:
    """定位前端构建产物目录，兼容 PyInstaller 打包环境。"""
    candidates = []
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
