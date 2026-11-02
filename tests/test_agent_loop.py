# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""AgentLoop 主循环测试（第16课（实战 AgentLoop）+ 第2课增强：自愈/死循环/截断/停止/会话落盘）。"""
import asyncio
import os

import pytest

from litework.core.agent_loop import AgentLoop
from litework.core.kernel import Kernel
from litework.core.session_store import SessionStore
from litework.core.types import Message
from litework.llm import LLMError
from litework.tools.registry import ToolRegistry
from tests.conftest import MockLLMAdapter, tool_call

SYSTEM_PROMPT = "你是测试 Agent。"


def _make_loop(tmp_path, adapter, registry=None, **kwargs):
    kernel = Kernel("test-session")
    store = SessionStore(str(tmp_path / "sessions"))
    registry = registry or ToolRegistry()
    loop = AgentLoop(kernel=kernel, adapter=adapter, registry=registry,
                     session_store=store, max_steps=10, **kwargs)
    loop.workspace = str(tmp_path)
    return loop, kernel, store


async def test_basic_think_act_observe(tmp_path):
    """写文件 → 回填 → 终答，验证完整闭环与落盘。"""
    registry = ToolRegistry()

    async def write(args):
        p = os.path.join(str(tmp_path), args["filePath"])
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(args["content"])
        return "[Success] 已写入"

    registry.register("write_file", "写文件", {"type": "object"}, write)

    adapter = MockLLMAdapter([
        ("", [tool_call("write_file", '{"filePath":"a.txt","content":"hi"}')]),
        ("任务完成。", []),
    ])
    loop, kernel, store = _make_loop(tmp_path, adapter, registry)

    events = []
    kernel.events.on("message:added", lambda d: events.append(d["message"]["role"]))

    result, stats = await loop.run_task("创建 a.txt", system_prompt=SYSTEM_PROMPT)

    assert result == "任务完成。"
    assert os.path.exists(tmp_path / "a.txt")
    assert stats["tool_calls"] == 1
    assert events.count("user") >= 1
    assert events.count("assistant") >= 2
    assert events.count("tool") == 1
    # 会话落盘
    snap = store.load("test-session")
    assert snap is not None and len(snap.messages) >= 5


async def test_loop_detection(tmp_path):
    """只读工具连续 6 次相同工具+参数 → 触发死循环防御并注入错误消息。

    （阈值分级：只读工具放宽到 6 次避免误杀复查；写类工具仍为 3 次）
    """
    registry = ToolRegistry()
    registry.register("read_file", "读文件", {"type": "object"},
                      lambda args: "content")

    adapter = MockLLMAdapter([
        ("", [tool_call("read_file", '{"filePath":"x.ts"}')]),
        ("", [tool_call("read_file", '{"filePath":"x.ts"}')]),
        ("", [tool_call("read_file", '{"filePath":"x.ts"}')]),
        ("", [tool_call("read_file", '{"filePath":"x.ts"}')]),
        ("", [tool_call("read_file", '{"filePath":"x.ts"}')]),
        ("", [tool_call("read_file", '{"filePath":"x.ts"}')]),
        ("好吧，我换策略。", []),
    ])
    loop, kernel, _ = _make_loop(tmp_path, adapter, registry)

    result, stats = await loop.run_task("读文件", system_prompt=SYSTEM_PROMPT)
    tool_msgs = [m for m in kernel.ctx.messages if m.role == "tool"]
    assert any("死循环" in m.content for m in tool_msgs)
    assert result == "好吧，我换策略。"


async def test_loop_detection_strict_for_write_tools(tmp_path):
    """写类工具保持严格阈值：连续 3 次相同写操作 → 触发死循环防御。"""
    registry = ToolRegistry()
    registry.register("write_file", "写文件", {"type": "object"},
                      lambda args: "ok")

    adapter = MockLLMAdapter([
        ("", [tool_call("write_file", '{"filePath":"x.ts","content":"a"}')]),
        ("", [tool_call("write_file", '{"filePath":"x.ts","content":"a"}')]),
        ("", [tool_call("write_file", '{"filePath":"x.ts","content":"a"}')]),
        ("好吧，我换策略。", []),
    ])
    loop, kernel, _ = _make_loop(tmp_path, adapter, registry)

    result, stats = await loop.run_task("写文件", system_prompt=SYSTEM_PROMPT)
    tool_msgs = [m for m in kernel.ctx.messages if m.role == "tool"]
    assert any("死循环" in m.content for m in tool_msgs)
    assert result == "好吧，我换策略。"


