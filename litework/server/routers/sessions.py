# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""会话管理：/api/sessions*、/api/todos、/api/compact。"""
from __future__ import annotations

import os
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

# 个人任务落盘区（Goals 全局视图）：单一来源 core/personal.py（core 反思模块
# 也要用，避免 core→server 反向依赖）；re-export 保持既有引用（含测试）兼容。
from ...core.personal import PERSONAL_WORKSPACE, ensure_personal_workspace  # noqa: F401
from .context import ServerContext, _session_title


class SessionCreateRequest(BaseModel):
    name: Optional[str] = None
    workspace: Optional[str] = None

class SessionDeriveRequest(BaseModel):
    """会话派生请求：可选自定义新会话名称（默认「源标题 · 派生」）。"""
    name: Optional[str] = None

class SessionTodosSeedRequest(BaseModel):
    """预置会话 TODO（新建任务向导「计划项」）：每行为一个待办项，状态全置 pending。"""
    items: list[str] = []


class SessionPatchRequest(BaseModel):
    """会话属性更新（任务卡重命名/置顶）：字段缺省 = 不改。"""
    name: Optional[str] = None
    pinned: Optional[bool] = None


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


class SessionWorktreeRequest(BaseModel):
    """会话隔离工作树开关（/worktree）：持久化到 metadata，任务在独立 worktree 执行。"""
    enabled: bool = True


class WorktreeCleanRequest(BaseModel):
    """遗留 worktree 清理：name 指定单个（不填=全部）；include_dirty=true 连有改动的也删。"""
    name: Optional[str] = None
    include_dirty: bool = False


class DeleteSessionsRequest(BaseModel):
    """批量删除会话：ids=会话 ID 列表（单次最多 200 个）。"""
    ids: list[str] = []


async def _shutdown_session_agents(app, session_id: str) -> None:
    """停止该会话全部后台子 Agent（会话删除时防泄漏）。"""
    try:
        manager = app.agent_manager(session_id, create=False)
        if manager is not None:
            await manager.shutdown()
            app.agent_managers.pop(session_id, None)
    except Exception:
        pass


def _todo_progress(todos: list) -> Optional[Dict[str, Any]]:
    """TODO 看板 → 任务卡进度摘要；无看板返回 None（前端退化为轻量卡）。"""
    if not todos:
        return None
    done = sum(1 for t in todos if t.get("status") == "completed")
    current = next((t.get("content") for t in todos if t.get("status") == "in_progress"), "")
    pending = [t.get("content") for t in todos if t.get("status") == "pending"]
    return {
        "total": len(todos),
        "done": done,
        "current": current,
        "next": pending[:2],
    }


def _project_badge(workspace: str) -> Dict[str, Any]:
    """任务卡项目徽标数据（全局视图）：名称 + 类型；个人区固定 👤 个人。

    kind 复用 project_scaffold 的目录内容启发式（code/project），仅在
    全局视图（无 workspace 过滤）时逐会话调用；文件存在性检查轻量可接受。
    """
    if os.path.normcase(os.path.abspath(workspace)) == os.path.normcase(os.path.abspath(PERSONAL_WORKSPACE)):
        return {"name": "个人", "kind": "personal"}
    name = os.path.basename(workspace.rstrip("/\\")) or workspace
    kind = "project"
    try:
        from ...tools.project_scaffold import classify_project_kind as _classify
        kind = _classify(workspace)
    except Exception:
        pass
    return {"name": name, "kind": kind}


#: 交付物识别（P3 交付物指针）：项目结构 v2「交付物平铺根目录」——完成态任务卡
#: 的交付物 = 落盘目录顶层、任务期间产出（mtime ≥ 会话创建时间）的文档/媒体文件。
#: 正向扩展名清单保守取常见产出格式；代码类（.py/.ts/...）与配置不在此列。
_DELIV_EXT = {
    ".docx", ".doc", ".pdf", ".xlsx", ".xls", ".pptx", ".ppt",
    ".md", ".txt", ".csv", ".html", ".rtf", ".odt",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".zip", ".7z",
}
#: 根目录里这些 .md 是工程约定文件，不算交付物
_DELIV_NAME_SKIP = {"agents.md", "readme.md", "changelog.md", "claude.md", "todo.md"}


