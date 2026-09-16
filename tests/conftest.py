# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""共享测试工具：Mock LLM 适配器。"""
from __future__ import annotations

import os

# 测试套件禁用引擎预装：大量 AgentApp 实例会并发拖起 npm install，
# 拖垮测试环境（live server 超时）。预装逻辑由专项测试覆盖。
os.environ.setdefault("LITEWORK_SKIP_ENGINE_PREINSTALL", "1")

import pytest
from typing import List, Optional, Tuple
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
    monkeypatch.setattr(ModelMetaService, "refresh", lambda self: False, raising=False)
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