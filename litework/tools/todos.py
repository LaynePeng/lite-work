# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""任务 TODO 清单工具：超长/多步骤任务的规划与进度追踪。

Agent 通过 `todo_write` 全量维护清单（对齐 Claude Code TodoWrite 模式），
每次调用提交完整列表并触发 `todo:updated` 事件实时推送到前端「TODOs」面板。
TaskManager 在任务启动时把会话事件总线绑定到看板、结束时解绑。

子 Agent 合并：子 Agent 的 kernel 带 root_session_id（指向主会话），agent_loop
把该值转发到 current_root_session_id ContextVar；todo_write 以根会话为「目标会话」
写看板 + 发事件，实现子 Agent 进度合并进主会话 TODO 面板（v1.0.1 修复）。

看板持久化：每次 todo_write 同步落盘到 `<config_dir>/todo_boards/<session>.json`，
页面刷新/服务重启后经 GET /api/todos 读取恢复（内存命中优先，未命中回退磁盘）。
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from ..core.types import ToolDefinition
from .plugin import ToolPlugin

logger = logging.getLogger("litework.tools.todos")

# 当前任务所属会话（agent_loop.run_task 启动时设置；工具处理器运行时读取）
current_session_id: contextvars.ContextVar = contextvars.ContextVar(
    "current_session_id", default=""
)

# 当前任务归属的根会话：子 Agent 执行时 = 主会话 id（sub_agent 设置 kernel.root_session_id，
# agent_loop.run_task 转发到此处）；主 Agent 执行时 = 自身会话 id。
# todo_write 以此为目标写看板 + 发事件，实现子 Agent 进度合并进主会话 TODO 面板。
current_root_session_id: contextvars.ContextVar = contextvars.ContextVar(
    "current_root_session_id", default=""
)

VALID_STATUSES = ("pending", "in_progress", "completed")
MAX_TODOS = 100

_STATUS_MARKS = {"pending": "☐", "in_progress": "▶", "completed": "✔"}

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _render_board(todos: List[Dict[str, Any]]) -> str:
    return "\n".join(f"{_STATUS_MARKS.get(t['status'], '☐')} {t['content']}" for t in todos)


