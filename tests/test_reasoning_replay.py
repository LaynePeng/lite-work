# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""思考模式推理回传（reasoning_content）测试：白名单判定 + payload 注入位置。

回归背景：DeepSeek 思考模式要求带 tools 的请求里，当前轮的
assistant(tool_calls) 必须回带 `reasoning_content`，否则
`400 The reasoning_content in the thinking mode must be passed back to the
API`（Kimi/GLM 同类）。lite-work 的 CoT 只做流式转发、不入 Message 与落盘，
回传时该字段缺失 → 多轮工具调用必挂。

反向要求同时存在：Mistral/Cerebras/Fireworks 等严格网关（extra="forbid"）
会因该字段多余而 422，故必须是白名单分流，不能全局开关。
"""
from __future__ import annotations

from litework.core.types import Message, ToolCall
from litework.llm.openai_compat import OpenAICompatAdapter
from litework.llm.registry import needs_reasoning_replay


def _tc(cid: str) -> ToolCall:
    return ToolCall(id=cid, name="read_file", arguments="{}")


def _adapter(**kw):
    kw.setdefault("api_key", "sk-test")
    return OpenAICompatAdapter(**kw)


# ---------------------------------------------------------------- 白名单判定

def test_builtin_providers_need_replay():
    for pid in ("deepseek", "kimi", "glm"):
        assert needs_reasoning_replay(pid, "any-model") is True


def test_strict_providers_do_not_need_replay():
    # 严格网关（extra="forbid"）多字段即 422，必须排除
    assert needs_reasoning_replay("openai", "gpt-5.6") is False
    assert needs_reasoning_replay("anthropic", "claude-sonnet-5") is False
    assert needs_reasoning_replay("custom_x", "mistral-large") is False


def test_custom_provider_matched_by_model_hint():
    # self_user 的 opencode-go 网关：pid 是 custom_*，只能按模型名判定
    assert needs_reasoning_replay("custom_1788492302820", "deepseek-v4.1-flash") is True
    assert needs_reasoning_replay("custom_x", "kimi-k3") is True
    assert needs_reasoning_replay("custom_x", "qwen3.8-max") is False


def test_config_mode_always_and_never():
    assert needs_reasoning_replay("openai", "gpt-5.6", {"mode": "always"}) is True
    assert needs_reasoning_replay("deepseek", "deepseek-flash", {"mode": "never"}) is False


def test_config_extra_providers_and_exclude():
    # 追加新供应商
    cfg = {"providers": ["custom_mistral"]}
    assert needs_reasoning_replay("custom_mistral", "zai-glm-5-3", cfg) is True
    # 剔除内置误伤
    assert needs_reasoning_replay("glm", "x", {"exclude_providers": ["glm"]}) is False


def test_config_model_hints_extra_and_string_input():
    assert needs_reasoning_replay("custom_n", "mimo-v2.6-flash") is True  # 内置已含
    assert needs_reasoning_replay("custom_n", "newmodel-x", {"model_hints": ["newmodel"]}) is True
    # 逗号字符串容错（设置页文本框直接粘贴的形态）
    assert needs_reasoning_replay("custom_a", "m", {"providers": "custom_a, custom_b"}) is True


def test_config_tolerates_garbage():
    for bad in (None, [], "x", 3, {"providers": 5}, {"mode": 1}):
        assert needs_reasoning_replay("deepseek", "deepseek-flash", bad) in (True, False)


# ---------------------------------------------------------------- payload 注入

def test_current_turn_tool_call_gets_empty_reasoning():
    """当前轮的 assistant(tool_calls) 补空串（空串即满足契约，无需真实 CoT）。"""
    adapter = _adapter(model="deepseek-flash", replay_reasoning_content=True)
    msgs = [
        Message(role="user", content="问题"),
        Message(role="assistant", content=None, tool_calls=[_tc("c1")]),
        Message(role="tool", content="结果", tool_call_id="c1"),
    ]
    wire = adapter._build_payload(msgs, [])["messages"]
    assert wire[1]["reasoning_content"] == ""


def test_earlier_turns_untouched_for_prefix_cache():
    """只改当前轮：更早轮次逐字节不变，保住 prefix cache 命中。

    改写历史会让已缓存前缀失效（pydantic-ai 实测 4,096 cached tokens 级）。
    """
    adapter = _adapter(model="deepseek-flash", replay_reasoning_content=True)
    msgs = [
        Message(role="user", content="旧问题"),
        Message(role="assistant", content=None, tool_calls=[_tc("old")]),
        Message(role="tool", content="旧结果", tool_call_id="old"),
        Message(role="assistant", content="旧答复"),
        Message(role="user", content="新问题"),
        Message(role="assistant", content=None, tool_calls=[_tc("new")]),
        Message(role="tool", content="新结果", tool_call_id="new"),
    ]
    wire = adapter._build_payload(msgs, [])["messages"]
    assert "reasoning_content" not in wire[1], "历史轮不应被改写"
    assert wire[5]["reasoning_content"] == "", "当前轮应补上"


def test_disabled_never_injects():
    adapter = _adapter(model="mistral-large", replay_reasoning_content=False)
    msgs = [
        Message(role="user", content="q"),
        Message(role="assistant", content=None, tool_calls=[_tc("c1")]),
    ]
    wire = adapter._build_payload(msgs, [])["messages"]
    assert "reasoning_content" not in wire[1]


def test_default_is_off():
    """默认关闭：既有供应商行为不变（避免误伤严格网关）。"""
    adapter = _adapter()
    assert adapter.replay_reasoning_content is False
    msgs = [
        Message(role="user", content="q"),
        Message(role="assistant", content=None, tool_calls=[_tc("c1")]),
    ]
    assert "reasoning_content" not in adapter._build_payload(msgs, [])["messages"][1]


def test_existing_reasoning_value_not_overwritten():
    """已带真实推理（网关回传/历史）不覆盖，避免丢有效内容。"""
    adapter = _adapter(model="deepseek-flash", replay_reasoning_content=True)
    wire = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "z", "name": "f", "arguments": "{}"}],
         "reasoning_content": "真实推理"},
    ]
    adapter._ensure_reasoning_content(wire)
    assert wire[1]["reasoning_content"] == "真实推理"


def test_multiple_tool_calls_in_current_turn_all_filled():
    adapter = _adapter(model="deepseek-flash", replay_reasoning_content=True)
    wire = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
        {"role": "tool", "content": "r", "tool_call_id": "1"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "2"}]},
    ]
    adapter._ensure_reasoning_content(wire)
    assert wire[1]["reasoning_content"] == ""
    assert wire[3]["reasoning_content"] == ""


def test_plain_assistant_text_not_injected():
    """无 tool_calls 的纯文本 assistant 不需要该字段（契约只针对工具调用轮）。"""
    adapter = _adapter(model="deepseek-flash", replay_reasoning_content=True)
    wire = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "text"}]
    adapter._ensure_reasoning_content(wire)
    assert "reasoning_content" not in wire[1]


def test_no_user_message_and_empty_input_safe():
    adapter = _adapter(model="deepseek-flash", replay_reasoning_content=True)
    adapter._ensure_reasoning_content([])  # 不崩
    wire = [{"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]}]
    adapter._ensure_reasoning_content(wire)
    assert "reasoning_content" not in wire[0]


# ---------------------------------------------------------------- 适配器接线

def test_registry_wires_whitelist():
    """build_adapter 必须按白名单把开关接到适配器上。"""
    from litework.llm.registry import LLMRegistry

    reg = LLMRegistry()
    reg.providers["deepseek"]["api_key"] = "sk-test"
    reg.providers["deepseek"]["model"] = "deepseek-flash"
    assert reg.build_adapter("deepseek").replay_reasoning_content is True

    reg.providers["openai"]["api_key"] = "sk-test"
    reg.providers["openai"]["model"] = "gpt-5.6"
    assert reg.build_adapter("openai").replay_reasoning_content is False


def test_registry_config_override_applies():
    from litework.llm.registry import LLMRegistry

    reg = LLMRegistry()
    reg.apply_config({"reasoning_replay": {"mode": "never"}})
    reg.providers["deepseek"]["api_key"] = "sk-test"
    assert reg.build_adapter("deepseek").replay_reasoning_content is False


def test_anthropic_adapter_unaffected():
    """Anthropic 通道用 thinking 块，不接受该参数（接线不能串到它）。"""
    from litework.llm.anthropic import AnthropicAdapter
    from litework.llm.registry import LLMRegistry

    reg = LLMRegistry()
    reg.providers["anthropic"]["api_key"] = "sk-test"
    adapter = reg.build_adapter("anthropic")
    assert isinstance(adapter, AnthropicAdapter)


# ---------------------------------------------------------------- 落盘保留

def test_persist_keeps_non_provider_keys():
    """保存设置时 llm 段的非供应商键（reasoning_replay）不能被覆盖丢。

    陷阱：_persist_config 用 to_config() 重写整个 llm 段，而它只产出
    active + providers——用户手配的 reasoning_replay 会在每次保存设置时被抹掉。
    """
    from unittest.mock import patch

    from litework.app import AgentApp

    app = object.__new__(AgentApp)
    app.config = {"llm": {"reasoning_replay": {"mode": "never"}}}
    app.llm_registry = _FakeRegistry()

    with patch.object(AgentApp, "_write_config_merged", lambda self: None):
        AgentApp._persist_config(app)

    llm = app.config["llm"]
    assert llm["reasoning_replay"] == {"mode": "never"}, "非供应商键被覆盖丢失"
    assert llm["active"] == "deepseek"
    assert "providers" in llm


class _FakeRegistry:
    def to_config(self, persist_key: bool = False):
        return {"active": "deepseek", "providers": {"deepseek": {"api_key": "k"}}}
