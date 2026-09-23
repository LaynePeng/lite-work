# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""本轮 token 速度（StreamMeter + 适配器计量 + AgentLoop 口径）回归测试。

覆盖：
  - StreamMeter 口径（估算公式 / 首字 TTFT / 生成窗口 / 端到端 / 节流 / 重置）
  - usage 校准（有 usage → 精确且 estimated=False；无 usage → 估算且 estimated=True）
  - 采样保护（生成窗口过小 → 速度 None，避免噪音数字）
  - 性能纪律（只按增量累加，不对全文本重复分词）
  - 适配器接线（通过 contextvar 注入计量器；节流点 emit llm:progress，strict 校验通过）
  - AgentLoop：_record_usage 写入速度字段 + 任务平均的配对累计；重试只计成功那次
"""
from __future__ import annotations

import json

import httpx
import pytest

from litework.core.events import TypedEventBus
from litework.core.token_counter import TokenCounter
from litework.core.types import Message
from litework.llm.openai_compat import OpenAICompatAdapter
from litework.llm.stream_meter import (
    EMIT_INTERVAL_MS,
    StreamMeter,
    use_stream_meter,
)

# ---------------------------------------------------------------- 假时钟


class FakeClock:
    """可控单调时钟（秒）：测试里用 advance 精确控制时间轴。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _meter(clock: FakeClock | None = None) -> tuple[StreamMeter, FakeClock]:
    clk = clock or FakeClock()
    return StreamMeter(clock=clk), clk


# ---------------------------------------------------------------- TokenCounter 口径


def test_token_counter_cjk_and_shared_formula():
    """CJK 计数与估算公式是唯一口径来源（流式计量复用同一公式）。"""
    assert TokenCounter.cjk_count("你好 world") == 2
    assert TokenCounter.cjk_count("hello") == 0
    # count_text_tokens 的 minimum=1（空文本也按 1 计，保持既有行为）
    assert TokenCounter.count_text_tokens("") == 1
    # 流式累计用 minimum=0（还没吐字就是 0）
    assert TokenCounter.count_tokens_by_counts(0, 0, minimum=0) == 0
    # 同一输入两条路径结果一致
    assert TokenCounter.count_text_tokens("你好 world") == TokenCounter.count_tokens_by_counts(2, 6)


# ---------------------------------------------------------------- StreamMeter 口径


def test_meter_estimates_tokens_from_increment():
    """估算只按增量累加：CJK ×1.3 + 其余 ÷3.8（与 TokenCounter 同公式）。"""
    m, _ = _meter()
    m.start()
    m.feed("你好")          # 2 CJK
    m.feed(" world")        # 6 非 CJK
    assert m.est_output_tokens == TokenCounter.count_tokens_by_counts(2, 6, minimum=0)
    assert m.chunks == 2
    assert m.chars == 8


def test_meter_ttft_gen_and_e2e_windows():
    """三个时间窗：TTFT（请求→首块）、生成窗口（首块→末块）、端到端（请求→末块）。"""
    m, clk = _meter()
    m.start()                     # t=1000.0 请求发出
    clk.advance(0.8)
    m.feed("A")                   # t=1000.8 首块 → TTFT 800ms
    clk.advance(1.2)
    m.feed("B")                   # t=1002.0 末块 → 生成窗口 1200ms
    assert m.ttft_ms() == 800
    assert m.gen_ms() == 1200
    assert m.e2e_ms() == 2000


def test_meter_snapshot_calibrates_with_exact_usage():
    """usage 到手后用精确 tokens 校准：estimated 翻转，速度按精确值算。"""
    m, clk = _meter()
    m.start()
    m.feed("你好世界")             # 估算值
    clk.advance(1.0)
    m.feed("再见")                 # 生成窗口 ≈ 1000ms
    est = m.snapshot()
    assert est["estimated"] is True
    assert est["tps_gen"] is not None
    exact = m.snapshot(exact_output_tokens=100)
    assert exact["estimated"] is False
    assert exact["output_tokens"] == 100
    # 100 tokens / 1.0s ≈ 100 tok/s（按生成窗口算）
    assert exact["tps_gen"] == pytest.approx(100.0, abs=1.0)


def test_meter_skips_noisy_samples():
    """生成窗口过小（<50ms）或没吐字 → 速度为 None，不给用户看噪音数字。"""
    m, clk = _meter()
    m.start()
    m.feed("A")
    clk.advance(0.01)             # 只有 10ms
    m.feed("B")
    assert m.snapshot()["tps_gen"] is None

    empty, _ = _meter()
    empty.start()
    assert empty.snapshot()["tps_gen"] is None
    assert empty.snapshot()["ttft_ms"] is None


