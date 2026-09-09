# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""上下文滑动窗口裁剪（对应课程第3课 ContextManager，策略 B 增强）。

关键约束（绝对不能违反）：
1. Index 0 的 System Prompt 永远不能删；
2. assistant(tool_calls) 与其后的 tool(result) 必须作为原子对存在或一起被裁剪，
   否则 LLM API 会直接报 HTTP 400。

策略 B（面向多轮对话 + 节省 token）：
- 保留最近 keep_recent_full_turns 轮的完整细节（含工具调用），保证当前任务连续性；
- 超预算时先压缩更早轮次的「工具细节」（整对删除 assistant(tool_calls)+tool），
  只保留该轮的 user 问题与最终回答（对话主干）；
- 还不够再按轮从最老开始整轮删除。
"""
from __future__ import annotations

import logging
import uuid
from typing import Dict, List, Optional, Tuple

from .token_counter import TokenCounter
from .types import Message

logger = logging.getLogger("litework.context")


def repair_tool_call_pairs(messages: List[Message]) -> List[Message]:
    """修复消息链中的工具调用原子对（OpenAI 兼容 API 硬约束）。

    约束：assistant(tool_calls) 之后必须为每个 tool_call_id 跟一条 tool 消息，
    否则 API 返回 HTTP 400。修复策略：
    - 完整配对：原样保留；
    - assistant(tool_calls) 缺少部分/全部 tool 结果 → 整条丢弃（连同其后的孤儿 tool 消息）；
    - 无主的 tool 消息（前面没有匹配的 assistant tool_calls）→ 丢弃；
    - 空 id 历史（旧版本适配器 bug 残留）：assistant 与 tool 消息都缺 id 时按顺序配对，
      并为双方补齐一致的合成 id —— 否则 API 无法匹配，照样 400。

    典型触发场景：任务被停止/中止时落盘了不完整历史，续聊恢复后发送被拒。
    """
    repaired: List[Message] = []
    i = 0
    n = len(messages)
    while i < n:
        m = messages[i]
        if m.role == "assistant" and m.tool_calls:
            calls = list(m.tool_calls)
            has_empty = any(not c.id for c in calls)
            ids = {c.id for c in calls if c.id}
            j = i + 1
            matched: List[Message] = []
            while j < n and messages[j].role == "tool":
                tid = messages[j].tool_call_id
                if has_empty:
                    # 空 id 链：按顺序与空 id 的 tool 消息配对
                    if tid:
                        break
                elif tid not in ids:
                    break
                matched.append(messages[j])
                j += 1
            if len(matched) >= len(calls):
                if has_empty:
                    # 补齐一致的合成 id（按位置配对），超出部分视为孤儿丢弃
                    matched = matched[: len(calls)]
                    for c in calls:
                        if not c.id:
                            c.id = f"call_{uuid.uuid4().hex[:12]}"
                    for k, t in enumerate(matched):
                        if not t.tool_call_id:
                            t.tool_call_id = calls[k].id
                repaired.append(m)
                repaired.extend(matched)
            else:
                logger.warning(
                    "[Repair] 丢弃不完整的 tool_calls 消息（%s 个 tool_call_id，仅 %s 条 tool 结果）",
                    len(calls), len(matched),
                )
            i = j
            continue
        if m.role == "tool":
            # 无主的 tool 消息（缺少前置 assistant tool_calls）
            logger.warning("[Repair] 丢弃无主的 tool 消息: %s", m.tool_call_id)
            i += 1
            continue
        repaired.append(m)
        i += 1
    return repaired


class ContextManager:
    def __init__(self, max_allowed_tokens: int = 48000, keep_recent_full_turns: int = 2) -> None:
        self.max_allowed_tokens = max_allowed_tokens
        self.keep_recent_full_turns = max(1, keep_recent_full_turns)
        # 最近一次裁剪的统计（供 UI「上下文情况」展示）
        self.last_prune: Dict[str, object] = {
            "compressed": False,
            "removed_tokens": 0,
            "stage": None,
        }

    def prune_messages(
        self, messages: List[Message], hard_cap: Optional[int] = None
    ) -> List[Message]:
        cap = hard_cap or self.max_allowed_tokens
        self.last_prune = {"compressed": False, "removed_tokens": 0, "stage": None}

        current = TokenCounter.count_messages_tokens(messages)
        if current <= cap:
            return messages

        logger.warning(
            "[ContextManager] Exceeded token budget (%s/%s). Pruning...",
            current,
            cap,
        )

        system, body = self._split_body(messages)
        # 预算核算含 system（索引 0 为 system，永不删除）
        sys_tokens = TokenCounter.count_message_tokens(system) if system else 0
        tokens = [sys_tokens] + [TokenCounter.count_message_tokens(m) for m in body]
        removed = [False] * (len(body) + 1)
        total = sum(tokens)

        # 阶段1：压缩更早轮次的工具细节（assistant(tool_calls)+tool 原子对）
        if total > cap:
            droppable = self._stage1_candidates(body)
            for ai, tool_idxs in droppable:
                if total <= cap:
                    break
                for idx in [ai + 1] + [t + 1 for t in tool_idxs]:
                    if not removed[idx]:
                        removed[idx] = True
                        total -= tokens[idx]
                self.last_prune["stage"] = "stage1"

        # 阶段2：整轮删除最老轮次（保留最新一轮）
        if total > cap:
            for start, end in self._oldest_turn_ranges(body):
                if total <= cap:
                    break
                for idx in range(start + 1, end + 1):
                    if not removed[idx]:
                        removed[idx] = True
                        total -= tokens[idx]
                self.last_prune["stage"] = "stage2"

        result = ([system] if system else []) + [
            m for m, r in zip(body, removed[1:]) if not r
        ]
        # 兜底：body 被删空时保留 system + 最新一轮
        if system is not None and len(result) == 1:
            newest = self._newest_turn(body)
            result = [system] + body[newest[0]:newest[1]]

        removed_tokens = TokenCounter.count_messages_tokens(messages) - TokenCounter.count_messages_tokens(result)
        self.last_prune.update(
            compressed=removed_tokens > 0,
            removed_tokens=max(0, removed_tokens),
        )
        logger.info(
            "[ContextManager] Pruned to %s messages (saved %s tokens, stage=%s).",
            len(result),
            self.last_prune["removed_tokens"],
            self.last_prune["stage"],
        )
        return result

    def split_for_compaction(
        self, messages: List[Message], hard_cap: Optional[int] = None
    ) -> Optional[Tuple[List[Message], List[Message], int]]:
        """opencode 风格：把超预算历史拆成 (head 待摘要, tail 原样保留, head_tokens)。

        从最新轮次往前保留 tail（预算内、至少一轮），head 为更早轮次。
        未超预算或 head 为空返回 None（调用方回退旧裁剪策略）。
        """
        cap = hard_cap or self.max_allowed_tokens
        system, body = self._split_body(messages)
        if not body:
            return None
        sys_tokens = TokenCounter.count_message_tokens(system) if system else 0
        turns = self._turn_ranges(body)
        sizes = [
            sum(TokenCounter.count_message_tokens(m) for m in body[start:end])
            for start, end in turns
        ]
        if sys_tokens + sum(sizes) <= cap:
            return None
        tail_start = turns[-1][0]
        acc = sys_tokens
        for i in range(len(turns) - 1, -1, -1):
            start, end = turns[i]
            if acc + sizes[i] <= cap:
                acc += sizes[i]
                tail_start = start
            else:
                break
        if tail_start <= 0:
            return None
        head = body[:tail_start]
        head_tokens = sum(TokenCounter.count_message_tokens(m) for m in head)
        if head_tokens <= 0:
            return None
        return head, body[tail_start:], head_tokens

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _split_body(messages: List[Message]):
        """拆出 system 消息与对话正文。"""
        system = messages[0] if messages and messages[0].role == "system" else None
        body = messages[1:] if system else list(messages)
        return system, body

    @staticmethod
    def _turn_ranges(body: List[Message]) -> List[tuple]:
        """把正文按 user 消息切成轮次，返回 [(start, end_excl), ...]。"""
        turns: List[tuple] = []
        start: Optional[int] = None
        for i, m in enumerate(body):
            if m.role == "user":
                if start is not None:
                    turns.append((start, i))
                start = i
        if start is not None:
            turns.append((start, len(body)))
        # 异常数据（正文开头不是 user）：并入第一个轮次
        if turns:
            if turns[0][0] != 0:
                turns[0] = (0, turns[0][1])
        elif body:
            turns = [(0, len(body))]
        return turns

    def _stage1_candidates(self, body: List[Message]) -> List[tuple]:
        """返回可删除的「assistant(tool_calls)+tool」对位置，最老在前；跳过最近 K 轮。"""
        turns = self._turn_ranges(body)
        keep_from = max(0, len(turns) - self.keep_recent_full_turns)
        candidates: List[tuple] = []
        for turn_idx, (start, end) in enumerate(turns):
            if turn_idx >= keep_from:
                continue
            i = start
            while i < end:
                m = body[i]
                if m.role == "assistant" and m.tool_calls:
                    ids = {c.id for c in m.tool_calls}
                    tool_idxs: List[int] = []
                    j = i + 1
                    while j < end and body[j].role == "tool" and body[j].tool_call_id in ids:
                        tool_idxs.append(j)
                        j += 1
                    candidates.append((i, tool_idxs))
                    i = j
                else:
                    i += 1
        return candidates

    def _oldest_turn_ranges(self, body: List[Message]) -> List[tuple]:
        """整轮删除顺序：最老优先，最新一轮永不删。"""
        turns = self._turn_ranges(body)
        if len(turns) <= 1:
            return []
        return turns[:-1]

    def _newest_turn(self, body: List[Message]) -> tuple:
        turns = self._turn_ranges(body)
        return turns[-1] if turns else (0, len(body))