class TodoPlugin(ToolPlugin):
    """todo_write 工具：任务级 TODO 看板（校验 + 存储 + 事件推送 + 持久化）。"""

    name = "todo-plugin"
    version = "1.0.1"

    def __init__(self, storage_dir: Optional[str] = None) -> None:
        self._items: Dict[str, List[Dict[str, Any]]] = {}
        self._events: Dict[str, Any] = {}  # session_id -> EventBus
        self.storage_dir = storage_dir  # None 时不落盘（纯内存，测试用）

    # ------------------------------------------------------------ 持久化

    def _board_path(self, session_id: str) -> Optional[str]:
        if not self.storage_dir or not session_id:
            return None
        safe = _SAFE_FILENAME_RE.sub("_", session_id)[:80]
        if not safe or safe in (".", ".."):
            safe = hashlib.sha1(session_id.encode("utf-8")).hexdigest()
        return os.path.join(self.storage_dir, f"{safe}.json")

    def _persist(self, session_id: str, todos: List[Dict[str, Any]]) -> None:
        path = self._board_path(session_id)
        if path is None:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"session_id": session_id, "updated_at": int(time.time() * 1000),
                           "todos": todos}, f, ensure_ascii=False)
            os.replace(tmp, path)  # 原子替换，避免写一半损坏
        except OSError:
            logger.exception("[TodoPlugin] 看板落盘失败: %s", session_id)

    def _load_disk(self, session_id: str) -> List[Dict[str, Any]]:
        path = self._board_path(session_id)
        if path is None or not os.path.isfile(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            todos = data.get("todos") if isinstance(data, dict) else None
            if not isinstance(todos, list):
                return []
            return [t for t in todos if isinstance(t, dict)
                    and t.get("content") and t.get("status") in VALID_STATUSES]
        except (OSError, ValueError):
            logger.exception("[TodoPlugin] 看板读取失败: %s", session_id)
            return []

    def delete_board(self, session_id: str) -> None:
        """删除会话看板（内存 + 磁盘，会话删除时调用）。"""
        self._items.pop(session_id, None)
        self._events.pop(session_id, None)
        path = self._board_path(session_id)
        if path and os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                logger.exception("[TodoPlugin] 看板删除失败: %s", session_id)

    # ------------------------------------------------------------ 生命周期

    def bind(self, session_id: str, events: Any) -> None:
        """任务启动时绑定会话事件总线（TaskManager 调用）；内存未命中时从磁盘恢复。"""
        self._events[session_id] = events
        if session_id not in self._items:
            disk = self._load_disk(session_id)
            if disk:
                self._items[session_id] = disk

    def unbind(self, session_id: str) -> None:
        self._events.pop(session_id, None)

    def get(self, session_id: str) -> List[Dict[str, Any]]:
        """读取会话当前清单：内存命中优先，未命中回退磁盘（刷新/重启后恢复）。"""
        if session_id in self._items:
            return list(self._items[session_id])
        disk = self._load_disk(session_id)
        if disk:
            self._items[session_id] = disk
        return list(disk)

    # ------------------------------------------------------------ 工具

    def get_tools(self) -> List[ToolDefinition]:
        return [ToolDefinition(
            name="todo_write",
            description=(
                "维护当前任务的 TODO 清单（多步骤/超长任务必用，琐碎单步任务不要用）。"
                "每次调用提交完整清单（全量覆盖，不是增量追加）。使用规则："
                "①接到复杂任务先列计划再动手，每项是一个可验证的小步骤；"
                "②同一时间只允许一项 in_progress；"
                "③开始某项前置为 in_progress，完成后立即置为 completed；"
                "④过程中发现新的工作项及时补进清单；"
                "⑤全部完成后所有项应为 completed。"
                "用户可在右侧 TODOs 面板实时看到清单与进度。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": "完整的 TODO 列表（全量覆盖）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string", "description": "待办事项描述"},
                                "status": {
                                    "type": "string",
                                    "enum": list(VALID_STATUSES),
                                    "description": "pending(待办) / in_progress(进行中) / completed(已完成)",
                                },
                            },
                            "required": ["content", "status"],
                        },
                    },
                },
                "required": ["todos"],
            },
        )]

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        session_id = current_session_id.get("")
        if not session_id:
            return "[Error] 当前没有活动会话，无法维护 TODO"
        # 子 Agent 的 todo_write 合并进主会话看板（经 root_session_id），
        # 事件也发往主会话总线 → 前端 TODOs 面板实时更新。
        target = current_root_session_id.get("") or session_id
        todos = args.get("todos")
        if not isinstance(todos, list):
            return "[Error] todos 必须是数组，且每次提交完整清单（全量覆盖）"
        if len(todos) > MAX_TODOS:
            return f"[Error] TODO 数量超过上限 {MAX_TODOS}"
        cleaned: List[Dict[str, Any]] = []
        for i, item in enumerate(todos):
            if not isinstance(item, dict):
                return f"[Error] 第 {i + 1} 项必须是对象（content/status）"
            content = str(item.get("content", "")).strip()
            status = str(item.get("status", "pending")).strip()
            if not content:
                return f"[Error] 第 {i + 1} 项 content 不能为空"
            if status not in VALID_STATUSES:
                return (f"[Error] 第 {i + 1} 项 status 非法: {status!r}"
                        f"（允许: {', '.join(VALID_STATUSES)}）")
            cleaned.append({"content": content, "status": status})

        self._items[target] = cleaned
        self._persist(target, cleaned)
        events = self._events.get(target)
        if events is not None:
            try:
                await events.emit("todo:updated", {"todos": list(cleaned)})
            except Exception:
                pass  # 事件推送失败不影响工具结果

        done = sum(1 for t in cleaned if t["status"] == "completed")
        doing = sum(1 for t in cleaned if t["status"] == "in_progress")
        header = f"TODO 清单已更新：共 {len(cleaned)} 项（✔{done} ▶{doing} ☐{len(cleaned) - done - doing}）"
        return f"{header}\n{_render_board(cleaned)}"