async def test_loop_detection_sliding_window(tmp_path):
    """窄行窗口滑动重读同一文件（参数每次都不同）→ 精确哈希拦不住，冗余重读检测兜底。

    事故复盘：read_file 1-6、5-7、6-8、7-8……绕过旧防御空转十几轮。
    """
    registry = ToolRegistry()
    registry.register("read_file", "读文件", {"type": "object"},
                      lambda args: "content")

    reads = [
        '{"filePath":"recipe.md"}',                      # 整读（不冗余）
        '{"filePath":"recipe.md","startLine":1,"endLine":6}',
        '{"filePath":"recipe.md","startLine":5,"endLine":7}',   # 冗余 1
        '{"filePath":"recipe.md","startLine":6,"endLine":8}',   # 冗余 2
        '{"filePath":"recipe.md","startLine":7,"endLine":8}',   # 冗余 3
        '{"filePath":"recipe.md","startLine":6,"endLine":9}',   # 冗余 4
        '{"filePath":"recipe.md","startLine":2,"endLine":3}',   # 冗余 5 → 触发
    ]
    adapter = MockLLMAdapter(
        [("", [tool_call("read_file", r)]) for r in reads]
        + [("收到，直接执行编辑。", [])]
    )
    loop, kernel, _ = _make_loop(tmp_path, adapter, registry)

    result, _ = await loop.run_task("改 recipe.md", system_prompt=SYSTEM_PROMPT)
    tool_msgs = [m for m in kernel.ctx.messages if m.role == "tool"]
    assert any("死循环" in m.content and "重复读取" in m.content for m in tool_msgs)
    assert result == "收到，直接执行编辑。"


async def test_no_loop_for_sequential_chunk_reads(tmp_path):
    """顺序分块读大文件（窗口互不重叠）→ 冗余计数不增长，不误杀。"""
    registry = ToolRegistry()
    registry.register("read_file", "读文件", {"type": "object"},
                      lambda args: "content")

    reads = [
        '{"filePath":"big.ts","startLine":1,"endLine":50}',
        '{"filePath":"big.ts","startLine":51,"endLine":100}',
        '{"filePath":"big.ts","startLine":101,"endLine":150}',
        '{"filePath":"big.ts","startLine":151,"endLine":200}',
        '{"filePath":"big.ts","startLine":201,"endLine":250}',
        '{"filePath":"big.ts","startLine":251,"endLine":300}',
    ]
    adapter = MockLLMAdapter(
        [("", [tool_call("read_file", r)]) for r in reads]
        + [("读完了。", [])]
    )
    loop, kernel, _ = _make_loop(tmp_path, adapter, registry)

    result, _ = await loop.run_task("读大文件", system_prompt=SYSTEM_PROMPT)
    tool_msgs = [m for m in kernel.ctx.messages if m.role == "tool"]
    assert not any("死循环" in m.content for m in tool_msgs)
    assert result == "读完了。"


def test_state_tracker_write_invalidates_coverage():
    """写类调用清空该文件已读覆盖：写后重读是合法校验，冗余计数从零重算。

    判别点：若无清空逻辑，写之后的第一次重读就是第 5 次冗余 → 必触发。
    """
    from litework.core.state_tracker import AgentStateTracker

    st = AgentStateTracker()
    # 冗余累计到 4（差一步触发）
    assert not st.register_and_check_loop(
        "read_file", '{"filePath":"a.ts","startLine":1,"endLine":10}')
    for lo, hi in [(5, 8), (6, 9), (4, 7), (2, 3)]:
        assert not st.register_and_check_loop(
            "read_file", f'{{"filePath":"a.ts","startLine":{lo},"endLine":{hi}}}')

    # 写文件 → 覆盖清空
    assert not st.register_and_check_loop(
        "write_file", '{"filePath":"a.ts","content":"x"}')

    # 写后的第一次重读不触发（覆盖已清零，冗余计数从头算）
    assert not st.register_and_check_loop(
        "read_file", '{"filePath":"a.ts","startLine":5,"endLine":8}')

    # 对照：没有写操作打断时，第 5 次冗余即触发
    st2 = AgentStateTracker()
    st2.register_and_check_loop("read_file", '{"filePath":"b.ts","startLine":1,"endLine":10}')
    for lo, hi in [(5, 8), (6, 9), (4, 7), (2, 3)]:
        assert not st2.register_and_check_loop(
            "read_file", f'{{"filePath":"b.ts","startLine":{lo},"endLine":{hi}}}')
    assert st2.register_and_check_loop(
        "read_file", '{"filePath":"b.ts","startLine":5,"endLine":8}')


