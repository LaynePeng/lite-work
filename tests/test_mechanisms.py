# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""v1.6.0 效率机制测试：观察打包 / 压缩经济学 / 动作融合 / 证据收据。"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from litework.core.agent_loop import AgentLoop
from litework.core.compaction_economics import decide_compaction
from litework.core.kernel import Kernel
from litework.core.observation_pack import (
    FULL_SENDS, THRESHOLD_BYTES, observations_dir, project_observations, read_recall_chunk,
)
from litework.core.types import Message, ToolCall
from litework.tools.registry import ToolRegistry
from tests.conftest import MockLLMAdapter


# ---------------------------------------------------------------- 观察打包

def _big_text(lines: int = 900) -> str:
    return "\n".join(f"line {i}: " + "x" * 60 for i in range(lines))


def _chain(text: str, assistant_after: int) -> list:
    """构造消息链：tool 结果 + N 条 assistant（模拟已被发送 N 次）。"""
    msgs = [Message(role="tool", name="run_cmd", tool_call_id="c1", content=text)]
    msgs += [Message(role="assistant", content=f"turn {i}") for i in range(assistant_after)]
    return msgs


def test_full_sends_then_placeholder(tmp_path):
    root = observations_dir(str(tmp_path / "truncations"))
    text = _big_text()
    assert len(text.encode()) >= THRESHOLD_BYTES

    # 前 FULL_SENDS 次：全文发送
    for sends in range(FULL_SENDS):
        projected, saved, packed = project_observations(_chain(text, sends), root)
        assert packed == 0 and saved == 0
        assert projected[0].content == text

    # 第 FULL_SENDS+1 次投影：替换为占位符并归档
    projected, saved, packed = project_observations(_chain(text, FULL_SENDS), root)
    assert packed == 1 and saved > 0
    assert "obs_" in projected[0].content and "obs_recall" in projected[0].content
    assert len(projected[0].content) < len(text) // 2
    # 原文已归档且内容逐字节一致
    obs_id = projected[0].content.split("[obs:")[1].split("]")[0]
    with open(os.path.join(root, f"{obs_id}.txt"), encoding="utf-8") as f:
        assert f.read() == text
    # 存档目录外的 session 消息（输入链）未被修改
    assert _chain(text, FULL_SENDS)[0].content == text


def test_small_results_never_packed(tmp_path):
    root = observations_dir(str(tmp_path / "truncations"))
    projected, saved, packed = project_observations(
        _chain("small output", 99), root)
    assert packed == 0 and saved == 0 and projected[0].content == "small output"


def test_project_is_stateless_across_restarts(tmp_path):
    """发送计数是无状态的：重建投影器（模拟重启/恢复）结果一致。"""
    root = observations_dir(str(tmp_path / "truncations"))
    text = _big_text()
    # 已发送 5 次（跨了两次进程）→ 直接投影即替换
    projected, _, packed = project_observations(_chain(text, 5), root)
    assert packed == 1
    # 同一条链再投影一次（占位内容与归档一致）→ 幂等，不重复报节省
    _, saved2, packed2 = project_observations(projected, root)
    assert packed2 == 0 and saved2 == 0


def test_recall_chunk_pagination(tmp_path):
    root = observations_dir(str(tmp_path / "truncations"))
    text = _big_text(2000)
    projected, _, _ = project_observations(_chain(text, 9), root)
    obs_id = projected[0].content.split("[obs:")[1].split("]")[0]

    chunk = read_recall_chunk(root, obs_id, 0)
    assert chunk.bytes > 0 and not chunk.eof and chunk.next_offset > 0
    # 沿 next_offset 读完全文，拼接结果与原文一致
    pieces = [chunk.text]
    offset = chunk.next_offset
    while not chunk.eof:
        chunk = read_recall_chunk(root, obs_id, offset)
        pieces.append(chunk.text)
        offset = chunk.next_offset
    assert "".join(pieces) == text


