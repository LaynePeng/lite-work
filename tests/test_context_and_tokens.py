# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""Token 计数 / 上下文裁剪 / JSON 容错 / 截断器单元测试（对应第2/3课增强）。"""
import os

from litework.core.context_manager import ContextManager
from litework.core.json_repair import safe_json_parse
from litework.core.token_counter import TokenCounter
from litework.core.truncator import truncate_tool_output
from litework.core.types import Message, ToolCall
from litework.llm.anthropic import AnthropicAdapter
from litework.llm.openai_compat import OpenAICompatAdapter


def test_count_text_tokens():
    assert TokenCounter.count_text_tokens("hello world") > 0
    assert TokenCounter.count_text_tokens("中文测试") >= TokenCounter.count_text_tokens("abcd")


def test_count_messages_includes_structure_overhead():
    msgs = [Message(role="user", content="hi")]
    assert TokenCounter.count_messages_tokens(msgs) > TokenCounter.count_text_tokens("hi")


def test_safe_json_parse_fences():
    ok, data, err = safe_json_parse('```json\n{"a": 1}\n```')
    assert ok and data == {"a": 1} and not err


def test_safe_json_parse_failure_reports_error():
    ok, data, err = safe_json_parse('{invalid json')
    assert not ok and data is None and "JSON Parse Failed" in err


def test_truncate_tool_output():
    # 行数未超限 → 不截断
    short = "hello\nworld"
    result = truncate_tool_output(short, max_lines=100, max_bytes=10**6)
    assert not result.truncated
    assert result.content == short

    # 超行数 → 截断，保留头部
    out = "\n".join(f"line_{i}" for i in range(5000))
    result = truncate_tool_output(out, max_lines=50, max_bytes=10**6)
    assert result.truncated
    assert result.content.startswith("line_0")
    assert "lines truncated" in result.content
    assert result.output_path is None  # 无 output_dir 不落盘

    # 超字节 → 截断
    result = truncate_tool_output("x" * 10000, max_lines=10**6, max_bytes=1000)
    assert result.truncated
    assert "bytes truncated" in result.content

    # 短文本不截断
    result = truncate_tool_output("hello")
    assert not result.truncated

    # 带 output_dir 落盘
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        result = truncate_tool_output("\n".join(f"line_{i}" for i in range(5000)),
                                      max_lines=50, max_bytes=10**6, output_dir=tmp)
        assert result.truncated
        assert result.output_path is not None
        assert os.path.exists(result.output_path)
        assert os.path.getsize(result.output_path) > 0


def _make_chain() -> list:
    return [
        Message(role="system", content="sys" * 300),
        Message(role="user", content="hello"),
        Message(role="assistant", content=None, tool_calls=[
            ToolCall(id="c1", name="read_file", arguments='{"filePath":"a.ts"}')]),
        Message(role="tool", tool_call_id="c1", content="result" * 50),
        Message(role="user", content="follow up"),
        Message(role="assistant", content="final answer" * 10),
    ]


def test_prune_keeps_system_and_tool_pair_atomic():
    messages = _make_chain()
    # 小预算强制裁剪
    cm = ContextManager(max_allowed_tokens=100)
    pruned = cm.prune_messages(messages)

    assert pruned[0].role == "system"
    # 任何 assistant(tool_calls) 与其 tool 结果要么都在要么都不在
    i = 1
    while i < len(pruned):
        m = pruned[i]
        if m.role == "assistant" and m.tool_calls:
            assert i + 1 < len(pruned)
            assert pruned[i + 1].role == "tool"
            assert pruned[i + 1].tool_call_id == m.tool_calls[0].id
            i += 2
        else:
            i += 1
    # 裁剪后消息数必须少于原始
    assert len(pruned) < len(messages)
    assert TokenCounter.count_messages_tokens(pruned) <= TokenCounter.count_messages_tokens(messages)


def test_prune_noop_within_budget():
    messages = _make_chain()
    cm = ContextManager(max_allowed_tokens=10**6)
    assert cm.prune_messages(messages) == messages


# ---------------------------------------------------------------- 策略 B


def _big_tool_turn(user_text: str, call_id: str, final_text: str) -> list:
    return [
        Message(role="user", content=user_text),
        Message(role="assistant", content=None, tool_calls=[
            ToolCall(id=call_id, name="read_file", arguments='{"filePath":"a.ts"}')]),
        Message(role="tool", tool_call_id=call_id, content="result " * 200),
        Message(role="assistant", content=final_text),
    ]