async def test_json_self_heal(tmp_path):
    """非法 JSON 参数 → 回填错误让 LLM 自愈，不 crash。"""
    registry = ToolRegistry()
    registry.register("read_file", "读文件", {"type": "object"}, lambda args: "ok")

    adapter = MockLLMAdapter([
        ("", [tool_call("read_file", "{not valid json")]),
        ("修正了。", []),
    ])
    loop, kernel, _ = _make_loop(tmp_path, adapter, registry)

    result, _ = await loop.run_task("读文件", system_prompt=SYSTEM_PROMPT)
    tool_msgs = [m for m in kernel.ctx.messages if m.role == "tool"]
    assert any("JSON Parse Failed" in m.content for m in tool_msgs)
    assert result == "修正了。"


async def test_output_truncation(tmp_path):
    """工具输出超长 → 截断后进入消息链。"""
    registry = ToolRegistry()
    registry.register("big_tool", "大输出", {"type": "object"},
                      lambda args: "A" * 60000)

    adapter = MockLLMAdapter([
        ("", [tool_call("big_tool", "{}")]),
        ("完成", []),
    ])
    loop, kernel, _ = _make_loop(tmp_path, adapter, registry)

    await loop.run_task("跑大输出", system_prompt=SYSTEM_PROMPT)
    tool_msgs = [m for m in kernel.ctx.messages if m.role == "tool"]
    assert tool_msgs and len(tool_msgs[0].content) < 50000
    assert "truncated" in tool_msgs[0].content


async def test_abort_stop(tmp_path):
    """停止信号在工具批次边界生效（协作式中断）。"""
    registry = ToolRegistry()
    loop_ref = {}

    async def trigger(args):
        loop_ref["loop"].abort_event.set()  # 第一个工具触发停止
        return "ok"

    registry.register("trigger_stop", "触发停止", {"type": "object"}, trigger)
    registry.register("other", "其他工具", {"type": "object"}, lambda args: "done")

    adapter = MockLLMAdapter([
        ("", [tool_call("trigger_stop", "{}"), tool_call("other", "{}")]),
        ("完成", []),
    ])
    loop, kernel, _ = _make_loop(tmp_path, adapter, registry)
    loop.abort_event = asyncio.Event()
    loop_ref["loop"] = loop

    result, _ = await loop.run_task("跑触发停止", system_prompt=SYSTEM_PROMPT)
    assert "Stopped" in result
    # 第二个工具不应被执行
    tool_msgs = [m for m in kernel.ctx.messages if m.role == "tool"]
    assert all("trigger_stop" in m.content or m.name == "trigger_stop" for m in tool_msgs)


async def test_before_tool_cancel(tmp_path):
    """beforeTool 中间件可阻断工具执行。"""
    registry = ToolRegistry()
    registry.register("danger", "危险工具", {"type": "object"},
                      lambda args: "[Success] 不该执行")

    adapter = MockLLMAdapter([
        ("", [tool_call("danger", "{}")]),
        ("完成", []),
    ])
    loop, kernel, _ = _make_loop(tmp_path, adapter, registry)

    @kernel.before_tool.use
    async def block(ctx, data, next):
        data["cancel"] = True
        data["reason"] = "被测试拦截"
        return await next(data)

    await loop.run_task("跑危险工具", system_prompt=SYSTEM_PROMPT)
    tool_msgs = [m for m in kernel.ctx.messages if m.role == "tool"]
    assert "Tool Execution Cancelled" in tool_msgs[0].content
    assert "被测试拦截" in tool_msgs[0].content


# ---------------------------------------------------------------- 上下文压缩（opencode 风格）


