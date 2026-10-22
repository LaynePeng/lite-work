# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""ask_user 工具契约测试。

用户反馈的两个老问题，都在这里锁住：

1. **「ask_user 过来的问题经常不是选择题」**：工具只写「可提供选项」，
   模型于是常发一句开放式提问 → 前端只能渲染成一个空输入框。
   现在 options < 2 直接返回 [Error]，并教模型改成带选项的调用；
2. **「把多选和问多个问题误用」**：旧描述里写「多个问题可同时提出」，
   诱导模型在一轮里堆一串问题；且只有 2 个选项时标 multi_select
   等于单选（用户只会全选）。现在 multi_select 需要 ≥3 个选项。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from litework.security.question import QuestionGate
from litework.tools.ask import QuestionPlugin, make_ask_user_handler


class RecordingGate(QuestionGate):
    """记录每次 request 的参数，并可自动作答——免得 handler 真的挂起。"""

    def __init__(self, answer: str = "选好了") -> None:
        super().__init__(timeout_seconds=5)
        self.calls: List[Dict[str, Any]] = []
        self._answer = answer
        self.suspended = 0

    def request(self, question: str, options: Optional[List[str]] = None,
                multi_select: bool = False) -> asyncio.Future:
        self.calls.append({
            "question": question, "options": list(options or []),
            "multi_select": multi_select,
        })
        future = super().request(question, options, multi_select=multi_select)
        self.suspended += 1
        # 立刻resolve：模拟用户已作答
        for qid in list(self._pending):
            self.resolve(qid, self._answer)
        return future


def _run(handler, args: Dict[str, Any]) -> str:
    return asyncio.run(handler(args))


# ---------------------------------------------------------------- 必须是选择题


def test_missing_options_rejected_with_guidance():
    """没有 options：直接失败，且错误信息本身教会模型怎么改。"""
    gate = RecordingGate()
    out = _run(make_ask_user_handler(gate), {"question": "你的需求是什么？"})

    assert out.startswith("[Error]")
    assert "options" in out
    # 必须给出可照抄的示例，否则模型下次还是只发一句话
    assert "ask_user(" in out
    assert "至少 2 个" in out
    # 没进过门 → 前端不会弹出空输入框
    assert gate.calls == []


def test_single_option_rejected():
    """只有 1 个选项不算选择题（用户没得选）。"""
    gate = RecordingGate()
    out = _run(make_ask_user_handler(gate), {"question": "用哪个？", "options": ["只有一个"]})
    assert out.startswith("[Error]")
    assert gate.calls == []


def test_blank_options_treated_as_missing():
    gate = RecordingGate()
    out = _run(make_ask_user_handler(gate), {"question": "用哪个？", "options": ["  ", ""]})
    assert out.startswith("[Error]")
    assert gate.calls == []


def test_hint_points_open_questions_away_from_tool():
    """需要用户写字的情况，错误信息要明确说「别用这个工具」。"""
    gate = RecordingGate()
    out = _run(make_ask_user_handler(gate), {"question": "请提供 API Key"})
    assert "不要用这个工具" in out


def test_valid_options_reach_gate():
    """合规调用：进到门里，选项原样带出（前端据此渲染按钮）。"""
    gate = RecordingGate(answer="PostgreSQL（功能全，需运维）")
    out = _run(make_ask_user_handler(gate), {
        "question": "用哪个数据库？",
        "options": ["PostgreSQL（功能全，需运维）", "SQLite（零运维，单机）"],
    })
    assert out == "[用户回答] PostgreSQL（功能全，需运维）"
    assert gate.calls == [{
        "question": "用哪个数据库？",
        "options": ["PostgreSQL（功能全，需运维）", "SQLite（零运维，单机）"],
        "multi_select": False,
    }]


# ---------------------------------------------------------------- 多选边界


def test_multi_select_requires_three_options():
    """2 个选项标 multi_select：拒绝——那是「二选一」伪装成多选。"""
    gate = RecordingGate()
    out = _run(make_ask_user_handler(gate), {
        "question": "保留哪些方案？",
        "options": ["方案 A", "方案 B"],
        "multi_select": True,
    })
    assert out.startswith("[Error]")
    assert "至少需要 3 个选项" in out
    assert gate.calls == []


def test_multi_select_with_three_options_accepted():
    gate = RecordingGate(answer="邮件；日历")
    out = _run(make_ask_user_handler(gate), {
        "question": "要启用哪些功能？",
        "options": ["邮件", "日历", "文档"],
        "multi_select": True,
    })
    assert out == "[用户回答] 邮件；日历"
    assert gate.calls[0]["multi_select"] is True


def test_two_options_single_select_is_fine():
    """二选一用单选：完全合规（这是最常见的用法）。"""
    gate = RecordingGate(answer="方案 A")
    out = _run(make_ask_user_handler(gate), {"question": "选哪个？", "options": ["方案 A", "方案 B"]})
    assert out == "[用户回答] 方案 A"


# ---------------------------------------------------------------- 工具定义（给模型看的契约）


@pytest.fixture()
def tool_def():
    return QuestionPlugin(QuestionGate()).get_tools()[0]


def test_options_is_required_in_schema(tool_def):
    """schema 层面声明 options 必填——只在描述里说容易被忽略。"""
    assert tool_def.parameters["required"] == ["question", "options"]
    assert tool_def.parameters["properties"]["options"]["minItems"] == 2


def test_description_forbids_batching_questions(tool_def):
    """旧描述里的「多个问题可同时提出」是误用多轮的诱因，不能留。"""
    d = tool_def.description
    assert "多个问题可同时提出" not in d
    assert "一次只问一个" in d


def test_description_states_tool_is_a_choice_tool(tool_def):
    d = tool_def.description
    assert "选择题" in d
    # 明确什么情况不该用（开放式提问）
    assert "直接在回复里问" in d


def test_description_pins_multi_select_semantics(tool_def):
    d = tool_def.description
    assert "至少 3 个" in d
    assert "二选一" in d
