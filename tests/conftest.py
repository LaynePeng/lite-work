# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""共享测试工具：Mock LLM 适配器 + 真实 live server 启动器。"""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager

import httpx
import uvicorn

# 测试套件禁用引擎预装：大量 AgentApp 实例会并发拖起 npm install，
# 拖垮测试环境（live server 超时）。预装逻辑由专项测试覆盖。
os.environ.setdefault("LITEWORK_SKIP_ENGINE_PREINSTALL", "1")

import pytest
from typing import AsyncIterator, List, Optional, Tuple
from litework.core.events import TypedEventBus
from litework.core.types import Message, ToolCall, ToolDefinition


@pytest.fixture(autouse=True)
def _no_network_model_meta(monkeypatch):
    """测试期禁止真实联网拉取 models.dev 元数据。

    每个 AgentApp 启动（含 live server 的 lifespan）都会在后台线程调用
    `ModelMetaService.refresh()` → `httpx.get(MODELS_DEV_URL, timeout=10)`，
    失败还会重试。测试用的是临时 config_dir（无缓存）→ 每次都真请求：

    - 网络慢时单个请求吃满 10s + 重试，事件循环 teardown 又等默认线程池退出，
      整个测试套件从 ~80s 拖到 6 分钟（且结果依赖网络，随机变慢/失败）；
    - 元数据相关行为由 tests/test_model_meta.py 用本地缓存文件专项覆盖，
      不需要真实网络。

    统一在 conftest 短路（一处修复，全部测试受益）。
    """
    try:
        from litework.llm.model_meta import ModelMetaService
    except Exception:  # pragma: no cover - 导入失败不影响其他测试
        return
    # 只短路真正的网络下载即可：refresh() 的 TTL 判断本身不联网（缓存新鲜时纯读盘）。
    # 保留真实 refresh() 实现，便于单测覆盖「force 绕过 TTL 强制拉取」的行为。
    monkeypatch.setattr(ModelMetaService, "_fetch_and_store", lambda self: False, raising=False)


class MockLLMAdapter:
    """脚本化 LLM：按调用顺序返回 (content, tool_calls)。"""

    def __init__(self, responses: List[Tuple[str, List[ToolCall]]]) -> None:
        self.responses = list(responses)
        self.calls: List[int] = []

    async def chat_stream(
        self,
        messages: List[Message],
        tools: List[ToolDefinition],
        events: Optional[TypedEventBus] = None,
    ) -> tuple:
        idx = len(self.calls)
        self.calls.append(idx)
        if idx < len(self.responses):
            content, calls = self.responses[idx]
        else:
            content, calls = "（模拟完成）", []
        if events and content:
            await events.emit("llm:stream", {"chunk": content})
        return content, calls, None


def tool_call(name: str, args_json: str, cid: str = "call_1") -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=args_json)


@asynccontextmanager
async def live_server(fast_app, *, timeout: float = 15.0,
                      ready_timeout: float = 15.0) -> AsyncIterator[Tuple[httpx.AsyncClient, uvicorn.Server]]:
    """起一个真实 uvicorn（随机端口），返回**已确认就绪**的 httpx 客户端。

    为什么不用 httpx.ASGITransport：它会把完整响应体缓冲后才返回，无法交互式消费
    SSE 长连接，而依赖本 helper 的用例正是测流式任务与断线重连（见 test_server.py
    顶部说明）。

    为什么需要「就绪探测」：uvicorn 的 ``server.started`` 只表示 lifespan 启动完成，
    并不等于 accept 循环已经开始跑。高负载（全量套件）下跟着 started 立刻发起的
    第一个请求可能拿到 ``RemoteProtocolError``（连接被断开且无响应），表现为随机
    变红（曾观测到 ``test_status_and_sessions`` 偶发）。这里改为用一次幂等
    ``GET /api/status`` 轮询到 200 才算就绪，并给 transport 打开连接层重试兜底。

    产出 ``(client, server)``。退出时置 ``should_exit`` 并等 server 任务结束——放在
    ``finally`` 里，即使就绪断言失败也不会泄漏服务器任务。
    """
    config = uvicorn.Config(fast_app, host="127.0.0.1", port=0, log_level="error",
                            timeout_graceful_shutdown=2)
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    try:
        for _ in range(400):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started, "uvicorn 未在预期时间内启动"
        port = server.servers[0].sockets[0].getsockname()[1]

        # retries：连接层偶发抖动（负载/端口就绪竞争）自动重试，避免用例随机变红
        transport = httpx.AsyncHTTPTransport(retries=3)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}",
                                     timeout=timeout, transport=transport) as client:
            ready = False
            last_err = "无响应"
            deadline = time.monotonic() + ready_timeout
            while time.monotonic() < deadline:
                try:
                    if (await client.get("/api/status")).status_code == 200:
                        ready = True
                        break
                    last_err = "非 200 响应"
                except Exception as exc:      # noqa: BLE001 - 就绪前的连接抖动都重试
                    last_err = repr(exc)
                await asyncio.sleep(0.05)
            assert ready, f"live server 未就绪（{ready_timeout}s）：{last_err}"
            yield client, server
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=10)