def test_effective_cap_window_proportional():
    """大窗口 → 90%×窗口（不被 48K 预算锁死）；小窗口 → min(预算, 90%×窗口) 兜底。"""
    registry = ToolRegistry()
    adapter = MockLLMAdapter([("完成", [])])
    kernel = Kernel("cap-test")
    loop = AgentLoop(kernel=kernel, adapter=adapter, registry=registry,
                     token_budget=48_000, context_window=1_000_000)
    assert loop._effective_cap() == 900_000

    loop2 = AgentLoop(kernel=kernel, adapter=adapter, registry=registry,
                      token_budget=48_000, context_window=128_000)
    assert loop2._effective_cap() == 115_200

    loop3 = AgentLoop(kernel=kernel, adapter=adapter, registry=registry,
                      token_budget=48_000, context_window=40_000)
    assert loop3._effective_cap() == 36_000  # 小窗口保持 min(预算, 90%×窗口)


class SummarizingAdapter(MockLLMAdapter):
    """无工具调用 → 返回摘要（模拟压缩器）；有工具调用 → 走脚本化响应。"""

    async def chat_stream(self, messages, tools, events=None):
        if not tools:
            return "已完成的任务摘要：修改了 a.txt，结论正确。", [], None
        return await super().chat_stream(messages, tools, events)


async def test_try_compact_replaces_head_with_summary():
    """超预算时：旧轮次被 LLM 摘要替换，最近轮次原样保留，前缀只失效一次。"""
    registry = ToolRegistry()
    adapter = SummarizingAdapter([("完成", [])])
    kernel = Kernel("compact-test")
    loop = AgentLoop(kernel=kernel, adapter=adapter, registry=registry,
                     token_budget=200, context_window=1_000_000)

    messages = [Message(role="system", content="你是测试 Agent。")]
    for i in range(3):
        messages.append(Message(role="user", content=f"问题{i} " + "x" * 150))
        messages.append(Message(role="assistant", content=f"回答{i} " + "x" * 150))

    # 直接构造超预算场景：hard_cap=250 强制压缩，但仍装得下最新一轮
    # （system 11 + 每轮 92；装完最新轮还剩 147，可再装一轮）。
    # 注意不能像旧版那样设成 100——那比最新一轮自身（92）还小，
    # 会触发「按消息切」的新逻辑（见 test_compaction_tail_fits_budget）。
    plan = loop.context_manager.split_for_compaction(messages, hard_cap=250)
    assert plan is not None
    head, tail, head_tokens = plan
    assert any(m.content.startswith("问题0") for m in head)
    assert any(m.content.startswith("问题2") for m in tail)
    assert head_tokens > 0

    compacted = await loop._try_compact(messages, 250)
    assert compacted is not None
    assert compacted[0].role == "system"
    assert compacted[1].role == "user" and "历史摘要" in compacted[1].content
    assert any(m.content.startswith("问题2") for m in compacted)
    assert not any(m.content.startswith("问题0") for m in compacted)
    assert loop._compression_count == 1
    assert loop._compressed_tokens > 0


async def test_summarize_failure_falls_back_none():
    """摘要调用失败 → 返回 None，调用方回退旧裁剪。"""
    registry = ToolRegistry()

    class BrokenAdapter(MockLLMAdapter):
        async def chat_stream(self, messages, tools, events=None):
            if not tools:
                raise RuntimeError("摘要失败")
            return await super().chat_stream(messages, tools, events)

    adapter = BrokenAdapter([("完成", [])])
    kernel = Kernel("compact-fail")
    loop = AgentLoop(kernel=kernel, adapter=adapter, registry=registry,
                     token_budget=100, context_window=1_000_000)
    messages = [
        Message(role="system", content="sys"),
        Message(role="user", content="问题一"),
        Message(role="assistant", content="回答一"),
        Message(role="user", content="问题二"),
        Message(role="assistant", content="回答二"),
    ]
    result = await loop._try_compact(messages, 100)
    assert result is None
    assert loop._compression_count == 0