def _deliverables(workspace: str, created_at_ms: int, limit: int = 5) -> list:
    """完成态任务的交付物快照：根目录顶层文件，按修改时间倒序。

    - mtime ≥ 会话创建时间（毫秒转秒）：只认任务期间产出，项目里的存量
      文档不冒充本次交付；
    - 隐藏文件/目录跳过；单个 scandir 完成，列表页 O(n) 轻量读可接受
      （仅对「TODO 全部完成」的会话计算）。
    """
    if not workspace or not os.path.isdir(workspace):
        return []
    created_s = created_at_ms / 1000 if created_at_ms else 0
    out: list = []
    try:
        with os.scandir(workspace) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False) or e.name.startswith("."):
                        continue
                    if os.path.splitext(e.name)[1].lower() not in _DELIV_EXT:
                        continue
                    if e.name.lower() in _DELIV_NAME_SKIP:
                        continue
                    st = e.stat(follow_symlinks=False)
                    if created_s and st.st_mtime < created_s:
                        continue
                    out.append({"name": e.name, "mtime": int(st.st_mtime)})
                except OSError:
                    continue
    except OSError:
        return []
    out.sort(key=lambda x: -x["mtime"])
    return out[:limit]


def _last_activity(messages: list) -> Optional[Dict[str, Any]]:
    """消息尾部倒扫：最后一个工具调用（含成败）或助手文本 → 最近活动摘要。

    消息序列形如 assistant(tool_calls) → tool(结果) → assistant(…)；
    倒扫时先记住尾部 tool 结果的成败（按 tool_call_id 对齐），遇到带
    tool_calls 的 assistant 消息即组装返回；纯文本回复则截断为摘要。
    """
    import json as _json

    tail_results: Dict[str, Any] = {}  # tool_call_id → {ok}
    for m in reversed(messages):
        role = m.get("role")
        if role == "tool":
            content = m.get("content") or ""
            tc_id = m.get("tool_call_id") or ""
            tail_results[tc_id] = {
                "ok": not (content.startswith("[Error]") or content.startswith("[Execution Exception]")),
            }
            continue
        if role == "assistant":
            calls = m.get("tool_calls") or []
            if calls:
                call = calls[-1]
                tc = call.get("function", {}) if isinstance(call, dict) else {}
                name = tc.get("name") or "tool"
                args_raw = tc.get("arguments") or ""
                try:
                    args: Dict[str, Any] = _json.loads(args_raw) if isinstance(args_raw, str) else {}
                    # 摘要取首个命中的关键参数（含 camelCase 变体，如 write_file 的 filePath）
                    key = next((args[k] for k in
                                ("filePath", "filename", "path", "query", "command", "url", "name")
                                if args.get(k)), "")
                    summary = f"{name} {str(key)[:40]}".strip()
                except Exception:
                    summary = name
                tc_id = call.get("id") or ""
                ok = tail_results.get(tc_id, {}).get("ok")
                return {"summary": summary, "ok": ok, "tool": name}
            content = (m.get("content") or "").strip()
            if content:
                return {"summary": content[:60], "ok": None, "tool": ""}
    return None


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
            session_id = str(s.get("session_id") or "")
            metadata = s.get("metadata", {})
            # —— 任务卡聚合字段（Goals 视图；全部可选，旧前端无感）——
            todo_progress = _todo_progress(app.todo_plugin.get(session_id))
            last_activity = _last_activity(messages)
            # 交付物指针（P3）：仅 TODO 全部完成的会话计算（省一次根目录扫描）
            deliverables = (
                _deliverables(s_ws, int(s.get("created_at") or 0))
                if todo_progress and todo_progress["done"] >= todo_progress["total"]
                else []
            )
            subagent_count = len(metadata.get("subagent_records") or [])
            # 成本为内存态（本进程运行过的会话才有）；跨重启留空，不显示
            stats = app.get_context_session_stats(session_id)
            cost_usd = round(float(stats.get("cost_estimate", 0) or 0), 2) or None
            running = tasks.active_for_session(session_id) is not None
            result.append({
                "session_id": session_id,
                "created_at": s.get("created_at"),
                "updated_at": s.get("updated_at"),
                "message_count": len(messages),
                "title": _session_title(s),
                "metadata": metadata,
                "todo_progress": todo_progress,
                "last_activity": last_activity,
                "subagent_count": subagent_count,
                "cost_usd": cost_usd,
                "running": running,
                "pinned": bool(metadata.get("pinned", False)),
                "deliverables": deliverables,
                # 项目徽标（全局视图用；本项目视图忽略）
                "project": _project_badge(s_ws),
            })
        return result

    @router.get("/api/cards/{session_id}/{card_id}")
    async def get_card(session_id: str, card_id: str, request: Request):
        """读取富内容卡片（render_card 落盘的 HTML）。

        card_id 经格式校验（防路径注入）；文件不存在 → 404。HTML 由前端在
        sandbox iframe 中渲染（无脚本执行）。
        """
        ctx.check_auth(request)
        from ...tools.render_card import card_path
        try:
            path = card_path(app.config_dir, session_id, card_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        if not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="卡片不存在")
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"读取失败: {exc}")
        return {"session_id": session_id, "card_id": card_id, "html": content}

    @router.get("/api/personal-workspace")
    async def get_personal_workspace(request: Request):
        """个人任务落盘区（Goals 全局视图）：确保存在并返回路径（新建任务向导用）。"""
        ctx.check_auth(request)
        return {"path": ensure_personal_workspace()}

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

    @router.post("/api/sessions/{session_id}/derive")
    async def derive_session(session_id: str, payload: SessionDeriveRequest, request: Request):
        """派生会话：克隆源会话骨架（目标 / 协作模式 / 模型 / 隔离工作树开关 /
        TODO 结构）到新会话；对话历史不复制（派生 = 带骨架另起炉灶）。

        TODO 看板全部重置为 pending——「上一版已完成」不继承为新任务的进度。
        """
        ctx.check_auth(request)
        source = app.session_store.load(session_id)
        if source is None:
            raise HTTPException(status_code=404, detail="源会话不存在")
        src_meta = source.metadata or {}

        new_id = f"session_{uuid.uuid4().hex}"
        workspace = src_meta.get("workspace") or app.workspace
        new_meta: Dict[str, Any] = {}
        if workspace:
            new_meta["workspace"] = workspace
        name = (payload.name or "").strip()
        if name:
            new_meta["name"] = name[:120]
        elif src_meta.get("name"):
            new_meta["name"] = f"{src_meta['name']} · 派生"
        # 继承 goal / collab_mode / model；worktree 需先过 git 校验（同开关端点语义）
        for key in ("goal", "collab_mode", "model"):
            if src_meta.get(key):
                new_meta[key] = src_meta[key]
        worktree = False
        if src_meta.get("worktree"):
            mgr = app.worktree_manager()
            if mgr.is_git_repo():
                new_meta["worktree"] = True
                worktree = True

        app.session_store.save(new_id, [], new_meta)

        todos_copied = 0
        try:
            todos_copied = app.todo_plugin.seed_board(
                new_id, app.todo_plugin.get(session_id)
            )
        except Exception:
            pass  # TODO 复制失败不阻塞派生

        return {
            "session_id": new_id,
            "workspace": workspace,
            "goal": new_meta.get("goal"),
            "todos_copied": todos_copied,
            "worktree": worktree,
        }

    @router.patch("/api/sessions/{session_id}")
    async def patch_session(session_id: str, payload: SessionPatchRequest, request: Request):
        """更新会话属性（任务卡重命名/置顶）。

        - name：空串 = 清除自定义名（回退到首条消息推导）；
        - pinned：任务 Tab 排序时置顶（运行中 → pinned → 其余按更新时间倒序）。
        """
        ctx.check_auth(request)
        snap = app.session_store.load(session_id)
        if snap is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        updates: Dict[str, Any] = {}
        if payload.name is not None:
            name = payload.name.strip()
            if name:
                updates["name"] = name[:120]
            else:
                # 清除自定义名：save 会把空串当缺省，这里显式删除键
                meta = snap.metadata or {}
                meta.pop("name", None)
                app.session_store.save(session_id, snap.messages, meta)
        if payload.pinned is not None:
            updates["pinned"] = bool(payload.pinned)
        if updates:
            app.session_store.update_metadata(session_id, updates)
        refreshed = app.session_store.load(session_id)
        return {
            "session_id": session_id,
            "name": (refreshed.metadata or {}).get("name") if refreshed else None,
            "pinned": bool((refreshed.metadata or {}).get("pinned", False)) if refreshed else False,
        }

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

    @router.post("/api/sessions/delete-batch")
    async def delete_sessions_batch(payload: DeleteSessionsRequest, request: Request):
        """批量删除会话（复用单删逻辑；单个失败不中断整批，记入 failed）。

        会话删除成功后同样停掉该会话的后台子 Agent、清理 TODO 看板；
        不存在的会话记入 failed（{"id", "error": "会话不存在"}）继续处理其余。
        """
        ctx.check_auth(request)
        if not payload.ids:
            raise HTTPException(status_code=400, detail="ids 不能为空")
        if len(payload.ids) > 200:
            raise HTTPException(status_code=400, detail="单次最多删除 200 个会话")
        deleted = 0
        failed: list = []
        for sid in payload.ids:
            if not app.session_store.delete(sid):
                failed.append({"id": sid, "error": "会话不存在"})
                continue
            deleted += 1
            await _shutdown_session_agents(app, sid)
            # 附带清理该会话的 TODO 看板（内存 + 磁盘）
            try:
                app.todo_plugin.delete_board(sid)
            except Exception:
                pass
        return {"ok": True, "deleted": deleted, "failed": failed}

    @router.get("/api/todos")
    async def get_todos(request: Request, session_id: str = ""):
        """读取会话 TODO 看板（内存命中优先，未命中从磁盘恢复；刷新/重启后可还原）。"""
        ctx.check_auth(request)
        if not session_id:
            return {"todos": []}
        return {"todos": app.todo_plugin.get(session_id.strip())}

    @router.post("/api/sessions/{session_id}/todos")
    async def seed_session_todos(session_id: str, payload: SessionTodosSeedRequest, request: Request):
        """预置会话 TODO（新建任务向导「计划项」）：全部置为 pending（覆写已有看板）。

        种子经由 TodoPlugin.seed_board 落盘；若目标会话有事件总线（任务已在跑），
        广播 todo:updated 让前端 TODOs 面板 / 任务卡实时更新。
        """
        ctx.check_auth(request)
        snapshot = app.session_store.load(session_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        items = [t.strip() for t in (payload.items or []) if t and t.strip()][:100]
        # seed_board 以「内容 dict 列表」为输入（{content}），字符串列表归一化后传入
        seeded = app.todo_plugin.seed_board(session_id, [{"content": t} for t in items])
        try:
            events = app.todo_plugin._events.get(session_id)
            if events is not None:
                await events.emit("todo:updated", {"todos": app.todo_plugin.get(session_id)})
        except Exception:
            pass
        return {"ok": True, "seeded": seeded}

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

    # ------------------------------------------------------------ 隔离工作树

    @router.post("/api/sessions/{session_id}/worktree")
    async def set_session_worktree(session_id: str, payload: SessionWorktreeRequest, request: Request):
        """开启/关闭会话的隔离工作树（/worktree on|off）。

        开启后，该会话的任务在独立分支 worktree-<sid> + 目录
        .lite-work/worktrees/<sid> 中执行，主工作区不受影响。
        """
        ctx.check_auth(request)
        result = app.set_session_worktree(session_id, bool(payload.enabled))
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("reason", "无法切换隔离工作树"))
        return result

    @router.get("/api/sessions/{session_id}/worktree")
    async def worktree_status(session_id: str, request: Request):
        """worktree 状态：是否启用 + 变更文件/增删行数/分支（评审卡数据源）。"""
        ctx.check_auth(request)
        return app.worktree_status(session_id)

    @router.post("/api/sessions/{session_id}/agents/{agent_id}/close")
    async def close_session_agent(session_id: str, agent_id: str, request: Request):
        """从 Agents 看板手动取消一个后台子 Agent。

        复用 AgentManager.close（取消 runner + 置 closed 终态），并照
        /api/approve 的广播模式向活跃任务 kernel 发 agent:closed（by=user）。
        前端同时做乐观更新——SSE 只在主任务运行时存在，不能依赖事件到达。
        """
        import logging as _logging

        ctx.check_auth(request)
        manager = app.agent_manager(session_id, create=False)
        if manager is None:
            raise HTTPException(status_code=404, detail="会话无活动子 Agent")
        result = await manager.close(agent_id, by="user")
        if not result.get("ok"):
            raise HTTPException(status_code=404, detail=str(result.get("error", "未知 agent")))
        # 广播给所有活跃任务的 kernel（UI 按 agentId + by 匹配更新看板卡片）
        for handle in list(tasks.tasks.values()):
            try:
                await handle.kernel.events.emit(
                    "agent:closed", {"agentId": agent_id, "by": "user"})
            except Exception:
                _logging.getLogger("litework.server").debug(
                    "[Agents] 广播 agent:closed 失败", exc_info=True)
        return result

    @router.get("/api/sessions/{session_id}/worktree/diff")
    async def worktree_diff(session_id: str, request: Request):
        """worktree 改动的人类可读 unified diff（评审卡「查看 diff」）。"""
        ctx.check_auth(request)
        return {"diff": app.worktree_diff(session_id)}

    @router.post("/api/sessions/{session_id}/worktree/merge")
    async def worktree_merge(session_id: str, request: Request):
        """合并 worktree 改动回主工作区（成功清理 worktree + 分支）。"""
        ctx.check_auth(request)
        result = app.worktree_merge(session_id)
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("reason", "合并失败"))
        return result

    @router.post("/api/sessions/{session_id}/worktree/discard")
    async def worktree_discard(session_id: str, request: Request):
        """丢弃 worktree（删目录 + 分支，主工作区毫发无伤）。"""
        ctx.check_auth(request)
        return app.worktree_discard(session_id)

    @router.get("/api/worktrees")
    async def list_worktrees(request: Request):
        """项目级工作树总览：当前分支 + 所有活跃 worktree（含落后状态）。"""
        ctx.check_auth(request)
        ctx.require_workspace()
        return app.worktree_overview()

    @router.post("/api/worktrees/clean")
    async def clean_worktrees(payload: WorktreeCleanRequest, request: Request):
        """清理遗留 worktree：默认只删无改动的；include_dirty=true 全删（可指定单个 name）。"""
        ctx.check_auth(request)
        ctx.require_workspace()
        return app.worktree_clean(include_dirty=bool(payload.include_dirty), name=payload.name)

    @router.post("/api/worktrees/abort-merge")
    async def abort_merge(request: Request):
        """放弃进行中的合并（合并冲突后回退主工作区到合并前状态）。"""
        ctx.check_auth(request)
        ctx.require_workspace()
        result = app.worktree_abort_merge()
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("reason") or "放弃合并失败")
        return result

    @router.get("/api/worktrees/conflicts")
    async def worktree_conflicts(request: Request):
        """主工作区当前合并冲突文件列表（合并中状态）。"""
        ctx.check_auth(request)
        ctx.require_workspace()
        return app.worktree_conflicts()

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

    # ------------------------------------------------------------ 轨迹

    @router.get("/api/trajectories/{session_id}")
    async def list_trajectories(session_id: str, request: Request,
                                task_id: str = "", offset: int = 0, limit: int = 500):
        """列出会话的轨迹任务；指定 task_id 时返回该轨迹的事件（分页）。

        trajectory_enabled=false 时只返回空列表（不报错——轨迹是 opt-in 的）。
        """
        ctx.check_auth(request)
        import re as _re
        from ...core.trajectory import analyze_trajectory, list_session_trajectories, read_trajectory

        # H1 路径穿越修复：session_id/task_id 只允许安全字符（禁止 / \ .. 等）
        # Starlette 的 path param 会解码 %2F，不加校验可读任意 .jsonl / 列任意目录
        _SAFE_ID = _re.compile(r"^[\w\-.]+$")
        if not _SAFE_ID.match(session_id) or (task_id and not _SAFE_ID.match(task_id)):
            raise HTTPException(status_code=400, detail="session_id / task_id 含非法字符")

        if task_id:
            events = read_trajectory(app.config_dir, session_id, task_id,
                                     offset=max(0, offset), limit=max(1, min(limit, 2000)))
            return {
                "session_id": session_id, "task_id": task_id,
                "events": events, "count": len(events),
                "findings": analyze_trajectory(events),
            }
        return {
            "session_id": session_id,
            "trajectories": list_session_trajectories(app.config_dir, session_id),
            "enabled": bool(app.config.get("trajectory_enabled", False)),
        }
    return router