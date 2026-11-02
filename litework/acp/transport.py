# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""ACP（Agent Client Protocol）stdio 传输层。

协议要求（agentclientprotocol.com/protocol/v1/transports）：

- 客户端把 Agent 作为子进程拉起，Agent 从 **stdin** 读 JSON-RPC、
  向 **stdout** 写 JSON-RPC；
- 消息按 ``\\n`` 分隔，单条消息内 **不得** 含内嵌换行；
- Agent **只能** 把合法 ACP 消息写进 stdout，日志一律走 stderr；
- 客户端 **只能** 把合法 ACP 消息写进 stdin。

最后两条是整个实现里最容易踩碎的约束：项目里任何一个插件、工具或第三方库
随手一个 ``print()`` 都会往 stdout 吐一行非 JSON，客户端解析失败就会认为
Agent 崩了——而现象是「连上几秒后断开」，极难与业务 bug 区分。

因此这里的核心不是「发消息」，而是**在进入服务循环前就把 stdout 换成协议
通道**，把真正的进程 stdout 让给 stderr，从根上杜绝误写（见
``StdioTransport.capture_stdout``）。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger("litework.acp")

#: JSON-RPC 2.0 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

#: ACP 自定义错误码（client 侧有专门处理，例如 auth_required 触发登录流程）
AUTH_REQUIRED = -32000
RESOURCE_NOT_FOUND = -32002

Handler = Callable[[Dict[str, Any]], Awaitable[Any]]
NotificationHandler = Callable[[Dict[str, Any]], Awaitable[None]]