async def test_injected_inputs_reach_conversation(tmp_path):
    """任务运行中补充的指令：在下一回合开始前以 [用户补充指令] 前缀注入对话。"""
    registry = ToolRegistry()

    async def noop(args):
        return "[Success] ok"

    registry.register("noop", "空操作", {"type": "object"}, noop)

    seen_turn_payloads = []

    class RecordingAdapter(MockLLMAdapter):
        async def chat_stream(self, messages, tools, events=None):
            seen_turn_payloads.append([m.content for m in messages])
            return await super().chat_stream(messages, tools, events)

    # 首轮必须调工具否则直接终答；补充指令在第二轮开始前注入
    adapter = RecordingAdapter([
        ("", [tool_call("noop", "{}")]),
        ("已处理补充指令", []),
    ])
    loop, kernel, store = _make_loop(tmp_path, adapter, registry)

    # 任务启动前排入两条补充指令（模拟运行中用户追加，回合开始时统一注入）
    loop.injected_inputs.append("补充：顺便看下 README")
    loop.injected_inputs.append("补充：注意 Windows 路径")

    added_roles = []
    kernel.events.on("message:added", lambda d: added_roles.append(d["message"]["role"]))

    result, _ = await loop.run_task("开始任务", system_prompt=SYSTEM_PROMPT)

    assert result == "已处理补充指令"
    # 第二次 LLM 调用的输入里应包含两条补充指令（在首轮回答之后）
    second_call = seen_turn_payloads[1]
    assert any("[用户补充指令] 补充：顺便看下 README" in (c or "") for c in second_call)
    assert any("[用户补充指令] 补充：注意 Windows 路径" in (c or "") for c in second_call)
    # 注入消息以 user 角色进入消息链并触发 message:added
    assert added_roles.count("user") >= 3  # 原始 1 条 + 注入 2 条
    # 落盘的历史也包含注入消息
    snap = store.load("test-session")
    contents = [m.content for m in snap.messages]
    assert any("[用户补充指令]" in (c or "") for c in contents)


# ---------------------------------------------------------------- LLM 瞬时故障重试


class FlakyLLMAdapter(MockLLMAdapter):
    """前 fail_times 次 chat_stream 抛指定异常（模拟瞬时故障），之后按脚本恢复。"""

    def __init__(self, responses, fail_times, error):
        super().__init__(responses)
        self.fail_times = fail_times
        self.error = error
        self.attempts = 0  # 总调用次数（含失败；calls 仅统计成功调用）

    async def chat_stream(self, messages, tools, events=None):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise self.error
        return await super().chat_stream(messages, tools, events)


async def test_llm_no_retry_on_fatal_error(tmp_path):
    """不可重试错误（如鉴权失败）→ 立即终止，不重试。"""
    adapter = FlakyLLMAdapter(
        [("不该被调用", [])],
        fail_times=10,
        error=LLMError("HTTP 401: invalid api key", retryable=False),
    )
    loop, _, _ = _make_loop(tmp_path, adapter, llm_retries=3, llm_timeout=5.0)

    result, _ = await loop.run_task("开始任务", system_prompt=SYSTEM_PROMPT)

    assert "[LLM Error]" in result
    assert "401" in result
    assert adapter.attempts == 1  # 只调用了一次，未重试


async def test_midbatch_queued_input_interrupts_remaining_tools(tmp_path):
    """批次执行期间到达的排队输入：中断剩余工具（占位结果），注入发生在工具结果之后。

    回归守护：user 消息不得插在 assistant(tool_calls) 与 tool 结果之间（原子对）。
    """
    registry = ToolRegistry()
    executed = []

    async def noop(args):
        executed.append(args.get("tag"))
        return "[Success] ok"

    registry.register("noop", "空工具", {"type": "object"}, noop)

    # 第一批返回 3 个工具调用，随后直接收尾
    adapter = MockLLMAdapter([
        ("", [tool_call("noop", '{"tag": "a"}'),
              tool_call("noop", '{"tag": "b"}'),
              tool_call("noop", '{"tag": "c"}')]),
        ("已处理补充指令", []),
    ])
    # 强制串行执行：auto 模式下只读工具整批并行，无法逐个中断
    loop, kernel, store = _make_loop(tmp_path, adapter, registry)
    loop.parallel_tool_calls = "never"

    # 第一个工具执行时注入用户补充指令（模拟任务运行中用户发消息）
    orig_execute = loop._execute_tool_call

    async def execute_and_inject(call, stats):
        result = await orig_execute(call, stats)
        if call.arguments and '"a"' in call.arguments:
            loop.injected_inputs.append("顺便看下 README")
        return result

    loop._execute_tool_call = execute_and_inject  # type: ignore[method-assign]

    result, _ = await loop.run_task("开始", system_prompt=SYSTEM_PROMPT)
    assert result == "已处理补充指令"

    # 只有第一个工具真正执行；b/c 被占位中断
    assert executed == ["a"]

    snap = store.load("test-session")
    roles = [m.role for m in snap.messages]
    texts = [m.content or "" for m in snap.messages]

    # 注入消息存在于历史
    assert any("[用户补充指令] 顺便看下 README" in t for t in texts)
    # 占位结果存在
    assert any("[Interrupted]" in t for t in texts)
    # 原子对保护：user 注入消息必须出现在全部 tool 结果之后（不得插在中间）
    first_tool_idx = min(i for i, r in enumerate(roles) if r == "tool")
    last_tool_idx = max(i for i, r in enumerate(roles) if r == "tool")
    inject_idx = next(i for i, t in enumerate(texts) if "[用户补充指令]" in t)
    assert inject_idx > last_tool_idx > first_tool_idx, (
        f"注入消息位置非法：inject={inject_idx}, tool=[{first_tool_idx},{last_tool_idx}]"
    )