def test_pack_fail_open(tmp_path, monkeypatch):
    """归档失败 → 回退原文（fail-open），绝不丢证据。"""
    root = observations_dir(str(tmp_path / "truncations"))

    def boom(root, obs):
        raise OSError("disk full")

    import litework.core.observation_pack as op
    monkeypatch.setattr(op, "archive_observation", boom)
    projected, saved, packed = project_observations(_chain(_big_text(), 9), root)
    assert packed == 0 and saved == 0
    assert projected[0].content == _big_text()


# ---------------------------------------------------------------- 压缩经济学

def test_economics_cheap_write_compacts_soon():
    """缓存写入价接近读取价（无缓存债）→ 剩余轮数足够就压缩。"""
    d = decide_compaction(
        head_tokens=100_000, summary_tokens=10_000, context_tokens=120_000,
        context_window=1_000_000, current_turn=10, max_turns=100,
        pricing={"input_per_mtok": 0.3, "cache_hit_per_mtok": 0.3},
    )
    assert d.compact and d.reason == "economic"


def test_economics_expensive_cache_defers():
    """DeepSeek 式写入价 >> 读取价 → 缓存债使 breakeven 大于剩余轮数 → 推迟。"""
    d = decide_compaction(
        head_tokens=30_000, summary_tokens=3_000, context_tokens=920_000,
        context_window=1_000_000, current_turn=10, max_turns=100,
        pricing={"input_per_mtok": 0.15, "cache_hit_per_mtok": 0.003},
    )
    # 920k 已贴近 90% 窗口保护线 → window_protection 优先
    assert d.compact and d.reason == "window_protection"


def test_economics_mid_context_defers():
    """中段上下文 + 高写入价：回本遥遥无期 → deferred_economic（不压缩）。"""
    d = decide_compaction(
        head_tokens=30_000, summary_tokens=3_000, context_tokens=400_000,
        context_window=1_000_000, current_turn=10, max_turns=100,
        pricing={"input_per_mtok": 0.15, "cache_hit_per_mtok": 0.003},
    )
    assert not d.compact and d.reason == "deferred_economic"
    assert d.breakeven_requests is not None and d.breakeven_requests > 20


# ---------------------------------------------------------------- 动作融合

def _fusion_loop(tmp_path, executed: list):
    registry = ToolRegistry()

    async def run_cmd(args):
        executed.append(args.get("command"))
        return f"ran: {args.get('command')}"

    registry.register("execute_command", "执行命令", {}, run_cmd)
    kernel = Kernel("fusion-test")
    loop = AgentLoop(kernel=kernel, adapter=MockLLMAdapter([]), registry=registry)
    return loop


async def test_fusion_appends_then_output(tmp_path):
    executed: list = []
    loop = _fusion_loop(tmp_path, executed)
    calls = [ToolCall(id="c1", name="apply_search_replace",
                      arguments=json.dumps({
                          "filePath": "a.py", "searchBlock": "x", "replaceBlock": "y",
                          "then": ["pytest -q", "ruff check ."],
                      }))]
    results = ["[Patch Success]: ok"]
    stats: dict = {"tool_calls": 0}
    out = await loop._apply_action_fusion(calls, results, stats)
    assert executed == ["pytest -q", "ruff check ."]
    assert "[then 验证结果（动作融合，同轮执行）]" in out[0]
    assert "ran: pytest -q" in out[0] and "ran: ruff check ." in out[0]


async def test_fusion_ignores_missing_then(tmp_path):
    executed: list = []
    loop = _fusion_loop(tmp_path, executed)
    calls = [ToolCall(id="c1", name="write_file",
                      arguments=json.dumps({"filePath": "a.py", "content": "x"}))]
    out = await loop._apply_action_fusion(calls, ["ok"], {"tool_calls": 0})
    assert out == ["ok"] and executed == []


# ---------------------------------------------------------------- 证据收据

class _ReducerAdapter:
    """小模型假适配器：返回带合法逐字引用的收据。"""

    name = "reducer"
    provider_id = "deepseek"
    model = "small-model"

    def __init__(self, receipt: str) -> None:
        self.receipt = receipt

    async def chat_stream(self, messages, tools, events=None):
        return self.receipt, [], None


