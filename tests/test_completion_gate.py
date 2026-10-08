# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""完成证据门禁测试（W2）。

覆盖两层：
1. 机械聚合（`litework/core/gate.py`）：三态语义——必检项缺失/UNKNOWN/BLOCKED
   都不得放行，UNKNOWN 绝不当 PASS；Evidence 脱敏；
2. 循环集成：Agent 声称完成时按**工具事实**核验，advisory 只记录、enforced 未通过
   则注入提醒再给一轮；并提供 `kernel.before_finish` Stop 钩子给判定类插件（如 Jev）。
"""
from __future__ import annotations

import json
import os

import pytest

from litework.core.agent_loop import AgentLoop
from litework.core.gate import (
    GateSpec,
    Submission,
    aggregate,
    derive_submissions,
    load_spec,
    parse_exit_code,
    parse_exit_codes,
    redact_evidence,
)
from litework.core.kernel import Kernel
from litework.core.session_store import SessionStore
from litework.core.types import Message
from litework.tools.registry import ToolRegistry
from tests.conftest import MockLLMAdapter, tool_call

SYSTEM_PROMPT = "你是测试 Agent。"


# ---------------------------------------------------------------- 机械聚合

def _spec(*checks):
    return GateSpec.from_dict({
        "version": 1, "spec_id": "test-spec",
        "checks": [{"id": cid, "required": req} for cid, req in checks],
    })


def test_aggregate_pass_requires_all_required_pass():
    spec = _spec(("a", True), ("b", True))
    ok = {"a": Submission("a", "pass"), "b": Submission("b", "pass")}
    assert aggregate(spec, ok)["verdict"] == "pass"


def test_aggregate_missing_required_is_unknown_not_pass():
    """必检项缺失 ≠ 通过（UNKNOWN 绝不当 PASS）。"""
    spec = _spec(("a", True), ("b", True))
    result = aggregate(spec, {"a": Submission("a", "pass")})
    assert result["verdict"] == "unknown"
    missing = [c for c in result["checks"] if c["id"] == "b"][0]
    assert missing["status"] == "unknown" and "缺失" in missing["message"]


def test_aggregate_blocked_wins_and_unknown_blocks_pass():
    spec = _spec(("a", True), ("b", True), ("c", True))
    assert aggregate(spec, {"a": Submission("a", "pass"), "b": Submission("b", "blocked"),
                            "c": Submission("c", "pass")})["verdict"] == "blocked"
    assert aggregate(spec, {"a": Submission("a", "pass"), "b": Submission("b", "unknown"),
                            "c": Submission("c", "pass")})["verdict"] == "unknown"


def test_aggregate_optional_check_does_not_block():
    """非必检项缺失/未通过不影响整体结论（状态仍如实列出）。"""
    spec = _spec(("a", True), ("extra", False))
    result = aggregate(spec, {"a": Submission("a", "pass")})
    assert result["verdict"] == "pass"
    assert [c["status"] for c in result["checks"] if c["id"] == "extra"] == ["unknown"]


def test_aggregate_binds_spec_digest():
    """运行绑定固定规则版本：digest 随规则内容变化。"""
    spec_a = _spec(("a", True))
    spec_b = _spec(("a", True), ("b", True))
    assert aggregate(spec_a, {})["spec_digest"] != aggregate(spec_b, {})["spec_digest"]
    # 同规则同摘要（稳定）
    assert aggregate(spec_a, {})["spec_digest"] == aggregate(_spec(("a", True)), {})["spec_digest"]


def test_illegal_status_counts_as_unknown():
    spec = _spec(("a", True))
    assert aggregate(spec, {"a": Submission("a", "definitely")})["verdict"] == "unknown"


def test_evidence_is_redacted_and_capped():
    assert "sk-" not in redact_evidence("token sk-abcdef0123456789abcdef")
    assert "[redacted]" in redact_evidence("token sk-abcdef0123456789abcdef")
    long_text = "x" * 2000
    assert len(redact_evidence(long_text)) <= 520
    # 提交与聚合结果都过脱敏
    result = aggregate(_spec(("a", True)), {
        "a": Submission("a", "pass", evidence={"cmd": "ghp_0123456789abcdefghij"})})
    assert "ghp_" not in json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------- 规则加载与事实映射

def test_load_spec_defaults_and_custom(tmp_path):
    assert load_spec(None).spec_id == "litework-completion-v1"
    assert load_spec(str(tmp_path)).spec_id == "litework-completion-v1"  # 无文件 → 默认

    spec_dir = tmp_path / ".litework"
    spec_dir.mkdir()
    (spec_dir / "gate.json").write_text(json.dumps({
        "version": 2, "spec_id": "custom",
        "checks": [{"id": "verify-commands", "required": True}],
    }), encoding="utf-8")
    loaded = load_spec(str(tmp_path))
    assert loaded.spec_id == "custom" and loaded.version == 2

    # 损坏配置 → 回退默认（门禁不因配置坏掉而卡死）
    (spec_dir / "gate.json").write_text("{oops", encoding="utf-8")
    assert load_spec(str(tmp_path)).spec_id == "litework-completion-v1"


def test_parse_exit_codes_and_derive_submissions():
    assert parse_exit_code("[Exit Code]: 0\n[STDOUT]: ok") == 0
    assert parse_exit_code("[Exit Code]: 1") == 1
    assert parse_exit_code("no code here") is None
    assert parse_exit_codes("[Exit Code]: 0\n[Exit Code]: 2") == [0, 2]

    subs = derive_submissions({
        "commands": [{"command": "pytest -q", "exit_code": 0, "captured_at": "10:00:00"}],
        "changed_files": ["web/src/App.tsx"],
    })
    assert subs["verify-commands"].status == "pass"
    assert subs["changed-files"].status == "pass"

    # 非 0 退出码 → blocked
    assert derive_submissions({"commands": [{"exit_code": 1}]})["verify-commands"].status == "blocked"
    # 退出码解析不出来 → unknown（不当作通过）
    assert derive_submissions({"commands": [{"exit_code": None}]})["verify-commands"].status == "unknown"
    # 越界改动 → blocked
    assert derive_submissions({"changed_files": ["../outside.txt"]})["changed-files"].status == "blocked"


# ---------------------------------------------------------------- 循环集成

def _make_loop(tmp_path, adapter, registry, **kwargs):
    kernel = Kernel("gate-session")
    store = SessionStore(str(tmp_path / "sessions"))
    loop = AgentLoop(kernel=kernel, adapter=adapter, registry=registry,
                     session_store=store, max_steps=10, **kwargs)
    loop.workspace = str(tmp_path)
    return loop, kernel, store


def _tools(tmp_path):
    registry = ToolRegistry()

    async def write_file(args):
        p = os.path.join(str(tmp_path), args["filePath"])
        os.makedirs(os.path.dirname(p) or str(tmp_path), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(args.get("content", ""))
        return "[Success] 已写入"

    async def execute_command(args):
        cmd = args.get("command", "")
        code = 0 if "pytest" in cmd else 1
        return f"[Exit Code]: {code}\n[STDOUT]: ran {cmd}"

    registry.register("write_file", "写文件", {"type": "object"}, write_file)
    registry.register("execute_command", "执行命令", {"type": "object"}, execute_command)
    return registry


async def test_advisory_records_gate_without_blocking(tmp_path):
    """默认 advisory：只记录 + 发事件，不打断收尾。"""
    adapter = MockLLMAdapter([
        ("", [tool_call("write_file", '{"filePath":"a.txt","content":"hi"}')]),
        ("已完成了修改。", []),
    ])
    loop, kernel, _ = _make_loop(tmp_path, adapter, _tools(tmp_path))
    loop.completion_gate = "advisory"
    events = []
    kernel.events.on("gate:result", lambda d: events.append(d))

    result, _ = await loop.run_task("改点东西", system_prompt=SYSTEM_PROMPT)

    assert result == "已完成了修改。"
    assert len(adapter.calls) == 2                    # 未追加轮次
    assert loop.last_gate_result["verdict"] == "unknown"   # 改了文件但没验证
    assert loop.last_gate_result["mode"] == "advisory"
    statuses = {c["id"]: c["status"] for c in loop.last_gate_result["checks"]}
    assert statuses["changed-files"] == "pass"
    assert statuses["verify-commands"] == "unknown"   # 没跑验证 → 不得当 PASS
    assert len(events) == 1 and events[0]["verdict"] == "unknown"


async def test_enforced_injects_reminder_then_allows_finish(tmp_path):
    """enforced：未通过 → 注入提醒再给一轮；补齐验证后放行。"""
    adapter = MockLLMAdapter([
        ("", [tool_call("write_file", '{"filePath":"a.txt","content":"hi"}')]),
        ("已完成了修改。", []),                                        # 无验证就声称完成 → 被拦
        ("", [tool_call("execute_command", '{"command":"pytest -q"}')]),  # 补验证
        ("测试通过，任务完成。", []),
    ])
    loop, kernel, store = _make_loop(tmp_path, adapter, _tools(tmp_path))
    loop.completion_gate = "enforced"
    events = []
    kernel.events.on("gate:result", lambda d: events.append(d))

    result, _ = await loop.run_task("改点东西", system_prompt=SYSTEM_PROMPT)

    assert result == "测试通过，任务完成。"
    assert loop._gate_retries == 1                    # 恰好补了一轮
    # 提醒确实注入到 system 消息（模型下一轮才可能补验证）
    snap = store.load("gate-session")
    joined = "\n".join((m.content or "") for m in snap.messages if m.role == "system")
    assert "[gate]" in joined and "完成门禁未通过" in joined
    # 最终一次门禁结果：命令退出码 0 → pass
    assert loop.last_gate_result["verdict"] == "pass"
    assert [e["verdict"] for e in events] == ["unknown", "pass"]


async def test_enforced_passes_immediately_when_verified(tmp_path):
    """改了文件且跑过验证（退出码 0）→ 门禁直接 pass，不多给轮次。"""
    adapter = MockLLMAdapter([
        ("", [tool_call("write_file", '{"filePath":"a.js","content":"x"}')]),
        ("", [tool_call("execute_command", '{"command":"pytest -q"}')]),
        ("全部通过，完成。", []),
    ])
    loop, kernel, _ = _make_loop(tmp_path, adapter, _tools(tmp_path))
    loop.completion_gate = "enforced"

    result, _ = await loop.run_task("改点东西", system_prompt=SYSTEM_PROMPT)

    assert result == "全部通过，完成。"
    assert len(adapter.calls) == 3
    assert loop.last_gate_result["verdict"] == "pass"


async def test_off_mode_skips_gate(tmp_path):
    adapter = MockLLMAdapter([
        ("", [tool_call("write_file", '{"filePath":"a.txt","content":"hi"}')]),
        ("完成。", []),
    ])
    loop, kernel, _ = _make_loop(tmp_path, adapter, _tools(tmp_path))
    loop.completion_gate = "off"
    events = []
    kernel.events.on("gate:result", lambda d: events.append(d))

    result, _ = await loop.run_task("改点东西", system_prompt=SYSTEM_PROMPT)

    assert result == "完成。"
    assert loop.last_gate_result is None and events == []


async def test_before_finish_hook_can_attach_opinion_and_block(tmp_path):
    """Stop 钩子：判定类插件（如 Jev）可在收尾前附加意见，或要求再给一轮。

    第二轮真跑一次验证命令 → 机械门禁通过；钩子意见仍随门禁结果发出（只加信息）。
    """
    adapter = MockLLMAdapter([
        ("", [tool_call("write_file", '{"filePath":"a.txt","content":"hi"}')]),
        ("完成了。", []),                                        # 钩子要求补证据 → 再给一轮
        ("", [tool_call("execute_command", '{"command":"pytest -q"}')]),
        ("补充证据后的最终答复。", []),
    ])
    loop, kernel, _ = _make_loop(tmp_path, adapter, _tools(tmp_path))
    loop.completion_gate = "enforced"
    calls = []

    @kernel.before_finish.use
    async def _hook(ctx, data, next):
        calls.append(data.get("reason"))
        data["opinion"] = {"source": "Jev", "level": "证据不足", "confidence": 0.4}
        if data.get("retries", 0) == 0:
            data["block_finish"] = True
            data["reminder"] = "判定层认为证据不足，请补充实际执行证明。"
        return await next(data)

    events = []
    kernel.events.on("gate:result", lambda d: events.append(d))
    result, _ = await loop.run_task("改点东西", system_prompt=SYSTEM_PROMPT)

    assert calls == ["no_tool_calls", "no_tool_calls"]   # 两轮收尾尝试都过钩子
    assert result == "补充证据后的最终答复。"
    # 钩子意见随门禁结果一并发出（只加信息，不替代机械聚合）
    assert events[-1]["opinion"]["source"] == "Jev"
    assert loop.last_gate_result["opinion"]["confidence"] == 0.4
    assert loop.last_gate_result["verdict"] == "pass"