def _three_turn_chain() -> list:
    messages = [Message(role="system", content="sys" * 300)]
    messages += _big_tool_turn("问题一", "c1", "回答一")
    messages += _big_tool_turn("问题二", "c2", "回答二")
    messages += _big_tool_turn("问题三", "c3", "回答三")
    return messages


def test_prune_stage1_drops_old_tool_pairs_keeps_backbone():
    """策略 B：先删更早轮次的工具细节（原子对），保留问题+最终回答；
    最近 keep_recent_full_turns 轮的完整细节保留。"""
    messages = _three_turn_chain()  # 总 1443 tokens
    cm = ContextManager(max_allowed_tokens=1200, keep_recent_full_turns=2)
    pruned = cm.prune_messages(messages)

    contents = {m.content for m in pruned}
    kept_call_ids = {
        c.id for m in pruned if m.role == "assistant" and m.tool_calls for c in m.tool_calls
    }
    tool_call_ids = {m.tool_call_id for m in pruned if m.role == "tool"}

    # system 永远在
    assert pruned[0].role == "system"
    # 最老一轮（问题一）：user 与最终回答保留，工具对（assistant tc + tool）被删
    assert "问题一" in contents and "回答一" in contents
    assert "c1" not in kept_call_ids and "c1" not in tool_call_ids
    # 最近两轮的完整细节保留（含工具对）
    assert "c2" in kept_call_ids and "c3" in kept_call_ids
    assert "c2" in tool_call_ids and "c3" in tool_call_ids
    assert "问题二" in contents and "回答二" in contents
    assert "问题三" in contents and "回答三" in contents
    # 原子对约束：每个 assistant(tool_calls) 的 tool 都跟着
    assert len(kept_call_ids) == len(tool_call_ids)
    # 裁剪后有压缩统计，且不超预算（核算含 system）
    assert cm.last_prune["compressed"] is True
    assert cm.last_prune["removed_tokens"] > 0
    assert TokenCounter.count_messages_tokens(pruned) <= 1200


def test_prune_hard_cap_override():
    messages = _three_turn_chain()
    cm = ContextManager(max_allowed_tokens=10**6)  # 预算巨大，不触发
    pruned = cm.prune_messages(messages, hard_cap=1200)
    assert len(pruned) < len(messages)
    assert cm.last_prune["compressed"] is True


def test_prune_stage2_drops_oldest_turn():
    """阶段1 删完工具对仍超预算 → 从最老整轮删除（最新一轮永不删）。"""
    messages = _three_turn_chain()
    cm = ContextManager(max_allowed_tokens=500, keep_recent_full_turns=1)
    pruned = cm.prune_messages(messages)

    contents = {m.content for m in pruned}
    assert pruned[0].role == "system"
    assert "问题一" not in contents and "回答一" not in contents
    assert "问题二" not in contents and "回答二" not in contents
    assert "问题三" in contents and "回答三" in contents
    # 不能出现孤立 tool 消息（整轮删不破坏原子对）
    tool_count = sum(1 for m in pruned if m.role == "tool")
    tc_count = sum(1 for m in pruned if m.role == "assistant" and m.tool_calls)
    assert tool_count == tc_count
    assert TokenCounter.count_messages_tokens(pruned) <= TokenCounter.count_messages_tokens(messages)


def test_split_for_compaction_head_tail():
    """opencode 风格拆解：head 是更早轮次（待摘要），tail 是预算内最近轮次（原样保留）。"""
    messages = _three_turn_chain()  # 总 1443 tokens
    cm = ContextManager(max_allowed_tokens=700, keep_recent_full_turns=2)
    plan = cm.split_for_compaction(messages)
    assert plan is not None
    head, tail, head_tokens = plan

    # system 不进 head/tail（由调用方重组）
    assert all(m.role != "system" for m in head + tail)
    # head 是更早的轮次（问题一），tail 保留最新（问题三）
    head_contents = {m.content for m in head}
    tail_contents = {m.content for m in tail}
    assert "问题一" in head_contents
    assert "问题三" in tail_contents
    assert head_tokens > 0
    # tail + system 必须在预算内（system 由调用方补回，此处核算用占位）
    sys_placeholder = Message(role="system", content="sys" * 300)
    assert TokenCounter.count_message_tokens(sys_placeholder) + TokenCounter.count_messages_tokens(tail) <= 700
    # tail 必须是完整轮次边界（不能从工具对中间切断）
    roles = [m.role for m in tail]
    assert roles[0] == "user"