def test_meter_throttles_progress_emission():
    """节流：只有跨越 EMIT_INTERVAL_MS 才让调用方推送实时进度。"""
    m, clk = _meter()
    m.start()
    assert m.feed("A") is False            # 紧跟 start：还在窗口内
    clk.advance(EMIT_INTERVAL_MS / 1000.0 + 0.01)
    assert m.feed("B") is True             # 跨过窗口 → 该推送
    assert m.feed("C") is False            # 窗口重置后立即再喂 → 不推送
    clk.advance(EMIT_INTERVAL_MS / 1000.0 + 0.01)
    assert m.feed("D") is True


def test_meter_reset_clears_previous_attempt():
    """重试语义：reset 后上一（失败）尝试的字符/时间戳不再计入。"""
    m, clk = _meter()
    m.start()
    m.feed("失败的半截输出")
    clk.advance(2.0)
    m.reset()
    assert m.est_output_tokens == 0
    assert m.gen_ms() is None
    assert m.ttft_ms() is None
    m.start()                              # 第二次尝试
    clk.advance(0.5)
    m.feed("这是第二次尝试的成功输出")        # CJK 足够多，估算值必然 > 0
    assert m.est_output_tokens > 0
    assert m.ttft_ms() == 500


def test_meter_progress_payload_shape():
    """llm:progress 载荷字段固定（前端按这些字段渲染）。"""
    m, clk = _meter()
    m.start()
    clk.advance(0.3)
    m.feed("你好")
    clk.advance(1.0)
    m.feed("世界")
    payload = m.progress()
    assert set(payload) == {"est_tokens", "chars", "chunks", "ttft_ms", "gen_ms", "tps"}
    assert payload["chunks"] == 2
    assert payload["ttft_ms"] == 300
    assert payload["gen_ms"] == 1000


# ---------------------------------------------------------------- 接线守卫


def test_llm_progress_is_registered_and_forwarded():
    """接线守卫：事件表注册 + SSE 转发集合。

    这类「少加一行就静默不生效」的接线最容易漏——strict 模式对**未注册**事件只记
    warning 不抛错，所以上面的适配器用例通过并不代表接线完整，这里显式断言。
    """
    from litework.core.events import LLMProgressPayload, TypedEventBus
    from litework.server.tasks import EVENT_FORWARD

    assert TypedEventBus.EVENT_MAP.get("llm:progress") is LLMProgressPayload
    assert "llm:progress" in EVENT_FORWARD


# ---------------------------------------------------------------- 适配器接线


def _sse(events: list) -> bytes:
    return ("\n".join(f"data: {json.dumps(e, ensure_ascii=False)}" for e in events)
            + "\ndata: [DONE]\n\n").encode("utf-8")


def _openai_adapter(handler) -> OpenAICompatAdapter:
    adapter = OpenAICompatAdapter(api_key="sk-test", base_url="https://mock.test")
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return adapter


async def test_adapter_feeds_meter_via_contextvar_and_emits_progress():
    """适配器从 contextvar 取计量器：累加增量 + 到节流点推送 llm:progress（strict 通过）。"""
    def handler(request):
        return httpx.Response(200, content=_sse([
            {"choices": [{"delta": {"content": "你好"}}]},
            {"choices": [{"delta": {"content": "世界"}}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 4}},
        ]), headers={"content-type": "text/event-stream"})

    bus = TypedEventBus(strict=True)       # strict：载荷字段声明不符会直接抛错
    seen: list[tuple[str, dict]] = []

    async def on_progress(payload: dict) -> None:
        seen.append(("llm:progress", payload))

    async def on_stream(payload: dict) -> None:
        seen.append(("llm:stream", payload))

    bus.on("llm:progress", on_progress)
    bus.on("llm:stream", on_stream)

    meter, clk = _meter()
    meter.start()
    # 关键：不用改 chat_stream 签名，靠 contextvar 注入
    with use_stream_meter(meter):
        adapter = _openai_adapter(handler)
        # 让第一个 feed 之后立即跨过节流窗口，确保产生一次 llm:progress
        clk.advance(EMIT_INTERVAL_MS / 1000.0 + 0.01)
        content, _calls, usage = await adapter.chat_stream(
            [Message(role="user", content="hi")], [], bus)
    await adapter.close()

    assert content == "你好世界"
    assert usage == {"prompt_tokens": 10, "completion_tokens": 4,
                     "prompt_cache_hit_tokens": 0}
    # 计量器确实收到了两个增量
    assert meter.est_output_tokens > 0
    names = [n for n, _ in seen]
    assert names.count("llm:stream") == 2
    assert names.count("llm:progress") >= 1
    progress = dict(seen)["llm:progress"]
    assert progress["est_tokens"] > 0
    # 校准可用：流末 AgentLoop 会用 usage 的 completion_tokens 覆盖估算
    exact = meter.snapshot(exact_output_tokens=usage["completion_tokens"])
    assert exact["estimated"] is False and exact["output_tokens"] == 4


