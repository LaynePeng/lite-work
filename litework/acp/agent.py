# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""ACP（Agent Client Protocol）Agent 端实现。

让 lite-work 作为 **Agent** 被编辑器（Zed / JetBrains / Neovim 等 ACP 客户端）
直接驱动：客户端下发 ``session/prompt``，本模块把 lite-work 的 AgentLoop 事件
翻译成 ``session/update`` 通知流回去。

设计取舍（为什么不是「再包一层 HTTP」）：

- ACP 走 stdio，客户端把 Agent 当子进程；因此本模块直接持有 AgentLoop，
  不经过 server/FastAPI。多一层 HTTP 会引入端口、鉴权、进程生命周期三份
  复杂度，却换不到任何东西；
- 一 turn 一 task：``session/prompt`` 必须等整个 turn 结束才返回 result，
  中途靠 ``session/update`` 推流。因此 prompt 处理函数是长时间运行的，
  传输层必须允许它被 ``session/cancel`` 打断（见 transport 的独立 task 派发）；
- 只做「能跑通主流客户端」的最小闭环：initialize / session-new / prompt /
  cancel + 流式文本 + 工具调用状态。session/load、slash commands、
  terminal 等能力**显式不声明**，客户端据此不会调用（协议要求 client 先查
  capabilities）。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, List, Optional

from .transport import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    RpcError,
    StdioTransport,
)

logger = logging.getLogger("litework.acp")

#: 本 Agent 声明的协议版本（ACP v1）
PROTOCOL_VERSION = 1


def _text_block(text: str) -> Dict[str, Any]:
    return {"type": "text", "text": text}


def replay_updates(messages: List[Any]) -> List[Dict[str, Any]]:
    """内部 Message 列表 → ACP session/update 序列。

    映射规则（刻意保守）：

    - ``user`` → ``user_message_chunk``
    - ``assistant`` 且有文本 → ``agent_message_chunk``
    - ``assistant`` 的工具调用 → ``tool_call`` + ``tool_call_update``
      （status=completed，紧跟其 ``tool`` 结果）
    - ``tool`` 消息本身不单独产出更新（已并入上一条 tool_call_update），
      避免客户端出现「有结果没有调用」的孤立条目
    - ``system`` 跳过：ACP 没有对应概念，塞进去只会污染对话

    工具结果只重放**成对存在**的调用：历史里若有悬空 tool_calls（旧版本
    中断遗留），跳过它比推一个永远 completed 不了的卡片更干净。
    """
    results: Dict[str, str] = {}
    for msg in messages:
        role = _role_of(msg)
        if role == "tool":
            call_id = _attr(msg, "tool_call_id")
            if call_id:
                results[str(call_id)] = _attr(msg, "content") or ""

    updates: List[Dict[str, Any]] = []
    for msg in messages:
        role = _role_of(msg)
        if role == "user":
            content = _attr(msg, "content")
            if content:
                updates.append({
                    "sessionUpdate": "user_message_chunk",
                    "content": _text_block(content),
                })
        elif role == "assistant":
            content = _attr(msg, "content")
            if content:
                updates.append({
                    "sessionUpdate": "agent_message_chunk",
                    "content": _text_block(content),
                })
            for call in (_attr(msg, "tool_calls") or []):
                call_id = str(_attr(call, "id") or "")
                if not call_id:
                    continue
                fn = _attr(call, "function") or {}
                name = str(_attr(fn, "name") or "tool")
                args_raw = _attr(fn, "arguments")
                args: Any = args_raw
                if isinstance(args_raw, str):
                    try:
                        import json as _json
                        args = _json.loads(args_raw)
                    except (ValueError, TypeError):
                        args = args_raw
                updates.append({
                    "sessionUpdate": "tool_call",
                    "toolCallId": call_id,
                    "title": f"{name}",
                    "name": name,
                    "kind": "other",
                    "status": "completed",
                    "rawInput": args if isinstance(args, dict) else {},
                })
                output = results.get(call_id, "")
                updates.append({
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": call_id,
                    "status": "completed",
                    "content": [{"type": "content",
                                 "content": _text_block(output[:4000])}],
                })
    return updates


def _role_of(msg: Any) -> str:
    if isinstance(msg, dict):
        return str(msg.get("role") or "")
    return str(getattr(msg, "role", "") or "")


def _attr(obj: Any, name: str) -> Any:
    """同时支持 dict（新落盘）与对象（内存里的 Message）两种形态。"""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


class AcpSession:
    """一个 ACP 会话 = 一个 lite-work session + 一个可选运行中的 turn。"""

    def __init__(self, session_id: str, cwd: str) -> None:
        self.session_id = session_id
        self.cwd = cwd
        self.turn_task: Optional[asyncio.Task] = None
        self.cancelled = False