class RpcError(Exception):
    """处理失败 → JSON-RPC error 响应。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class StdioTransport:
    """按行分隔的 JSON-RPC over stdio。

    读与写共用一个事件循环：``pump()`` 负责逐行读 stdin 并分发，写操作
    用互斥锁串行化——ACP 不要求响应顺序，但**单条消息必须完整写出**，
    交错写会产生被截断的 JSON 行。
    """

    def __init__(self, reader: Optional[asyncio.StreamReader] = None,
                 writer: Optional[Any] = None) -> None:
        self._reader = reader
        self._writer = writer
        self._write_lock = asyncio.Lock()
        self._handlers: Dict[str, Handler] = {}
        self._notification_handlers: Dict[str, NotificationHandler] = {}
        self._closed = False
        #: 已发出的请求 id → Future（Agent 主动向 client 发请求时用，如
        #: session/request_permission）。stdio 下 Agent↔Client 是双向的。
        self._pending_requests: Dict[Any, asyncio.Future] = {}
        self._next_id = 0

    # ------------------------------------------------------------ 装配

    async def use_standard_streams(self) -> None:
        """把 stdin 接到协议通道上（真正的 stdout 已改道 stderr）。

        ``connect_read_pipe`` 是协程，必须在运行中的事件循环里 await——
        用 ``run_until_complete`` 会抛 "This event loop is already running"，
        再退回线程读取虽然能用，但那段协程就成了「创建了却从未 await」的
        悬空对象（stderr 里一串 RuntimeWarning）。

        Windows 的 ProactorEventLoop 不支持 connect_read_pipe，那里**确实**
        退回线程读取（见 ``_iterate_messages``）。
        """
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        try:
            protocol = asyncio.StreamReaderProtocol(reader)
            await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
            self._reader = reader
        except (NotImplementedError, ValueError, AttributeError) as exc:
            logger.debug("[ACP] stdin 管道不可用（%s），退回线程读取", exc)
            self._reader = None
        # writer 由 capture_stdout 装配（它持有 dup 出来的真 stdout）

    @contextlib.contextmanager
    def capture_stdout(self):
        """进入协议模式：真正的 stdout 让给协议，其余输出改道 stderr。

        这是本模块唯一真正「防事故」的地方，必须在任何插件/工具初始化
        **之前**调用——插件 import 期就可能 print。
        """
        proto_fd = os.dup(1)          # 复制真 stdout，专门用来发协议消息
        os.dup2(2, 1)                 # 原 stdout 位置改成 stderr
        proto_stream = os.fdopen(proto_fd, "w", encoding="utf-8", newline="\n")
        old_stdout = sys.stdout
        sys.stdout = sys.stderr       # 让 Python 层的 print 也落到 stderr
        # 协议通道在这里才真正接上：writer 指向 dup 出来的真 stdout
        self._writer = _StdoutWriter(proto_stream)
        try:
            yield proto_stream
        finally:
            sys.stdout = old_stdout
            with contextlib.suppress(Exception):
                proto_stream.flush()
                proto_stream.close()

    # ------------------------------------------------------------ 注册

    def on_request(self, method: str, handler: Handler) -> None:
        self._handlers[method] = handler

    def on_notification(self, method: str, handler: NotificationHandler) -> None:
        self._notification_handlers[method] = handler

    # ------------------------------------------------------------ 写

    async def send_notification(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._write(payload)

    async def send_result(self, req_id: Any, result: Any) -> None:
        await self._write({"jsonrpc": "2.0", "id": req_id, "result": result})

    async def send_error(self, req_id: Any, code: int, message: str,
                         data: Any = None) -> None:
        err: Dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        await self._write({"jsonrpc": "2.0", "id": req_id, "error": err})

    async def request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Agent → Client 的反向请求（如权限审批），等待 client 响应。"""
        self._next_id += 1
        req_id = f"litework-{self._next_id}"
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_requests[req_id] = fut
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        await self._write(payload)
        try:
            return await fut
        finally:
            self._pending_requests.pop(req_id, None)

    async def _write(self, payload: Dict[str, Any]) -> None:
        if self._writer is None:
            raise RpcError(INTERNAL_ERROR, "传输层未初始化")
        # ensure_ascii=False：中文内容直接输出 UTF-8（协议要求 UTF-8 编码）
        # separators 去掉多余空格；line 内绝不能有换行——json.dumps 天然满足，
        # 但字符串值里若有 U+2028/U+2029 不影响（它们不是 \n）
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        async with self._write_lock:
            self._writer.write(line + "\n")
            flush = getattr(self._writer, "flush", None)
            if flush is not None:
                flush()

    # ------------------------------------------------------------ 读

    async def _iterate_messages(self):
        """逐条产出解析后的 JSON-RPC 消息 dict。"""
        if self._reader is not None:
            while True:
                try:
                    raw = await self._reader.readline()
                except (asyncio.IncompleteReadError, ConnectionResetError):
                    return
                if not raw:
                    return
                msg = self._decode(raw)
                if msg is not None:
                    yield msg
        # 线程读取兜底（Windows）：不能阻塞事件循环
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def _reader_thread() -> None:
            try:
                for raw in sys.stdin.buffer:
                    loop.call_soon_threadsafe(queue.put_nowait, raw)
            except Exception:
                pass
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        import threading
        threading.Thread(target=_reader_thread, daemon=True, name="acp-stdin").start()
        while True:
            raw = await queue.get()
            if raw is None:
                return
            msg = self._decode(raw)
            if msg is not None:
                yield msg

    def _decode(self, raw: bytes) -> Optional[Dict[str, Any]]:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            return None
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            # 无法解析 → 回 PARSE_ERROR（id 未知，用 null）
            asyncio.ensure_future(
                self.send_error(None, PARSE_ERROR, "JSON 解析失败"))
            logger.warning("[ACP] 收到无法解析的行：%.200s", line)
            return None
        if not isinstance(msg, dict):
            asyncio.ensure_future(
                self.send_error(None, INVALID_REQUEST, "消息必须是 JSON 对象"))
            return None
        return msg

    # ------------------------------------------------------------ 主循环

    async def pump(self) -> None:
        """读循环：分发请求/通知，处理 client 对本 Agent 请求的响应。"""
        async for msg in self._iterate_messages():
            method = msg.get("method")
            req_id = msg.get("id")
            # client 对「Agent 发出的请求」的响应：没有 method，只有 id
            if method is None:
                if req_id in self._pending_requests:
                    fut = self._pending_requests[req_id]
                    if not fut.done():
                        if "error" in msg:
                            fut.set_exception(RpcError(
                                msg["error"].get("code", INTERNAL_ERROR),
                                msg["error"].get("message", "client 返回错误")))
                        else:
                            fut.set_result(msg.get("result"))
                continue
            if req_id is None:
                await self._dispatch_notification(method, msg.get("params") or {})
            else:
                # 每个请求独立 task：session/prompt 可能跑几分钟，
                # 阻塞读循环会让 cancel 通知永远收不到（无法取消 = 卡死）
                asyncio.ensure_future(self._dispatch_request(req_id, method,
                                                             msg.get("params") or {}))
        # stdin EOF：client 关闭 → 退出
        self._closed = True
        for fut in list(self._pending_requests.values()):
            if not fut.done():
                fut.cancel()

    async def _dispatch_request(self, req_id: Any, method: str,
                                params: Dict[str, Any]) -> None:
        handler = self._handlers.get(method)
        if handler is None:
            await self.send_error(req_id, METHOD_NOT_FOUND, f"未实现的方法：{method}")
            return
        try:
            result = await handler(params)
            await self.send_result(req_id, result)
        except RpcError as exc:
            await self.send_error(req_id, exc.code, exc.message, exc.data)
        except asyncio.CancelledError:
            # 被取消的请求不必回error（client 已知自己是取消方），但要让它
            # 的 await 结束，否则上层会一直等
            await self.send_error(req_id, INTERNAL_ERROR, "请求已取消")
            raise
        except Exception as exc:  # noqa: BLE001 —— 任何处理异常都不能打死读循环
            logger.exception("[ACP] 处理 %s 失败", method)
            await self.send_error(req_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

    async def _dispatch_notification(self, method: str, params: Dict[str, Any]) -> None:
        handler = self._notification_handlers.get(method)
        if handler is None:
            logger.debug("[ACP] 忽略未实现的通知：%s", method)
            return
        try:
            await handler(params)
        except Exception:  # noqa: BLE001
            logger.exception("[ACP] 处理通知 %s 失败", method)


class _StdoutWriter:
    """协议输出包装：底层是 dup 出来的真 stdout fd。

    单独一层是为了让 ``_write`` 的 write/flush 有明确语义，并方便测试注入
    内存流（见 tests/test_acp.py）。
    """

    def __init__(self, stream: Optional[Any] = None) -> None:
        self._stream = stream

    def write(self, data: str) -> None:
        stream = self._stream
        if stream is None:
            raise RpcError(INTERNAL_ERROR, "协议输出流未就绪")
        stream.write(data)

    def flush(self) -> None:
        stream = self._stream
        if stream is not None and hasattr(stream, "flush"):
            stream.flush()