async def test_adapter_without_meter_is_unaffected():
    """没注入计量器（社区/自定义适配器场景）：行为与以前完全一致，不产生 llm:progress。"""
    def handler(request):
        return httpx.Response(200, content=_sse([
            {"choices": [{"delta": {"content": "hi"}}]},
        ]), headers={"content-type": "text/event-stream"})

    bus = TypedEventBus(strict=True)
    seen: list[str] = []

    async def on_progress(payload: dict) -> None:
        seen.append("llm:progress")

    async def on_stream(payload: dict) -> None:
        seen.append("llm:stream")

    bus.on("llm:progress", on_progress)
    bus.on("llm:stream", on_stream)
    adapter = _openai_adapter(handler)
    content, _calls, _usage = await adapter.chat_stream(
        [Message(role="user", content="hi")], [], bus)
    await adapter.close()
    assert content == "hi"
    assert seen == ["llm:stream"]          # 无计量器 → 不推送进度


# ---------------------------------------------------------------- AgentLoop 口径


def _loop():
    """最小 AgentLoop：只测 _record_usage/_record_speed 的纯计算，不跑完整任务。"""
    from litework.core.agent_loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop._last_call = None
    loop._last_usage = None
    loop._last_prompt_estimate = 0
    loop._speed_output_tokens = 0
    loop._speed_gen_ms = 0
    loop._speed_turns = 0

    class _Adapter:
        name = "openai-compat"

    loop.adapter = _Adapter()
    return loop


def _stats() -> dict:
    return {"input_tokens": 0, "output_tokens": 0, "cache_hit_tokens": 0,
            "cache_miss_tokens": 0, "tool_calls": 0, "turns": 0, "blocked": 0}


def test_record_usage_writes_speed_and_task_average():
    """有 usage：速度用精确 tokens 校准；任务平均按配对累计。"""
    loop = _loop()
    stats = _stats()
    meter, clk = _meter()
    meter.start()
    clk.advance(0.5)
    meter.feed("你好世界")                  # 首块（TTFT 500ms）
    clk.advance(1.5)
    meter.feed("继续输出")                  # 末块（生成窗口 1500ms）

    loop._record_usage({"prompt_tokens": 100, "completion_tokens": 60,
                        "prompt_cache_hit_tokens": 40}, "你好世界继续输出", stats, meter)

    call = loop._last_call or {}
    assert call["output_tokens"] == 60
    assert call["ttft_ms"] == 500
    assert call["gen_ms"] == 1500
    assert call["tps_estimated"] is False
    assert call["tps_gen"] == pytest.approx(40.0, abs=0.5)   # 60 / 1.5s（生成窗口）
    assert call["tps_e2e"] == pytest.approx(30.0, abs=0.5)   # 60 / 2.0s（首字 0.5 + 生成 1.5）
    # 任务平均：1 轮、Σ输出 60、Σ窗口 1.5s
    assert loop._speed_turns == 1
    assert loop._speed_output_tokens == 60
    assert loop._speed_gen_ms == 1500


def test_record_usage_without_usage_keeps_estimate():
    """无 usage（部分中转不返回）：走估算路径，速度标为估算。"""
    loop = _loop()
    stats = _stats()
    loop._last_prompt_estimate = 1_000
    meter, clk = _meter()
    meter.start()
    meter.feed("fallback")
    clk.advance(1.0)
    meter.feed("text")

    loop._record_usage(None, "fallback text", stats, meter)

    call = loop._last_call or {}
    assert call["tps_estimated"] is True
    assert call["prompt_tokens"] == 1_000          # 无 usage → 用调用前估算的 prompt
    assert call["output_tokens"] == TokenCounter.count_text_tokens("fallback text")
    assert stats["output_tokens"] == call["output_tokens"]


def test_record_usage_without_meter_skips_speed():
    """适配器未计量（meter=None）：只算用量，不写速度字段、不动平均累计。"""
    loop = _loop()
    stats = _stats()
    loop._record_usage({"prompt_tokens": 10, "completion_tokens": 5,
                        "prompt_cache_hit_tokens": 0}, "x", stats, None)
    assert "tps_gen" not in (loop._last_call or {})
    assert loop._speed_turns == 0
