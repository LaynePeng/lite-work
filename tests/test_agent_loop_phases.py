# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""P1-4 三阶段拆分的单元测试：LLM 调用 / 工具批次执行 / 状态更新可独立驱动。

run_task 只保留编排，各阶段方法不再依赖完整任务生命周期即可验证。
"""
from __future__ import annotations

import asyncio

import pytest

from litework.core.agent_loop import AgentLoop
from litework.core.types import Message, ToolCall
from litework.llm import LLMError
from litework.tools.registry import ToolRegistry
from tests.conftest import MockLLMAdapter, tool_call


def _make_loop(tmp_path, adapter, registry=None, **kwargs):
    from litework.core.kernel import Kernel
    from litework.core.session_store import SessionStore

    kernel = Kernel("phase-test")
    store = SessionStore(str(tmp_path / "sessions"))
    loop = AgentLoop(kernel=kernel, adapter=adapter, registry=registry or ToolRegistry(),
                     session_store=store, max_steps=10, **kwargs)
    loop.workspace = str(tmp_path)
    return loop, kernel, store


# ---------------------------------------------------------------- 阶段一：LLM 调用

class FlakyAdapter:
    """前 fail_times 次抛错，之后返回固定响应。"""

    def __init__(self, response, fail_times: int, error: Exception) -> None:
        self.response = response
        self.fail_times = fail_times
        self.error = error
        self.attempts = 0

    async def chat_stream(self, messages, tools, events=None):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise self.error
        return self.response


async def test_call_llm_with_retry_success_after_transient(tmp_path):
    adapter = FlakyAdapter(("ok", [], None), fail_times=1,
                           error=LLMError("HTTP 503", retryable=True))
    loop, kernel, _ = _make_loop(tmp_path, adapter, llm_retries=2)
    retries = []
    kernel.events.on("llm:retry", lambda d: retries.append(d))
    content, calls, usage = await loop._call_llm_with_retry([Message(role="user", content="x")], [])
    assert content == "ok"
    assert adapter.attempts == 2
    assert retries and retries[0]["attempt"] == 1


async def test_call_llm_with_retry_fatal_raises_immediately(tmp_path):
    adapter = FlakyAdapter(("ok", [], None), fail_times=5,
                           error=LLMError("HTTP 401", retryable=False))
    loop, _, _ = _make_loop(tmp_path, adapter, llm_retries=3)
    with pytest.raises(AgentLoop._LLMCallFailure):
        await loop._call_llm_with_retry([Message(role="user", content="x")], [])
    assert adapter.attempts == 1  # 不可重试：一次即终局


async def test_call_llm_with_retry_exhaustion(tmp_path):
    adapter = FlakyAdapter(("ok", [], None), fail_times=10,
                           error=LLMError("HTTP 503", retryable=True))
    loop, _, _ = _make_loop(tmp_path, adapter, llm_retries=2)
    with pytest.raises(AgentLoop._LLMCallFailure):
        await loop._call_llm_with_retry([Message(role="user", content="x")], [])
    assert adapter.attempts == 3  # 初次 + 2 次重试


# ---------------------------------------------------------------- 阶段二：工具批次执行

class _Recorder:
    def __init__(self) -> None:
        self.executed: list[str] = []


async def test_execute_tool_batch_serial_order(tmp_path):
    """never 模式：串行按序执行，结果与调用同序。"""
    executed: list[str] = []

    async def handler(args):
        await asyncio.sleep(0)
        executed.append(args["name"])
        return f"done:{args['name']}"

    registry = ToolRegistry()
    registry.register("read_x", "读", [], handler)
    loop, _, _ = _make_loop(tmp_path, MockLLMAdapter([]), registry)
    loop.parallel_tool_calls = "never"

    calls = [ToolCall(id=f"c{i}", name="read_x", arguments=f'{{"name": "f{i}"}}')
             for i in range(3)]
    results = await loop._execute_tool_batch(calls, {"tool_calls": 0})
    assert executed == ["f0", "f1", "f2"]
    assert results == ["done:f0", "done:f1", "done:f2"]


async def test_execute_tool_batch_abort_midway_returns_none(tmp_path):
    executed: list[str] = []

    async def abort_after_first(args):
        executed.append(args["name"])
        loop.abort_event.set()  # 第一个工具执行后触发停止
        return "ok"

    registry = ToolRegistry()
    registry.register("read_x", "读", [], abort_after_first)
    loop, _, _ = _make_loop(tmp_path, MockLLMAdapter([]), registry)
    loop.parallel_tool_calls = "never"
    loop.abort_event = asyncio.Event()  # 生产由 TaskHandle 注入，测试自建

    calls = [ToolCall(id=f"c{i}", name="read_x", arguments=f'{{"name": "f{i}"}}')
             for i in range(3)]
    results = await loop._execute_tool_batch(calls, {"tool_calls": 0})
    assert results is None  # 中途停止 → None（run_task 统一收尾）
    assert executed == ["f0"]  # 剩余调用未执行


async def test_execute_tool_batch_input_interrupts_remaining(tmp_path):
    """排队输入中断剩余工具：未执行项填 [Interrupted] 占位，保持原子对。"""
    async def handler(args):
        if args["name"] == "f0":
            loop.injected_inputs.append("用户补充指令")  # 首个工具后注入
        return "ok"

    registry = ToolRegistry()
    registry.register("read_x", "读", [], handler)
    loop, _, _ = _make_loop(tmp_path, MockLLMAdapter([]), registry)
    loop.parallel_tool_calls = "never"

    calls = [ToolCall(id=f"c{i}", name="read_x", arguments=f'{{"name": "f{i}"}}')
             for i in range(3)]
    results = await loop._execute_tool_batch(calls, {"tool_calls": 0})
    assert results[0] == "ok"
    assert "[Interrupted]" in results[1] and "[Interrupted]" in results[2]


# ---------------------------------------------------------------- 阶段三：状态更新

def test_record_usage_openai_semantics(tmp_path):
    """OpenAI 兼容口径：prompt_tokens 已含命中，miss = prompt - hit。"""
    loop, _, _ = _make_loop(tmp_path, MockLLMAdapter([]))
    stats = {"input_tokens": 0, "output_tokens": 0, "cache_hit_tokens": 0, "cache_miss_tokens": 0}
    loop._record_usage({"prompt_tokens": 100, "completion_tokens": 40,
                        "prompt_cache_hit_tokens": 30}, "text", stats)
    assert stats["input_tokens"] == 100
    assert stats["output_tokens"] == 40
    assert stats["cache_hit_tokens"] == 30
    assert stats["cache_miss_tokens"] == 70


def test_record_usage_anthropic_semantics(tmp_path):
    """Anthropic 口径：input_tokens 不含 cache_read，miss = input_tokens。"""
    loop, _, _ = _make_loop(tmp_path, MockLLMAdapter([]))
    loop.adapter.name = "anthropic"
    stats = {"input_tokens": 0, "output_tokens": 0, "cache_hit_tokens": 0, "cache_miss_tokens": 0}
    loop._record_usage({"prompt_tokens": 100, "completion_tokens": 40,
                        "prompt_cache_hit_tokens": 30}, "text", stats)
    assert stats["cache_miss_tokens"] == 100


def test_record_usage_fallback_estimation(tmp_path):
    """无 usage 时回退按输出文本估算。"""
    loop, _, _ = _make_loop(tmp_path, MockLLMAdapter([]))
    stats = {"input_tokens": 0, "output_tokens": 0, "cache_hit_tokens": 0, "cache_miss_tokens": 0}
    loop._record_usage(None, "一段回复文本", stats)
    assert stats["output_tokens"] > 0


async def test_context_stats_separates_last_call_from_task_total(tmp_path):
    """「本次调用」与「本任务累计」必须分开下发。

    Agent 每轮都把「系统提示 + 全部历史 + 工具结果」整段重发，累计输入 ≈ 轮数 ×
    上下文——若把它当成「本次调用」展示，单轮 1.2 万 token 的任务在几十轮后
    看起来像上百万（历史 bug：面板显示 "本次调用 Prompt tokens: 1,000,000+"）。
    """
    loop, kernel, _ = _make_loop(
        tmp_path, MockLLMAdapter([]),
        pricing={"input_per_mtok": 0.3, "output_per_mtok": 1.2, "cache_hit_per_mtok": 0.006},
        context_window=1_000_000,
    )
    events: list = []
    kernel.events.on("context:stats", lambda d: events.append(d))
    stats = {"input_tokens": 0, "output_tokens": 0, "cache_hit_tokens": 0, "cache_miss_tokens": 0}
    usage = {"prompt_tokens": 12_000, "completion_tokens": 50, "prompt_cache_hit_tokens": 9_000}
    for _ in range(3):
        loop._record_usage(usage, "x", stats)
        await loop._emit_context_stats(stats)

    task = events[-1]["task"]
    # 累计口径：3 轮 × 12,000
    assert task["prompt_tokens"] == 36_000
    # 本次调用口径：最后一轮的真实用量
    assert task["last"]["prompt_tokens"] == 12_000
    assert task["last"]["cache_hit_tokens"] == 9_000
    assert task["last"]["cache_miss_tokens"] == 3_000
    # 当前上下文水位 = 最近一次调用的 prompt（而不是累计值）
    assert task["last_prompt_tokens"] == 12_000
    assert task["usage_ratio"] == 0.012
    # 成本同样分口径：本次 = 单轮，累计 = 3 轮
    single = 3_000 / 1e6 * 0.3 + 9_000 / 1e6 * 0.006 + 50 / 1e6 * 1.2
    assert abs(task["last"]["cost_estimate"] - round(single, 4)) < 1e-9
    assert abs(task["cost_estimate"] - round(single * 3, 4)) < 1e-9
    # 计费单价随统计下发（面板展示成本依据）
    assert events[-1]["pricing"] == {
        "input_per_mtok": 0.3, "output_per_mtok": 1.2, "cache_hit_per_mtok": 0.006}


async def test_context_stats_uses_estimate_not_total_without_usage(tmp_path):
    """无 usage 时（供应商不回传），本次调用的 prompt 也必须是本轮估算值。"""
    loop, kernel, _ = _make_loop(tmp_path, MockLLMAdapter([]), context_window=1_000_000)
    events: list = []
    kernel.events.on("context:stats", lambda d: events.append(d))
    # 模拟 run_task 无 usage 时的累计（input_tokens 已累加到 12,000）
    stats = {"input_tokens": 12_000, "output_tokens": 0,
             "cache_hit_tokens": 0, "cache_miss_tokens": 0}
    loop._last_prompt_estimate = 7_000
    loop._record_usage(None, "回复", stats)
    await loop._emit_context_stats(stats)
    task = events[-1]["task"]
    assert task["prompt_tokens"] == 12_000          # 累计口径
    assert task["last"]["prompt_tokens"] == 7_000   # 本次调用 = 本轮估算，而非累计
    assert task["last_prompt_tokens"] == 7_000


async def test_append_tool_results_preserves_pairing(tmp_path):
    """结果回填保持 assistant tool_calls ↔ tool 消息原子对（id 对应）。"""
    loop, kernel, _ = _make_loop(tmp_path, MockLLMAdapter([]))
    messages = [Message(role="assistant", content=None, tool_calls=[
        ToolCall(id="c1", name="read_file", arguments="{}"),
        ToolCall(id="c2", name="search_code", arguments="{}"),
    ])]
    await loop._append_tool_results(
        [messages[0].tool_calls[0], messages[0].tool_calls[1]],
        ["结果一", "结果二"], messages,
    )
    assert [m.role for m in messages[1:]] == ["tool", "tool"]
    assert [m.tool_call_id for m in messages[1:]] == ["c1", "c2"]
    assert [m.content for m in messages[1:]] == ["结果一", "结果二"]