def test_split_for_compaction_within_budget_none():
    messages = _three_turn_chain()
    cm = ContextManager(max_allowed_tokens=10**6)
    assert cm.split_for_compaction(messages) is None


def test_split_for_compaction_keeps_at_least_last_turn():
    """即使最新一轮也超预算，仍保留最新轮（opencode tail fallback）。"""
    messages = _three_turn_chain()
    cm = ContextManager(max_allowed_tokens=100)
    plan = cm.split_for_compaction(messages)
    assert plan is not None
    head, tail, _ = plan
    assert any(m.content == "问题三" for m in tail)
    assert all(m.content != "问题三" for m in head)


# ---------------------------------------------------------------- usage 解析


def test_openai_extract_usage():
    # 末帧 usage（choices 为空）
    usage = OpenAICompatAdapter._extract_usage({
        "id": "x", "choices": [], "usage": {
            "prompt_tokens": 100, "completion_tokens": 20,
            "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20,
        },
    })
    assert usage == {"prompt_tokens": 100, "completion_tokens": 20,
                     "prompt_cache_hit_tokens": 80}
    # 普通内容帧无 usage
    assert OpenAICompatAdapter._extract_usage({"choices": [{"delta": {"content": "hi"}}]}) is None
    # usage 字段缺失类型异常时不崩溃
    assert OpenAICompatAdapter._extract_usage({"usage": {}}) is None


def test_anthropic_usage_extract():
    start = AnthropicAdapter._start_usage({
        "type": "message_start", "message": {"usage": {"input_tokens": 500, "cache_read_input_tokens": 300}},
    })
    assert start == {"prompt_tokens": 500, "completion_tokens": 0, "prompt_cache_hit_tokens": 300}
    delta = AnthropicAdapter._delta_usage({
        "type": "message_delta", "usage": {"output_tokens": 42, "cache_read_input_tokens": 10},
    })
    assert delta == {"output_tokens": 42, "cache_read_input_tokens": 10}
    assert AnthropicAdapter._start_usage({"type": "x"}) is None
    assert AnthropicAdapter._delta_usage({"type": "x"}) is None

# ------------------------------------------------- 压缩后必须真的落进预算
#
# 回归的是线上最要命的一条：cap 943718 却长期停在 1.5M，日志反复
# 「压缩 1 条旧消息」。原因不是摘要失败，而是 split_for_compaction 只按
# 「整轮」保留 tail，而**单个轮次自身就能超预算**（一轮里读几个大文件）。
# 于是 head 被摘要掉、tail 依然超 cap → 压完还是超 → 下一轮再压一次 head。
# 这个循环每轮都在花 token，却永远收敛不了。


def _msgs_with_huge_last_turn(payload_chars=8000):
    from litework.core.types import ToolCall

    return [
        Message(role="system", content="sys"),
        Message(role="user", content="很早的问题"),
        Message(role="assistant", content="很早的回答"),
        Message(role="user", content="当前问题"),
        Message(role="assistant", content=None,
                tool_calls=[ToolCall(id="c9", name="read", arguments="{}")]),
        Message(role="tool", content="X" * payload_chars, tool_call_id="c9"),
        Message(role="assistant", content="当前回答"),
    ]


def test_compaction_tail_fits_budget():
    """最新一轮自身超预算时，压缩结果必须落进 cap（而不是原样留着继续超）。"""
    from litework.core.token_counter import TokenCounter

    cm = ContextManager(max_allowed_tokens=1000)
    messages = _msgs_with_huge_last_turn()
    cap = 1000

    plan = cm.split_for_compaction(messages, hard_cap=cap)
    assert plan is not None
    head, tail, head_tokens = plan

    compacted = [messages[0], Message(role="user", content="[历史摘要] 摘要")] + tail
    after = TokenCounter.count_messages_tokens(compacted)
    assert after <= cap, f"压缩后仍超预算：{after} > {cap}（这正是历史永不收敛的根因）"
    assert head_tokens > 0


