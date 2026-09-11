# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""压缩经济学（SoL-Pi C20 思路的 lite-work 适配）：「要不要压缩」是个经济问题。

旧策略是「上下文超过 90% 窗口就 LLM 摘要压缩」。但它忽略了压缩本身的代价：

1. 摘要调用要按**缓存写入价**消耗 head_tokens 一次（小模型也要钱）；
2. 压缩会**打破 prompt 缓存前缀**——旧前缀的缓存作废，后续每轮都要按未命中
   重读新前缀，这是一笔「缓存债」；
3. 收益 = (head_tokens - 摘要 tokens) × 剩余预期请求数。

只有当「收益 > 写入成本 + 缓存债」时压缩才划算；否则推迟（deferred），
继续用免费的 prune 兜底。每个决策都带 reason 字段，可解释、可展示。

窗口保护优先于经济学：上下文逼近窗口上限时无论划不划算都必须压
（否则任务直接撑爆），这对应 SoL-Pi 的 window_protection。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class CompactionDecision:
    compact: bool
    reason: str
    saving_tokens: int            # 每个后续请求省下的 tokens（head - 摘要）
    write_cost_tokens: int        # 摘要调用的一次性写入成本（≈ head_tokens）
    cache_debt_tokens: int        # 缓存债：旧前缀作废，后续需重读的 tokens
    breakeven_requests: Optional[int]   # 多少个后续请求才能回本
    expected_remaining_requests: Optional[int]  # 预期剩余请求数
    cache_write_read_ratio: Optional[float]


def cache_write_read_ratio(pricing: Optional[dict]) -> Optional[float]:
    """缓存写入价 / 读取价（DeepSeek 等供应商两者差一个数量级以上）。"""
    if not pricing:
        return None
    input_price = float(pricing.get("input_per_mtok", 0) or 0)
    hit_price = float(pricing.get("cache_hit_per_mtok", 0) or 0)
    if input_price <= 0 or hit_price <= 0:
        return None
    return input_price / hit_price


def decide_compaction(
    *,
    head_tokens: int,
    summary_tokens: int,
    context_tokens: int,
    context_window: int,
    current_turn: int,
    max_turns: int,
    pricing: Optional[dict] = None,
    window_reserve_ratio: float = 0.9,
    subsequent_margin: float = 1.5,
) -> CompactionDecision:
    """判断「值得做 LLM 摘要压缩」还是「推迟/直接用免费裁剪兜底」。

    - head_tokens:      待摘要的旧消息 tokens（也是摘要调用的一次性写入成本）
    - summary_tokens:   预估摘要长度（lite-work 用 head 的 10% 估；真实值出来后
                        由调用方校正 _compressed_tokens）
    - cache_write_read_ratio: 写入价/读取价；缺省（未知供应商）时视为无缓存
                        债务（ratio=1 → breakeven=0 → 只看剩余轮数是否 > 0）
    """
    ratio = cache_write_read_ratio(pricing)
    incremental = max(0.0, (ratio or 1.0) - 1.0)
    saving = max(0, head_tokens - summary_tokens)
    write_cost = head_tokens
    # 缓存债：压缩后旧前缀作废，剩余上下文（tail ≈ context - head）需按未命中重读
    cache_debt = max(0, context_tokens - head_tokens)

    breakeven: Optional[int] = None
    if saving > 0:
        # 回本请求数 =（一次性写入 + 缓存债的增量成本）/ 每请求节省
        breakeven = int((write_cost * incremental + cache_debt * incremental) / saving)

    # 预期剩余请求数：任务步数上限是 lite-work 的天然 horizon；
    # 窗口余量按 90% 保护线折算，若上下文增速未知则忽略窗口项
    window_reserve = int(context_window * window_reserve_ratio)
    window_bound: Optional[int] = None
    if context_tokens > 0 and window_reserve > context_tokens:
        # 保守假设：剩余请求至少还能让上下文翻倍增长
        window_bound = max(1, (window_reserve - context_tokens) // max(1, context_tokens // max(1, current_turn)))
    expected = max(0, max_turns - current_turn)
    if window_bound is not None:
        expected = min(expected, window_bound)

    window_protection = context_tokens >= window_reserve
    economic = (
        saving > 0
        and expected > 0
        and breakeven is not None
        and breakeven * subsequent_margin <= expected
    )
    compact = saving > 0 and (window_protection or economic)

    if saving <= 0:
        reason = "non_positive_saving"
    elif window_protection:
        reason = "window_protection"
    elif economic:
        reason = "economic"
    elif expected <= 0:
        reason = "no_remaining_turns"
    elif breakeven is None:
        reason = "cache_ratio_unavailable"
    else:
        reason = "deferred_economic"

    return CompactionDecision(
        compact=compact,
        reason=reason,
        saving_tokens=saving,
        write_cost_tokens=write_cost,
        cache_debt_tokens=cache_debt,
        breakeven_requests=breakeven,
        expected_remaining_requests=expected,
        cache_write_read_ratio=ratio,
    )
