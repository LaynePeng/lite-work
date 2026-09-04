"""Token 估算器（对应课程第3课 TokenCounter，中文加权启发式）。"""
from __future__ import annotations

import re

from .types import Message

_CJK_RE = re.compile(r"[\u4e00-\u9fa5]")


class TokenCounter:
    """轻量 Token 计数：1 Token ≈ 4 英文字符 / 0.75 中文字符，消息结构开销另计。"""

    @staticmethod
    def count_text_tokens(text: str) -> int:
        cjk_count = len(_CJK_RE.findall(text))
        non_cjk_length = len(text) - cjk_count
        return max(1, int(cjk_count * 1.3 + non_cjk_length / 3.8))

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