async def test_prune_stats_do_not_double_count_across_turns():
    """prune 是逐轮投影（messages 不写回），同一批老消息会被反复裁掉。

    回归的是统计失真：原实现每轮把整笔 removed_tokens 累加，
    连续 N 轮就把同一笔节省计了 N 次（线上 604 轮 → 面板省 token 虚高几十倍）。
    正确行为：只累计**新**被裁掉的消息，重复裁剪不再加数。

    走 prune 分支的前提是「经济学决策认为不值得摘要」——把 economics 关掉
    即可稳定复现这条兜底路径（这也正是线上长期发生的路径）。
    """
    registry = ToolRegistry()
    adapter = MockLLMAdapter([("完成", [])])
    loop = AgentLoop(kernel=Kernel("prune-stats"), adapter=adapter, registry=registry,
                     token_budget=300, context_window=1000)
    # 强制走免费裁剪兜底（不触发 LLM 摘要，否则走的是另一条会写回的分支）
    loop._enable_compaction_economics = False

    async def _no_compact(*a, **kw):
        return None
    loop._try_compact = _no_compact  # type: ignore[method-assign]

    messages = [Message(role="system", content="sys")]
    for i in range(6):
        messages.append(Message(role="user", content=f"问题{i} " + "x" * 600))
        messages.append(Message(role="assistant", content=f"回答{i} " + "x" * 600))
    total = len(messages)

    stats: Dict[str, Any] = {"turns": 1}
    payload = await loop._trim_context(messages, stats)
    after_first = loop._compressed_tokens
    assert after_first > 0, "第一轮应统计到裁剪节省"
    assert loop._compression_count == 1
    # prune 投影不写回：messages 仍是完整历史，只有返回的 payload 变短
    assert len(messages) == total
    assert len(payload) < total

    # 第二轮：同一批老消息被重新裁掉，但不应再计入节省
    await loop._trim_context(messages, stats)
    assert loop._compressed_tokens == after_first, \
        "重复裁剪同一批消息不得再累加节省（否则随轮数线性虚高）"
    assert loop._compression_count == 1


async def test_prune_stats_count_newly_aged_messages():
    """新增的更老消息（本轮新被裁到的）仍要计入——去重不能把真节省也丢掉。"""
    registry = ToolRegistry()
    adapter = MockLLMAdapter([("完成", [])])
    loop = AgentLoop(kernel=Kernel("prune-new"), adapter=adapter, registry=registry,
                     token_budget=300, context_window=1000)
    loop._enable_compaction_economics = False

    async def _no_compact(*a, **kw):
        return None
    loop._try_compact = _no_compact  # type: ignore[method-assign]

    messages = [Message(role="system", content="sys")]
    for i in range(6):
        messages.append(Message(role="user", content=f"问题{i} " + "x" * 600))
        messages.append(Message(role="assistant", content=f"回答{i} " + "x" * 600))

    stats: Dict[str, Any] = {"turns": 1}
    await loop._trim_context(messages, stats)
    baseline = loop._compressed_tokens

    # 追加新轮次：更老的消息会滑出保留窗口 → 产生新的节省
    messages.append(Message(role="user", content="新问题 " + "x" * 400))
    messages.append(Message(role="assistant", content="新回答 " + "y" * 400))
    messages.append(Message(role="user", content="更新问题 " + "x" * 400))
    await loop._trim_context(messages, stats)
    assert loop._compressed_tokens > baseline, "滑出窗口的消息属于新节省，必须计入"


# ---------------------------------------------------------------- after_tool 协程泄漏

