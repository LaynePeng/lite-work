# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""P1-3 事件总线测试：TypedDict 负载声明 + emit 运行时校验。

strict 模式（LITEWORK_STRICT_EVENTS=1 / 构造参数）下 payload 漂移直接抛出；
默认模式记录错误日志但继续分发（UI 事件问题不应杀死 Agent 任务）。
"""
from __future__ import annotations

import asyncio

import pytest

from litework.core.events import (
    EventPayloadError,
    TypedEventBus,
    validate_payload,
)


def test_event_map_values_are_typed_dicts():
    """EVENT_MAP 名副其实：每个事件都有结构化 TypedDict 声明。"""
    import typing

    for name, payload_type in TypedEventBus.EVENT_MAP.items():
        assert isinstance(payload_type, type), f"{name} 的声明不是类型"
        # TypedDict 类带 __required_keys__ / __optional_keys__
        assert hasattr(payload_type, "__required_keys__"), f"{name} 非 TypedDict"
        assert hasattr(payload_type, "__optional_keys__"), f"{name} 非 TypedDict"
        typing.get_type_hints(payload_type)  # 注解可解析（无 NameError）


async def test_emit_valid_payload_dispatches_to_listeners():
    bus = TypedEventBus(strict=True)
    got = []
    bus.on("llm:stream", lambda p: got.append(p["chunk"]))
    await bus.emit("llm:stream", {"chunk": "hello"})
    assert got == ["hello"]


async def test_strict_mode_raises_on_missing_field():
    bus = TypedEventBus(strict=True)
    with pytest.raises(EventPayloadError, match="chunk"):
        await bus.emit("llm:stream", {})


async def test_strict_mode_raises_on_type_drift():
    bus = TypedEventBus(strict=True)
    with pytest.raises(EventPayloadError, match="期望"):
        await bus.emit("llm:stream", {"chunk": 123})


async def test_strict_mode_raises_on_unknown_field():
    bus = TypedEventBus(strict=True)
    with pytest.raises(EventPayloadError, match="声明外"):
        await bus.emit("llm:stream", {"chunk": "x", "bonus": 1})


async def test_non_strict_mode_logs_and_still_dispatches():
    """生产默认：payload 不合规记录日志（含 traceback），监听器照常收到事件。"""
    bus = TypedEventBus(strict=False)
    got = []
    bus.on("llm:stream", lambda p: got.append(p))
    await bus.emit("llm:stream", {"chunk": 42})  # 类型漂移
    assert got == [{"chunk": 42}]


async def test_bool_rejected_for_int_field():
    bus = TypedEventBus(strict=True)
    with pytest.raises(EventPayloadError):
        await bus.emit("llm:turn_start", {"turn": True})


async def test_optional_fields_checked_when_present():
    validate_payload("subagent:progress", {
        "subagentId": "sa_1", "role": "general", "kind": "tool:before_execute",
    })
    with pytest.raises(EventPayloadError):
        validate_payload("subagent:progress", {
            "subagentId": "sa_1", "role": "general", "kind": "llm:turn_start",
            "turn": "three",  # turn 声明为 int
        })


async def test_union_and_list_element_validation():
    # AgentSpawnedPayload.nickname: Optional[str]
    validate_payload("agent:spawned", {
        "agentId": "a1", "nickname": None, "role": "general", "task": "t",
        "allowedDirs": ["src/"], "mode": "orchestrate", "model": None,
    })
    with pytest.raises(EventPayloadError):
        validate_payload("agent:spawned", {
            "agentId": "a1", "nickname": None, "role": "general", "task": "t",
            "allowedDirs": [1, 2],  # List[str] 元素类型漂移
            "mode": "orchestrate", "model": None,
        })


async def test_validate_payload_rejects_unregistered_event():
    with pytest.raises(EventPayloadError, match="未注册"):
        validate_payload("no:such_event", {})


async def test_listener_exception_isolated():
    """单个监听器异常不影响后续监听器（既有语义保持）。"""
    bus = TypedEventBus(strict=True)
    got = []

    def bad(_p):
        raise RuntimeError("boom")

    bus.on("llm:stream", bad)
    bus.on("llm:stream", lambda p: got.append(p["chunk"]))
    await bus.emit("llm:stream", {"chunk": "ok"})
    assert got == ["ok"]


def test_llm_retry_is_registered_and_forwarded():
    """回归：llm:retry 此前未注册进 EVENT_MAP / 未转发 SSE，前端收不到。"""
    assert "llm:retry" in TypedEventBus.EVENT_MAP
    from litework.server.tasks import EVENT_FORWARD

    assert "llm:retry" in EVENT_FORWARD
    validate_payload("llm:retry", {
        "attempt": 1, "max_retries": 2, "reason": "timeout", "wait": 2,
    })


async def test_sequential_listener_registration_preserved():
    """监听器按注册顺序执行（Set 容器顺序不确定，List 保证确定序）。"""
    bus = TypedEventBus()
    order = []
    bus.on("llm:stream", lambda p: order.append("first"))
    bus.on("llm:stream", lambda p: order.append("second"))
    await bus.emit("llm:stream", {"chunk": "x"})
    assert order == ["first", "second"]