async def test_reducer_replaces_large_result(tmp_path):
    text = "HEAD " + _big_text(900) + " line 42: unique-marker-abc"
    receipt = f"结论：输出正常。\n> line 42: unique-marker-abc"
    loop, kernel, _ = _reducer_loop(tmp_path, _ReducerAdapter(receipt))
    out = await loop._reduce_to_receipt("run_cmd", text)
    assert "[receipt id=" in out and "obs_recall" in out
    assert "unique-marker-abc" in out
    assert loop._mech_reducer_saved_tokens > 0
    # 原文已归档，可完整召回
    obs_id = out.split("id=")[1].split("]")[0]
    from litework.core.observation_pack import read_recall_chunk
    assert read_recall_chunk(loop._obs_root, obs_id, 0).text.startswith("HEAD ")


async def test_reducer_fail_open_on_bad_quote(tmp_path):
    """引用校验失败（编造的引用）→ 回退原文。"""
    text = _big_text(900)
    loop, _, _ = _reducer_loop(tmp_path, _ReducerAdapter("> 这段引用原文里根本没有 xyzzy"))
    out = await loop._reduce_to_receipt("run_cmd", text)
    assert out == text and loop._mech_reducer_saved_tokens == 0


def _reducer_loop(tmp_path, adapter):
    kernel = Kernel("reducer-test")
    loop = AgentLoop(kernel=kernel, adapter=MockLLMAdapter([]), registry=ToolRegistry(),
                     truncation_dir=str(tmp_path / "truncations"), reducer_adapter=adapter)
    return loop, kernel, None


# ---------------------------------------------------------------- 观察打包 × 循环

async def test_loop_projects_observations_before_call(tmp_path):
    """端到端：循环内的投影把第二次出现的同一大结果替换为占位符。"""
    root = observations_dir(str(tmp_path / "truncations"))
    big = _big_text(900)
    responses = [
        ("", [ToolCall(id="c1", name="noop", arguments='{"i": 0}')]),
        ("", [ToolCall(id="c2", name="noop", arguments='{"i": 1}')]),
        ("", [ToolCall(id="c3", name="noop", arguments='{"i": 2}')]),
        ("", [ToolCall(id="c4", name="noop", arguments='{"i": 3}')]),
        ("完成", []),
    ]
    sent_prompts: list = []

    class Adapter:
        name = "openai-compat"
        provider_id = "deepseek"
        model = "deepseek-flash"

        async def chat_stream(self, messages, tools, events=None):
            sent_prompts.append([m.content for m in messages if m.role == "tool"])
            n = len(sent_prompts)
            usage = {"prompt_tokens": 100, "completion_tokens": 5,
                     "prompt_cache_hit_tokens": 0}
            if n <= len(responses):
                return responses[n - 1] + (usage,)
            return "完成", [], usage

    registry = ToolRegistry()

    async def _noop(args):
        return big

    registry.register("noop", "noop", [], _noop)
    kernel = Kernel("obs-loop")
    loop = AgentLoop(kernel=kernel, adapter=Adapter(), registry=registry,
                     truncation_dir=str(tmp_path / "truncations"), max_steps=10)
    await loop.run_task("测试", system_prompt="sys")

    # 注意：noop 的返回会先过既有 truncator（50KB 上限）→ 入链的是截断预览+句柄，
    # 只要它仍 ≥ 24KB 就会被观察打包。因此断言用「入链内容」而非原始 big：
    full = sent_prompts[1][0]                       # 请求 2 携带的全文（截断后仍很大）
    assert len(full.encode()) >= THRESHOLD_BYTES    # 仍达到打包阈值
    assert "obs_" not in full                       # 请求 2 还是全文
    # 请求 2~4 全文一致（sends=0,1,2）；请求 5 起 sends=3 → 占位符
    for i in range(2, FULL_SENDS + 1):
        assert sent_prompts[i][0] == full
    assert sent_prompts[FULL_SENDS + 1][0].startswith("[obs:")
    # 存储链（session）里也是同一份全文（投影不修改存档）
    stored = [m.content for m in kernel.ctx.messages if m.role == "tool"]
    assert stored[0] == full