class AcpAgent:
    """ACP 方法处理器集合（与传输层解耦，便于单测直接调用方法）。"""

    def __init__(self, transport: StdioTransport, loop_factory, history_loader=None,
                 permission_relay=None) -> None:
        """
        loop_factory: ``(session_id, cwd) -> AgentLoop``。由 CLI 注入，避免
        本模块反向依赖 app/agent_loop 的具体装配方式（测试里可传假实现）。
        history_loader: ``(session_id) -> List[Message]``，供 session/load 重放。
        为 None 表示不支持会话加载（此时 initialize 不声明 loadSession）。
        permission_relay: ``(approval_id, approved) -> None``，把用户在编辑器里的
        决定写回 ApprovalGate。为 None 时不挂审批桥（写操作会被自己的审批门
        拦住直到超时，因此生产入口必须传）。
        """
        self.transport = transport
        self._loop_factory = loop_factory
        self._history_loader = history_loader
        self._permission_relay = permission_relay
        self._sessions: Dict[str, AcpSession] = {}
        self._client_capabilities: Dict[str, Any] = {}

    # ------------------------------------------------------------ 注册

    def register(self) -> None:
        self.transport.on_request("initialize", self.initialize)
        self.transport.on_request("session/new", self.session_new)
        self.transport.on_request("session/load", self.session_load)
        self.transport.on_request("session/prompt", self.session_prompt)
        self.transport.on_notification("session/cancel", self.session_cancel)

    # ------------------------------------------------------------ initialize

    async def initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """握手：声明协议版本与能力。

        ``agentCapabilities`` 里没声明的能力，合规客户端不会调用。这里只声明
        我们真做完的：prompt 流式（文本 + 工具调用）与可选的会话加载。

        ACP 的 history replay 要求把整段对话按顺序推成 session/update 流，
        包括用户消息（user_message_chunk）与助手消息（agent_message_chunk）。
        工具调用只重放 status=completed 的结果，不重放 pending 中间态——
        重放一个「正在运行」的旧工具会让客户端永远转圈。
        """
        self._client_capabilities = params.get("clientCapabilities") or {}
        peer_version = params.get("protocolVersion")
        if isinstance(peer_version, int) and peer_version != PROTOCOL_VERSION:
            logger.warning(
                "[ACP] 客户端协议版本 %s 与本端 %s 不同，按 v1 处理",
                peer_version, PROTOCOL_VERSION,
            )
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "agentCapabilities": {
                # 支持 prompt 期间的流式更新（文本块 + 工具调用状态）
                "promptCapabilities": {
                    "image": False,
                    "audio": False,
                    "embeddedContext": False,
                },
                # 有 history_loader 才声明：见方法注释
                "loadSession": self._history_loader is not None,
                "mcpCapabilities": {"http": False, "sse": False},
            },
            "authMethods": [],
        }

    # ------------------------------------------------------------ session/new

    async def session_new(self, params: Dict[str, Any]) -> Dict[str, Any]:
        cwd = params.get("cwd") or ""
        if not cwd:
            # 协议要求 cwd 必填；缺了就退到进程当前目录，而不是直接失败
            import os
            cwd = os.getcwd()
        session_id = f"acp_{uuid.uuid4().hex[:12]}"
        self._sessions[session_id] = AcpSession(session_id, cwd)
        logger.info("[ACP] 新建会话 %s（cwd=%s）", session_id, cwd)
        return {"sessionId": session_id}

    # ------------------------------------------------------------ session/load

    async def session_load(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """恢复已有会话：先整段重放历史，再响应请求。

        协议要求「等全部历史都推完才回 result」（client 据此认为恢复完成），
        因此这里顺序 await，不用并发推送——并发会让消息顺序错乱，
        而对话顺序是这段历史唯一有价值的信息。
        """
        if self._history_loader is None:
            raise RpcError(METHOD_NOT_FOUND, "本端不支持 session/load")
        session_id = params.get("sessionId") or ""
        if not session_id:
            raise RpcError(INVALID_PARAMS, "缺少 sessionId")
        cwd = params.get("cwd") or ""
        self._sessions[session_id] = AcpSession(session_id, cwd)
        try:
            messages = self._history_loader(session_id) or []
        except Exception:  # noqa: BLE001
            logger.exception("[ACP] 读取会话历史失败：%s", session_id)
            messages = []
        for update in replay_updates(messages):
            await self._emit(session_id, update)
        logger.info("[ACP] 会话 %s 已重放 %d 条历史", session_id, len(messages))
        return {}

    # ------------------------------------------------------------ session/prompt

    async def session_prompt(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """跑一个完整 turn，流式推送 session/update，结束时返回 stopReason。"""
        session_id = params.get("sessionId") or ""
        session = self._sessions.get(session_id)
        if session is None:
            raise RpcError(METHOD_NOT_FOUND, f"未知会话：{session_id}")

        prompt_text = self._flatten_prompt(params.get("prompt") or [])
        if not prompt_text.strip():
            # 空 prompt 不报错（客户端可能发纯图片等我们不支持的类型），
            # 直接结束——报错会让编辑器弹红框，体验更差
            return {"stopReason": "end_turn"}

        session.cancelled = False
        # 前一个 turn 还在跑：按协议应排队，但 ACP 客户端自身保证串行，
        # 真撞上了说明客户端有 bug，这里直接拒绝而不是静默并发
        if session.turn_task is not None and not session.turn_task.done():
            raise RpcError(INTERNAL_ERROR, "该会话已有 turn 在运行")

        session.turn_task = asyncio.current_task()
        bridges = []
        try:
            agent_loop = self._loop_factory(session_id, session.cwd)
            # 工具调用可视化 + 审批：都挂在本次 turn 的 kernel 上，turn 结束即卸载
            # （kernel 每 turn 新建，事件天然属于当前会话）。
            bridges = self._attach_bridges(agent_loop, session_id)
            await self._emit(session_id, {
                "sessionUpdate": "agent_message_chunk",
                "content": _text_block(""),
            })
            text, _stats = await agent_loop.run_task(prompt_text)
            if text:
                await self._emit(session_id, {
                    "sessionUpdate": "agent_message_chunk",
                    "content": _text_block(text),
                })
            return {"stopReason": "cancelled" if session.cancelled else "end_turn"}
        except asyncio.CancelledError:
            # session/cancel 触发的 task.cancel() 会以 CancelledError 冒到这里。
            # 协议要求 prompt **正常返回** {stopReason: "cancelled"}，客户端
            # 据此收尾；若把异常抛出去，传输层会回 JSON-RPC error，编辑器
            # 会把「用户主动停止」显示成一次失败。
            logger.info("[ACP] 会话 %s 的 turn 被取消，按协议返回 cancelled", session_id)
            return {"stopReason": "cancelled"}
        finally:
            for bridge in bridges:
                try:
                    bridge.detach()
                except Exception:  # noqa: BLE001
                    pass
            session.turn_task = None

    def _attach_bridges(self, agent_loop: Any, session_id: str) -> List[Any]:
        """挂载工具展示桥与审批桥。两者都可选——缺配置时不影响 prompt 主流程。"""
        from .permissions import PermissionBridge
        from .tools import ToolUpdateBridge

        kernel = getattr(agent_loop, "kernel", None)
        if kernel is None:
            return []
        attached: List[Any] = []
        try:
            tools_bridge = ToolUpdateBridge(self._emit, session_id)
            tools_bridge.attach(kernel)
            attached.append(tools_bridge)
        except Exception:  # noqa: BLE001
            logger.debug("[ACP] 工具事件桥挂载失败", exc_info=True)
        if self._permission_relay is not None:
            try:
                perm_bridge = PermissionBridge(
                    self.transport, self._permission_relay, session_id)
                perm_bridge.attach(kernel)
                attached.append(perm_bridge)
            except Exception:  # noqa: BLE001
                logger.debug("[ACP] 审批桥挂载失败", exc_info=True)
        return attached

    # ------------------------------------------------------------ session/cancel

    async def session_cancel(self, params: Dict[str, Any]) -> None:
        """取消运行中的 turn（通知，无响应）。

        协议语义：client 发 cancel 后仍要等 ``session/prompt`` 的 result
        （stopReason=cancelled），因此这里只置标志 + 取消 task，
        由 prompt 侧返回。
        """
        session_id = params.get("sessionId") or ""
        session = self._sessions.get(session_id)
        if session is None:
            return
        session.cancelled = True
        task = session.turn_task
        if task is not None and not task.done():
            task.cancel()
            logger.info("[ACP] 会话 %s 的 turn 已取消", session_id)

    # ------------------------------------------------------------ 内部

    async def _emit(self, session_id: str, update: Dict[str, Any]) -> None:
        await self.transport.send_notification(
            "session/update",
            {"sessionId": session_id, "update": update},
        )

    @staticmethod
    def _flatten_prompt(blocks: List[Dict[str, Any]]) -> str:
        """ACP content block 列表 → 单个提示串。

        只取 text 块：image/audio/resource 未声明支持，静默跳过比报错好
        （客户端可能习惯性附带上下文，整体失败会让用户莫名其妙）。
        """
        parts: List[str] = []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        return "\n\n".join(parts)


__all__ = ["AcpAgent", "AcpSession", "PROTOCOL_VERSION"]