async def test_after_tool_timeout_leaves_no_dangling_coroutine(tmp_path):
    """afterTool hook 超时/被取消时不得留下未 await 的协程。

    回归：原实现写 `await asyncio.wait_for(pipeline.run(...), timeout=...)`。
    wait_for 在超时或外层取消时会丢弃传进去的协程对象，CPython 随即对每个
    受影响的工具报 "coroutine 'Pipeline.run' was never awaited"。它不致命，
    但**每个被用户拒绝/超时的工具都漏一个**，日志被 RuntimeWarning 灌满，
    真正的问题反而被淹没（ACP 联调时就是这样被埋掉的）。

    修法：先 ensure_future 包成 task 再等，task 负责关闭协程；
    finally 里兜底 cancel + await，保证不留悬空对象。
    """
    import gc
    import warnings

    registry = ToolRegistry()

    async def echo(args):
        return "ok"

    registry.register("echo", "回声", {"type": "object"}, echo)
    adapter = MockLLMAdapter([
        ("", [tool_call("echo", "{}")]),
        ("完成", []),
    ])
    loop, kernel, _store = _make_loop(tmp_path, adapter, registry)

    async def slow_hook(ctx, data, next_call):
        await asyncio.sleep(30)
        return await next_call(data)

    kernel.after_tool.use(slow_hook)
    loop.tool_timeout = 0.05        # 让 hook 必然超时

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)
        await loop.run_task("试试")
        gc.collect()
        await asyncio.sleep(0)

    leaked = [w for w in caught if "never awaited" in str(w.message)]
    assert not leaked, f"出现协程泄漏: {[str(w.message) for w in leaked]}"


async def test_after_tool_cancel_cleans_up_task(tmp_path):
    """外层 turn 被取消时，afterTool 的 task 必须被收干净再抛出。"""
    registry = ToolRegistry()

    async def echo(args):
        return "ok"

    registry.register("echo", "回声", {"type": "object"}, echo)
    adapter = MockLLMAdapter([("", [tool_call("echo", "{}")]), ("完成", [])])
    loop, kernel, _store = _make_loop(tmp_path, adapter, registry)

    started = asyncio.Event()

    async def hanging_hook(ctx, data, next_call):
        started.set()
        await asyncio.sleep(60)
        return await next_call(data)

    kernel.after_tool.use(hanging_hook)

    task = asyncio.ensure_future(loop.run_task("试试"))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # 取消后不应有残留的 hook 任务在跑
    assert not [t for t in asyncio.all_tasks()
                if t is not asyncio.current_task() and not t.done()], "hook task 残留"


async def test_after_tool_passes_a_task_not_a_bare_coroutine(tmp_path, monkeypatch):
    """afterTool 必须把**已包装的 Task**交给 wait_for，而不是裸协程。

    回归：原实现 ``wait_for(pipeline.run(...))`` 传入的是协程对象。
    wait_for 在超时/外层取消时会丢弃它，CPython 随即报
    "coroutine 'Pipeline.run' was never awaited"——每个被拒绝或超时的工具
    漏一个，日志被 RuntimeWarning 灌满（ACP 联调时真实问题就是这样被埋掉的）。

    为什么断言「收到的是 Task」而不是「没有警告」：协程何时被 GC 回收取决于
    运行环境，最小单测里抓不到那条警告（实测旧实现也能通过抓警告的测试）。
    而「传入的是 Task 还是协程」是**两条实现路径的确定差别**：
    Task 持有协程、取消时负责关闭；裸协程一旦被丢弃就无人回收。
    """
    registry = ToolRegistry()

    async def echo(args):
        return "ok"

    registry.register("echo", "回声", {"type": "object"}, echo)
    adapter = MockLLMAdapter([("", [tool_call("echo", "{}")]), ("完成", [])])
    loop, kernel, _store = _make_loop(tmp_path, adapter, registry)

    captured = []
    real_wait_for = asyncio.wait_for

    async def spy_wait_for(aw, timeout=None):
        captured.append(type(aw).__name__)
        return await real_wait_for(aw, timeout=timeout)

    monkeypatch.setattr(asyncio, "wait_for", spy_wait_for)

    async def fast_hook(ctx, data, next_call):
        return await next_call(data)

    kernel.after_tool.use(fast_hook)
    await loop.run_task("试试")

    # 只关心 afterTool 那一次调用（工具执行本身也用 wait_for 包协程，
    # 那是注册表调用，语义不同也不在本次修复范围）
    assert "Task" in captured, (
        f"wait_for 未收到 Task（实际收到: {captured}）——"
        "afterTool 的协程仍可能被丢弃而永不 await"
    )