def test_compaction_never_orphans_tool_messages():
    """按消息粒度切 tail 时绝不能劈开 assistant(tool_calls)+tool 原子对。"""
    from litework.core.types import ToolCall

    cm = ContextManager(max_allowed_tokens=500)
    messages = [
        Message(role="system", content="sys"),
        Message(role="user", content="q1"),
        Message(role="assistant", content=None,
                tool_calls=[ToolCall(id="a", name="read", arguments="{}")]),
        Message(role="tool", content="A" * 3000, tool_call_id="a"),
        Message(role="assistant", content="a1"),
        Message(role="user", content="q2"),
        Message(role="assistant", content=None,
                tool_calls=[ToolCall(id="b", name="read", arguments="{}")]),
        Message(role="tool", content="B" * 3000, tool_call_id="b"),
        Message(role="assistant", content="a2"),
    ]

    for cap in (500, 800, 1000, 1500, 2000):
        plan = cm.split_for_compaction(messages, hard_cap=cap)
        if plan is None:
            continue
        _head, tail, _ht = plan
        # tail 里每个 tool 消息都必须能找到回应它的 assistant
        for i, m in enumerate(tail):
            if m.role != "tool":
                continue
            owners = [
                t for t in tail[:i]
                if t.role == "assistant" and t.tool_calls
                and any(c.id == m.tool_call_id for c in t.tool_calls)
            ]
            assert owners, f"cap={cap}: tail 出现无主 tool 消息（API 会 400）"


def test_compaction_converges_after_repeated_rounds():
    """反复压缩必须收敛：每轮压完都落在 cap 内，不会越压越超。"""
    from litework.core.token_counter import TokenCounter

    cm = ContextManager(max_allowed_tokens=1000)
    cap = 1000
    messages = _msgs_with_huge_last_turn()

    # 模拟连续 3 轮：每轮都按 split_for_compaction 压缩，再追加新内容
    for round_no in range(3):
        plan = cm.split_for_compaction(messages, hard_cap=cap)
        if plan is None:
            break
        head, tail, _ht = plan
        messages = ([messages[0]]
                    + [Message(role="user", content=f"[历史摘要] 第{round_no}次")]
                    + tail)
        assert TokenCounter.count_messages_tokens(messages) <= cap, (
            f"第 {round_no} 轮压缩后仍超预算："
            f"{TokenCounter.count_messages_tokens(messages)} > {cap}"
        )
        # 追加一轮新对话（模拟任务继续）
        messages.append(Message(role="user", content=f"新问题{round_no}"))
        messages.append(Message(role="assistant", content=f"新回答{round_no}"))


def test_compaction_single_turn_session_still_compacts():
    """单轮长会话也必须能压缩（自动压缩失效的真实形态）。

    线上最常见的历史形态**不是**很多轮问答，而是「用户只发一条指令，
    agent 连跑上千次工具调用」。此时 `_turn_ranges` 只切出 1 个轮次，
    `tail_start` 初值 = 0 → 旧实现直接 `return None` → 摘要压缩**从不触发**，
    只剩 prune 免费裁剪兜底（而 prune 不写回 messages，下一轮又从完整历史
    重算）。这就是「超预算 282 次、摘要只成功 22 次」的直接原因。
    """
    from litework.core.types import ToolCall
    from litework.core.token_counter import TokenCounter

    cm = ContextManager(max_allowed_tokens=943718)
    messages = [Message(role="system", content="sys")]
    messages.append(Message(role="user", content="帮我重构整个项目"))
    for i in range(700):
        messages.append(Message(role="assistant", content=None,
                                tool_calls=[ToolCall(id=f"c{i}", name="read", arguments="{}")]))
        messages.append(Message(role="tool", content="X" * 6000, tool_call_id=f"c{i}"))
        messages.append(Message(role="assistant", content=f"a{i}"))

    cap = 943718
    assert TokenCounter.count_messages_tokens(messages) > cap

    # 前提确认：这种形态确实只切出 1 个轮次
    assert len(ContextManager._turn_ranges(messages[1:])) == 1

    plan = cm.split_for_compaction(messages, hard_cap=cap)
    assert plan is not None, "单轮长会话必须能压缩，否则摘要永不触发"
    head, tail, head_tokens = plan
    assert head_tokens > 0
    assert len(head) > 0 and len(tail) > 0

    after = TokenCounter.count_messages_tokens(
        [messages[0], Message(role="user", content="[历史摘要] x")] + tail)
    assert after <= cap, f"压缩后仍超预算：{after} > {cap}"
