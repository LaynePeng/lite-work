# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""推理强度（reasoning_effort）全链路测试：适配器 payload 注入 + create_loop override 语义。

回归背景：前端「切换 effort 不生效」bug 曾因 send 闭包读到旧档位而漏网——
本文件从后端补上 override 语义与 payload 注入的两端护栏。
"""
from __future__ import annotations

from litework.app import AgentApp
from litework.core.kernel import Kernel
from litework.core.types import Message
from litework.llm.anthropic import AnthropicAdapter
from litework.llm.openai_compat import OpenAICompatAdapter
from litework.tools.registry import ToolRegistry


# ---------------------------------------------------------------- 适配器 payload

def test_openai_payload_reasoning_effort_injected_and_temperature_omitted():
    adapter = OpenAICompatAdapter(api_key="sk-test", reasoning_effort="low")
    payload = adapter._build_payload([Message(role="user", content="hi")], [])
    assert payload["reasoning_effort"] == "low"
    assert "temperature" not in payload


def test_openai_payload_without_effort_keeps_temperature():
    adapter = OpenAICompatAdapter(api_key="sk-test", reasoning_effort="")
    payload = adapter._build_payload([Message(role="user", content="hi")], [])
    assert "reasoning_effort" not in payload
    assert payload["temperature"] == 0.2


def test_anthropic_payload_effort_maps_to_thinking_budget():
    adapter = AnthropicAdapter(api_key="sk-ant-test", reasoning_effort="max")
    payload = adapter._build_payload(
        [Message(role="user", content="hi")], [], system="sys", enable_cache=False,
    )
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 64000}
    assert "temperature" not in payload


def test_anthropic_payload_without_effort_keeps_temperature():
    adapter = AnthropicAdapter(api_key="sk-ant-test", reasoning_effort="")
    payload = adapter._build_payload(
        [Message(role="user", content="hi")], [], system="sys", enable_cache=False,
    )
    assert "thinking" not in payload
    assert payload["temperature"] == 0.2


# ---------------------------------------------------------------- create_loop override 语义

def _make_app(tmp_path) -> AgentApp:
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    app.llm_registry.providers["deepseek"]["api_key"] = "sk-test"
    return app


def _loop_effort(app: AgentApp, override):
    kernel = Kernel("s-effort")
    registry = ToolRegistry()
    loop = app.create_loop(kernel, registry, reasoning_effort_override=override)
    return loop.adapter.reasoning_effort


def test_create_loop_effort_override_semantics(tmp_path):
    app = _make_app(tmp_path)

    # 无 override → 跟随 provider 配置（默认空串 = 关闭）
    assert _loop_effort(app, None) == ""

    # "off" = 显式关闭（覆盖 provider 默认 → 空串）
    assert _loop_effort(app, "off") == ""

    # provider 配置了默认高（走 update_llm_config：持久化 + reset_adapter）→ 无 override 时生效
    app.update_llm_config(providers={"deepseek": {"reasoning_effort": "high"}})
    assert _loop_effort(app, None) == "high"

    # override "low" 覆盖 provider 默认
    assert _loop_effort(app, "low") == "low"

    # override "off" 覆盖 provider 默认 high → 显式关闭
    assert _loop_effort(app, "off") == ""
