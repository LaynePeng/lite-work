# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""流式增量计量器：为「本轮 token 速度」提供**零成本**的数据采集。

为什么需要它
------------
模型只在**流结束时**返回 usage（OpenAI 兼容走 `stream_options.include_usage`
的末帧，Anthropic 走 `message_delta`），所以「正在生成时每秒多少 token」拿不到
精确值；而等 usage 到手时本轮已经结束。行业通用做法（见 opencode #5374 的
"current + average tokens/s" 与社区实现）是：

1. 流式期间用**字符增量估算** token 数，给出实时的近似速度；
2. 流结束时用 usage 的精确值**校准**，最终数字是准的。

性能纪律（本模块的存在意义）
----------------------------
- 每个增量块只做「整数累加 + 对**该增量**做一次 CJK 小正则」，O(delta)；
- **绝不**对不断增长的全文本反复调 `TokenCounter.count_text_tokens`（O(n²) 正则），
  也**绝不**引入 tiktoken 之类的分词器（每块数 ms）；
- 实时推送由 :meth:`feed` 的返回值**节流**（默认 400ms），避免把前端刷爆。

因此额外开销相对「每块一次 llm:stream（含全文增量）」可忽略。
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Dict, Iterator, Optional

from ..core.token_counter import TokenCounter

# 实时进度推送的节流窗口（毫秒）：约 2.5 次/秒，肉眼够平滑，又不至于刷爆前端
EMIT_INTERVAL_MS = 400


# 当前调用所属的计量器：由 AgentLoop 在发起 LLM 调用前用 use_stream_meter 设置。
# 为什么用 contextvar 而不是给 chat_stream 加参数：
#   1) 适配器签名保持不变——社区/测试里的自定义适配器（大量 `chat_stream(messages,
#      tools, events)` 实现）无需改动，不实现计量也不影响功能（拿不到速度而已）；
#   2) contextvar 是「按任务隔离」的：主任务、子 Agent、并发会话各持自己的值，
#      不会像实例属性那样在并发调用间串味。
_current_meter: ContextVar[Optional["StreamMeter"]] = ContextVar(
    "litework_stream_meter", default=None)


def current_stream_meter() -> Optional["StreamMeter"]:
    """取当前调用上下文里的计量器（适配器内部调用；无则 None = 不做计量）。"""
    return _current_meter.get()


@contextmanager
def use_stream_meter(meter: Optional["StreamMeter"]) -> Iterator[Optional["StreamMeter"]]:
    """把 meter 绑定为「本次 LLM 调用」的计量器，退出时还原。

    绑定的作用域必须**只包住一次 chat_stream 调用**（含重试的单次尝试），
    否则并发/嵌套调用会互相看到对方的计量器。
    """
    token = _current_meter.set(meter)
    try:
        yield meter
    finally:
        _current_meter.reset(token)


