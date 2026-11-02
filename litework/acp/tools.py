# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""把 lite-work 的工具/审批事件翻译成 ACP 的 session/update。

为什么要单独一层：ACP 的工具展示协议（``tool_call`` / ``tool_call_update``）
与内部事件模型并不一一对应——

- 内部 ``tool:before_execute`` 只有工具名与参数，没有「这个操作在编辑器里
  该显示成什么图标」；ACP 要求 ``kind``（read/edit/execute/...）与
  ``title``（人能读的一句话），必须由工具名推断；
- 内部 ``tool:after_execute`` 才有结果，且 ``status`` 是内部术语
  （ok/error/...），ACP 要 ``completed`` / ``failed``；
- 审批是**反向请求**（Agent → Client），不是通知，不能在事件订阅里直接
  await 完再发——它必须挂起当前工具执行直到用户点选。

因此这里做两件事：纯函数映射（``kind_of_tool`` 等）+ 事件订阅胶水
（``ToolUpdateBridge``）。纯函数抽出来才能把映射规则钉死在测试里。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("litework.acp")

#: 工具名 → ACP ToolKind。顺序敏感：先匹配更具体的。
#: 用子串匹配而非精确表——插件工具名千变万化（read_file / fs_read / cat_file），
#: 精确表必然会漏，漏了只是图标不对（fallback other），代价远小于维护一张全表。
_KIND_RULES: List[tuple[tuple[str, ...], str]] = [
    # think 放最前：todo_write / plan_save 这类名字里带 write 但是「规划」
    # 语义，落到 edit 会让编辑器显示成「正在改代码」，误导用户。
    (("todo", "think", "plan", "reason"), "think"),
    (("delete", "remove", "unlink", "rm_"), "delete"),
    (("move", "rename"), "move"),
    (("write", "edit", "create", "patch", "replace", "append"), "edit"),
    (("read", "cat", "view", "open_file", "list_dir"), "read"),
    (("grep", "search", "find", "glob", "query"), "search"),
    (("bash", "shell", "exec", "run", "command", "terminal", "python"), "execute"),
    (("fetch", "http", "web", "url", "download"), "fetch"),
]


def kind_of_tool(tool_name: str) -> str:
    """推断 ACP ToolKind（客户端据此选图标与展示方式）。

    认不出来就返回 ``other``——这是协议给的默认值，比猜错好。
    """
    name = (tool_name or "").lower()
    if not name:
        return "other"
    for needles, kind in _KIND_RULES:
        if any(n in name for n in needles):
            return kind
    return "other"


def title_of_tool(tool_name: str, args: Optional[Dict[str, Any]] = None) -> str:
    """生成人能读的一句话标题。

    优先带上最关键的那个参数（路径/命令/查询词），否则只显示工具名——
    ACP 客户端会把 title 直接显示在工具卡片上，光一个函数名信息量太少。
    """
    args = args or {}
    key = ""
    for field in ("filePath", "file_path", "path", "filename", "command",
                  "query", "pattern", "url", "name"):
        value = args.get(field)
        if isinstance(value, str) and value.strip():
            key = value.strip()
            break
    base = tool_name or "tool"
    if not key:
        return base
    # 长路径截断：编辑器面板窄，标题过长会被截成省略号，不如自己控制
    if len(key) > 60:
        key = "…" + key[-59:]
    return f"{base}: {key}"


def content_of_result(result: str, limit: int = 4000) -> List[Dict[str, Any]]:
    """把工具结果包成 ACP 的 content 数组。

    截断到 ``limit``：工具结果动辄几万字符（读文件、跑测试），全量塞进
    session/update 会让编辑器卡顿，而用户真正要看的通常只是开头。
    完整结果在 lite-work 自己的会话记录里，不受影响。
    """
    text = result or ""
    truncated = len(text) > limit
    if truncated:
        text = text[:limit] + f"\n…（已截断，完整结果 {len(result)} 字符）"
    return [{"type": "content", "content": {"type": "text", "text": text}}]


#: 内部 status → ACP ToolCallStatus
_STATUS_MAP = {
    "ok": "completed",
    "success": "completed",
    "completed": "completed",
    "error": "failed",
    "failed": "failed",
    "timeout": "failed",
    "denied": "failed",
    "blocked": "failed",
    # cancelled = 被安全策略/用户拒绝拦住。ACP 没有 "cancelled" 状态，
    # 映射到 failed 而不是 completed —— 显示成「成功完成」会让用户误以为
    # 操作真的执行了（实际它被审批门拒绝了）。
    "cancelled": "failed",
}


def status_of_result(status: str) -> str:
    """内部执行状态 → ACP 状态。

    关键：``cancelled``（被拒绝/安全拦截）必须显示为 failed。此前一律
    fallback completed，导致「用户拒绝删除」在编辑器里显示成「删除已完成」，
    是危险的误导。
    """
    return _STATUS_MAP.get((status or "").lower(), "completed")


class ToolUpdateBridge:
    """订阅 kernel 事件，翻译成 ACP session/update。

    只在 prompt turn 期间挂载：ACP 的 session/update 必须带 sessionId，
    而 kernel 是每个 turn 新建的，事件天然属于当前会话。
    """

    def __init__(self, emit, session_id: str) -> None:
        """
        emit: ``async (session_id, update) -> None``（AcpAgent._emit）
        """
        self._emit = emit
        self._session_id = session_id
        self._detached = False

    def attach(self, kernel: Any) -> None:
        events = getattr(kernel, "events", None)
        if events is None:
            return
        events.on("tool:before_execute", self._on_before)
        events.on("tool:after_execute", self._on_after)

    def _on_before(self, payload: Optional[Dict[str, Any]]) -> None:
        if self._detached:
            return
        p = payload or {}
        call_id = str(p.get("callId") or "")
        if not call_id:
            return
        name = str(p.get("toolName") or "")
        args = p.get("args") if isinstance(p.get("args"), dict) else {}
        self._fire({
            "sessionUpdate": "tool_call",
            "toolCallId": call_id,
            "title": title_of_tool(name, args),
            "name": name,
            "kind": kind_of_tool(name),
            "status": "in_progress",
            "rawInput": args,
        })

    def _on_after(self, payload: Optional[Dict[str, Any]]) -> None:
        if self._detached:
            return
        p = payload or {}
        call_id = str(p.get("callId") or "")
        if not call_id:
            return
        result = p.get("result")
        text = result if isinstance(result, str) else ""
        self._fire({
            "sessionUpdate": "tool_call_update",
            "toolCallId": call_id,
            "status": status_of_result(str(p.get("status") or "")),
            "content": content_of_result(text),
        })

    def _fire(self, update: Dict[str, Any]) -> None:
        """事件回调是同步的，发送是异步的 → 丢进事件循环，不阻塞工具执行。"""
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._safe_emit(update))

    async def _safe_emit(self, update: Dict[str, Any]) -> None:
        try:
            await self._emit(self._session_id, update)
        except Exception:  # noqa: BLE001
            # 推送失败绝不能让工具执行失败——编辑器断开是客户端的事
            logger.debug("[ACP] 推送 tool 更新失败", exc_info=True)

    def detach(self) -> None:
        self._detached = True
