# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""Token 估算器（对应课程第3课 TokenCounter，中文加权启发式）。"""
from __future__ import annotations

import re

from .types import Message

_CJK_RE = re.compile(r"[\u4e00-\u9fa5]")


class TokenCounter:
    """轻量 Token 计数：1 Token ≈ 4 英文字符 / 0.75 中文字符，消息结构开销另计。"""

    @staticmethod
    def cjk_count(text: str) -> int:
        """文本中的 CJK 字符数。

        单独暴露是为了让流式增量计量（StreamMeter）复用同一口径——它只对
        每个增量块做一次小正则，不对全文反复计数。
        """
        return len(_CJK_RE.findall(text))

    @staticmethod
    def count_tokens_by_counts(cjk_count: int, non_cjk_length: int, *, minimum: int = 1) -> int:
        """按「CJK 字数 + 其余字符数」换算 token——**全项目唯一的估算公式**。

        minimum=1：单条文本/消息（空文本也按 1 token 计，保持既有行为）；
        minimum=0：流式增量累计（还没吐字时就是 0）。
        """
        return max(minimum, int(cjk_count * 1.3 + non_cjk_length / 3.8))

    @staticmethod
    def count_text_tokens(text: str) -> int:
        cjk_count = TokenCounter.cjk_count(text)
        return TokenCounter.count_tokens_by_counts(cjk_count, len(text) - cjk_count)

    @classmethod
    def count_message_tokens(cls, message: Message) -> int:
        num = 4  # role / 格式基础开销
        if message.content:
            num += cls.count_text_tokens(message.content)
        if message.tool_calls:
            for call in message.tool_calls:
                num += cls.count_text_tokens(call.name)
                num += cls.count_text_tokens(call.arguments)
                num += 6
        if message.tool_call_id:
            num += cls.count_text_tokens(message.tool_call_id)
        return num

    @classmethod
    def count_messages_tokens(cls, messages) -> int:
        return sum(cls.count_message_tokens(m) for m in messages) + 3