class StreamMeter:
    """一次 LLM 调用的流式计量器（只做累加与时间戳，不做分词）。

    生命周期（由 AgentLoop 持有）::

        meter.reset(); meter.start()          # 每次尝试（重试）前重置并记下发请求时刻
        meter.feed(delta)                     # 每个增量块；返回 True 表示该推送实时进度了
        meter.snapshot(exact_output_tokens=…)  # 流结束后取结果（可带 usage 精确值校准）

    口径：
      - ``ttft_ms``  首字延迟 = 首个增量块 − 请求发出（含排队与网络）
      - ``gen_ms``   **生成窗口** = 末块 − 首块（排除首字等待，速度的主口径）
      - ``e2e_ms``   端到端 = 末块 − 请求发出（含首字，贴近体感）
    """

    EMIT_INTERVAL_MS = EMIT_INTERVAL_MS

    def __init__(self, clock: Optional[Callable[[], float]] = None) -> None:
        # 可注入时钟：测试里用假时钟做确定性断言（生产用 perf_counter，单调且高精度）
        self._clock: Callable[[], float] = clock or time.perf_counter
        self.reset()

    # ------------------------------------------------------------ 生命周期

    def reset(self) -> None:
        """重置所有计数与时间戳（重试时调用，避免把失败尝试的字符算进速度）。"""
        self.started_at: Optional[float] = None
        self.first_chunk_at: Optional[float] = None
        self.last_chunk_at: Optional[float] = None
        self.chars = 0
        self.cjk_chars = 0
        self.chunks = 0
        self._last_emit_at = 0.0

    def start(self) -> None:
        """记录「请求发出」时刻（TTFT 与端到端速度的起点）。"""
        now = self._clock()
        self.started_at = now
        self._last_emit_at = now

    # ------------------------------------------------------------ 采集（热路径）

    def feed(self, text: str) -> bool:
        """记录一个流式增量；返回 True 表示到达节流窗口、调用方应推送一次实时进度。

        热路径：只做几次整数运算 + 对**本增量**的小正则；无 I/O、无分配（除正则内部）。
        """
        if not text:
            return False
        now = self._clock()
        if self.started_at is None:      # 防御：调用方忘了 start（例如测试直接 feed）
            self.started_at = now
            self._last_emit_at = now
        if self.first_chunk_at is None:
            self.first_chunk_at = now
        self.last_chunk_at = now
        self.chunks += 1
        self.chars += len(text)
        self.cjk_chars += TokenCounter.cjk_count(text)
        if (now - self._last_emit_at) * 1000.0 >= self.EMIT_INTERVAL_MS:
            self._last_emit_at = now
            return True
        return False

    # ------------------------------------------------------------ 口径

    @property
    def est_output_tokens(self) -> int:
        """按增量字符估算的输出 token（与 TokenCounter 同一公式，minimum=0）。"""
        return TokenCounter.count_tokens_by_counts(
            self.cjk_chars, self.chars - self.cjk_chars, minimum=0)

    def ttft_ms(self) -> Optional[int]:
        if self.started_at is None or self.first_chunk_at is None:
            return None
        return max(0, self._to_ms(self.first_chunk_at - self.started_at))

    def gen_ms(self) -> Optional[int]:
        """生成窗口（首块→末块）；只有 1 个块时为 0（速度无意义，由调用方判空）。"""
        if self.first_chunk_at is None or self.last_chunk_at is None:
            return None
        return max(0, self._to_ms(self.last_chunk_at - self.first_chunk_at))

    def e2e_ms(self) -> Optional[int]:
        if self.started_at is None or self.last_chunk_at is None:
            return None
        return max(0, self._to_ms(self.last_chunk_at - self.started_at))

    @staticmethod
    def _to_ms(seconds: float) -> int:
        """秒 → 毫秒整数：**四舍五入**（int() 是截断，会让每个窗口系统性少报 <1ms）。"""
        return int(round(seconds * 1000.0))

    @staticmethod
    def _tps(tokens: Optional[int], ms: Optional[int]) -> Optional[float]:
        """tokens ÷ 秒；窗口太小（<50ms）或没吐字时返回 None——那种采样值纯噪音。"""
        if tokens is None or ms is None or ms < 50 or tokens <= 0:
            return None
        return round(tokens / (ms / 1000.0), 2)

    def snapshot(self, *, exact_output_tokens: Optional[int] = None) -> Dict[str, Any]:
        """当前快照。

        exact_output_tokens 传入 usage 的精确输出 token 时，tokens 字段即精确值，
        ``estimated`` 为 False（流结束后的校准）；否则用字符估算，``estimated`` 为 True。
        """
        estimated = exact_output_tokens is None
        tokens = self.est_output_tokens if estimated else int(exact_output_tokens or 0)
        gen_ms, e2e_ms = self.gen_ms(), self.e2e_ms()
        return {
            "output_tokens": tokens,
            "estimated": estimated,
            "chars": self.chars,
            "chunks": self.chunks,
            "ttft_ms": self.ttft_ms(),
            "gen_ms": gen_ms,
            "e2e_ms": e2e_ms,
            "tps_gen": self._tps(tokens, gen_ms),
            "tps_e2e": self._tps(tokens, e2e_ms),
        }

    def progress(self) -> Dict[str, Any]:
        """实时进度事件载荷（llm:progress）：只带实时展示需要的字段。"""
        snap = self.snapshot()
        return {
            "est_tokens": snap["output_tokens"],
            "chars": snap["chars"],
            "chunks": snap["chunks"],
            "ttft_ms": snap["ttft_ms"],
            "gen_ms": snap["gen_ms"],
            "tps": snap["tps_gen"],
        }
