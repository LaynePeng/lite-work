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