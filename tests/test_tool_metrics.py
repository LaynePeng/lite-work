# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""工具/技能指标测试（W3）。

对应手册 p.47「工具评测」：把「命中率 / 成功率」持续量化，据此改进描述与技能边界。
本文件覆盖：
1. 记录与聚合（core/metrics.py）：成败判定、成功率、技能归属、文件轮转、隐私（不落正文）；
2. 技能负向触发（not-for）：命中即抑制，两种匹配模式都要生效；
3. 循环集成：工具调用与 load_skill 被记录；
4. 接口：GET /api/metrics/tools。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from litework.app import AgentApp
from litework.core.agent_loop import AgentLoop
from litework.core.kernel import Kernel
from litework.core.metrics import ToolMetrics, classify_outcome, read_events, summarize
from litework.core.session_store import SessionStore
from litework.server.app import create_app
from litework.tools.registry import ToolRegistry
from litework.tools.skills import SkillsTools
from tests.conftest import MockLLMAdapter, tool_call

SYSTEM_PROMPT = "你是测试 Agent。"


# ---------------------------------------------------------------- 记录与聚合

def test_classify_outcome_uses_tool_level_prefixes():
    assert classify_outcome("[Command OK] 输出") == "ok"
    assert classify_outcome("[Exit Code]: 0\n[STDOUT]: ok") == "ok"
    assert classify_outcome("[Error]: 找不到文件") == "error"
    assert classify_outcome("[Security Blocked]: 命中高危模式") == "error"
    assert classify_outcome("[Denied] 无权限") == "error"
    assert classify_outcome("[Stopped]: 用户停止") == "cancelled"
    assert classify_outcome("[Exit Code]: 124\n[Timed Out]: 超时") == "cancelled"


def test_record_and_summarize(tmp_path):
    path = str(tmp_path / "metrics" / "tool_events.jsonl")
    m = ToolMetrics(path)
    m.record_tool("read_file", "ok", duration_ms=12.0, skills=["ppt"], session_id="s1")
    m.record_tool("read_file", "ok", duration_ms=8.0, skills=["ppt"])
    m.record_tool("write_file", "error", duration_ms=30.0)
    m.record_skill("ppt", source="explicit", session_id="s1")

    events = read_events(path)
    assert len(events) == 4
    # 隐私：不落参数/输出正文
    blob = json.dumps(events, ensure_ascii=False)
    for leak in ("content", "arguments", "stdout", "result"):
        assert leak not in blob

    data = summarize(events)
    assert data["tool_count"] == 2 and data["skill_count"] == 1
    rf = data["tools"]["read_file"]
    assert rf["calls"] == 2 and rf["errors"] == 0 and rf["success_rate"] == 1.0
    assert rf["avg_ms"] == 10.0
    wf = data["tools"]["write_file"]
    assert wf["errors"] == 1 and wf["success_rate"] == 0.0
    # 成功率最低的工具排在前面（用于"该改哪个工具的描述了"）
    assert data["worst_tools"][0]["tool"] == "write_file"
    # 技能 → 关联工具（任务级归属）
    assert data["skills"]["ppt"]["tools"] == {"read_file": 2}


def test_metrics_disabled_or_missing_path_is_noop(tmp_path):
    m = ToolMetrics(None)
    m.record_tool("read_file", "ok")
    m.record_skill("x")
    assert read_events(None) == []
    assert read_events(str(tmp_path / "nope.jsonl")) == []
    # 损坏文件不影响读取
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n{\"type\":\"tool\",\"tool\":\"t\"}\n", encoding="utf-8")
    events = read_events(str(bad))
    assert len(events) == 1 and events[0]["tool"] == "t"


def test_metrics_file_rotation_keeps_recent(tmp_path, monkeypatch):
    """文件超限后保留最近一半，指标不允许无限增长。"""
    import litework.core.metrics as metrics_mod

    monkeypatch.setattr(metrics_mod, "_MAX_FILE_BYTES", 300)
    path = str(tmp_path / "tool_events.jsonl")
    m = ToolMetrics(path)
    for i in range(50):
        m.record_tool(f"tool{i}", "ok")
    events = read_events(path)
    assert 0 < len(events) < 50          # 被截断
    assert events[-1]["tool"] == "tool49"  # 保留的是最近的


# ---------------------------------------------------------------- 技能负向触发（not-for）

def _write_skill(root, name: str, frontmatter: str) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n# {name}\n", encoding="utf-8")


def test_not_for_suppresses_trigger(tmp_path, monkeypatch):
    """`not-for` 命中 → 该技能不被自动注入（提升命中率的负向触发）。

    内置技能根恒可见（随包分发的 ppt-master 等也参与匹配），所以断言只针对
    本用例写入的 presentation，不能假设 "命中列表 == [presentation]"。
    """
    root = tmp_path / ".agents" / "skills"
    root.mkdir(parents=True)
    _write_skill(root, "presentation",
                 "name: presentation\ntriggers: ppt,幻灯片\nnot-for: 表格,excel")
    tools = SkillsTools(str(tmp_path))

    def hits(text: str, mode: str = "substring") -> list:
        return [s["name"] for s in tools.match_skills(text, mode=mode)]

    skills = {s["name"]: s for s in tools.list_skills()}
    assert skills["presentation"]["not_for"] == "表格,excel"

    # 正向：presentation 被命中（此时可能还有内置 ppt-master 一起命中，
    # 那是真实的产品行为；本用例只关心 not-for 的抑制效果）
    assert "presentation" in hits("帮我做个 ppt")
    # 负向：命中 not-for → presentation 被抑制
    assert "presentation" not in hits("把 ppt 里的表格改成 excel 格式")
    # 高级模式同样生效（注意：advanced 用 \b 词边界，CJK 连写没有边界，
    # 所以正向用 latin 触发词 ppt；not-for 是子串匹配，中文照样抑制）
    assert "presentation" not in hits("把 ppt 里的表格改一下", mode="advanced")
    assert "presentation" in hits("做个 ppt", mode="advanced")


# ---------------------------------------------------------------- 循环集成

async def test_loop_records_tool_and_skill_metrics(tmp_path):
    registry = ToolRegistry()

    async def load_skill(args):
        return "# 技能正文"

    async def read_file(args):
        return "[Error]: 文件不存在"

    registry.register("load_skill", "加载技能", {"type": "object"}, load_skill)
    registry.register("read_file", "读文件", {"type": "object"}, read_file)

    adapter = MockLLMAdapter([
        ("", [tool_call("load_skill", '{"name":"ppt-master"}')]),
        ("", [tool_call("read_file", '{"filePath":"a.md"}')]),
        ("完成。", []),
    ])
    metrics = ToolMetrics(str(tmp_path / "metrics" / "tool_events.jsonl"))
    kernel = Kernel("metrics-session")
    loop = AgentLoop(kernel=kernel, adapter=adapter, registry=registry,
                     session_store=SessionStore(str(tmp_path / "sessions")),
                     max_steps=10, metrics=metrics)
    loop.workspace = str(tmp_path)
    loop.completion_gate = "off"          # 与本测试无关

    result, _ = await loop.run_task("加载技能并读文件", system_prompt=SYSTEM_PROMPT)
    assert result == "完成。"

    events = read_events(metrics.path)
    tools = [e for e in events if e["type"] == "tool"]
    skills = [e for e in events if e["type"] == "skill"]
    assert [t["tool"] for t in tools] == ["load_skill", "read_file"]
    assert tools[0]["outcome"] == "ok"
    assert tools[1]["outcome"] == "error"                 # [Error] → error
    assert skills == [e for e in skills] and skills[0]["skill"] == "ppt-master"
    # 归属：load_skill 之后的调用带上该技能
    assert tools[1]["skills"] == ["ppt-master"]
    data = summarize(events)
    assert data["tools"]["read_file"]["success_rate"] == 0.0
    # 耗时必须写入指标：此前 _record_tool_metrics 未透传 duration_ms，
    # 导致 tool_events.jsonl 里恒为 null，「哪个工具慢」无法回溯（实测踩过）。
    # 注：慢工具/长尾只能靠这里的数据量化，故与 outcome 同等重要。
    for t in tools:
        assert isinstance(t["duration_ms"], (int, float)), f"{t['tool']} 缺耗时"
    assert data["tools"]["read_file"]["avg_ms"] is not None


async def test_loop_without_metrics_is_noop(tmp_path):
    """未装配指标时零开销、不抛错。"""
    registry = ToolRegistry()

    async def read_file(args):
        return "ok"

    registry.register("read_file", "读文件", {"type": "object"}, read_file)
    adapter = MockLLMAdapter([
        ("", [tool_call("read_file", '{"filePath":"a.md"}')]),
        ("完成。", []),
    ])
    kernel = Kernel("no-metrics")
    loop = AgentLoop(kernel=kernel, adapter=adapter, registry=registry, max_steps=5)
    loop.workspace = str(tmp_path)
    loop.completion_gate = "off"
    assert loop.metrics is None
    result, _ = await loop.run_task("读文件", system_prompt=SYSTEM_PROMPT)
    assert result == "完成。"


# ---------------------------------------------------------------- 接口

def test_metrics_endpoint(tmp_path):
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    metrics = ToolMetrics(app.tool_metrics_path())
    metrics.record_tool("read_file", "ok", duration_ms=5.0)
    metrics.record_tool("write_file", "error")
    metrics.record_skill("ppt-master")

    fast = create_app(app, token=None)
    with TestClient(fast) as client:
        resp = client.get("/api/metrics/tools")
        assert resp.status_code == 200
        body: Dict[str, Any] = resp.json()
        assert body["enabled"] is True
        assert body["tool_count"] == 2 and body["skill_count"] == 1
        assert body["tools"]["write_file"]["success_rate"] == 0.0
        assert body["worst_tools"][0]["tool"] == "write_file"
        # 配置白名单里能看到开关
        assert "tool_metrics" in client.get("/api/config").json()


def test_metrics_endpoint_empty_is_ok(tmp_path):
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    fast = create_app(app, token=None)
    with TestClient(fast) as client:
        body = client.get("/api/metrics/tools").json()
        assert body["tool_count"] == 0 and body["worst_tools"] == []


def test_metrics_clear_endpoint(tmp_path):
    """清空指标：删除 JSONL，聚合从零开始（旧失败不再压着成功率）。"""
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    metrics = ToolMetrics(app.tool_metrics_path())
    metrics.record_tool("write_file", "error")
    metrics.record_skill("ppt-master")

    fast = create_app(app, token=None)
    with TestClient(fast) as client:
        # 清空前有数据
        assert client.get("/api/metrics/tools").json()["events"] == 2
        # 清空：ok，且文件已删
        assert client.post("/api/metrics/tools/clear").json()["ok"] is True
        assert not os.path.exists(app.tool_metrics_path())
        # 聚合归零
        body = client.get("/api/metrics/tools").json()
        assert body["tool_count"] == 0 and body["skill_count"] == 0
        assert body["worst_tools"] == []
        # 再清一次（文件已不存在）幂等不报错
        assert client.post("/api/metrics/tools/clear").json()["ok"] is True


# ---------------------------------------------------------------- 内置技能可触发

def test_builtin_presentation_skill_not_shadowed(tmp_path):
    """内置技能必须能被 triggers 命中：presentation 与 ppt-master 各有分工。

    回归背景：ppt-master 以「随包内置技能」发布后，其 triggers 与 presentation
    的宽泛词（PPT/ppt/幻灯片…）完全重叠 → 用户说「做个 PPT」同时注入两个技能。
    社区的修法是把 presentation 收窄为「初稿意图词」（PPT初稿/快速PPT/…），
    让 ppt-master 承接专业版。这里锁住该分工不被改回去。
    """
    from litework.tools.skills import SkillsTools

    tools = SkillsTools(str(tmp_path))
    by_name = {s["name"]: s for s in tools.list_skills()}

    assert "presentation" in by_name
    assert "ppt-master" in by_name
    pres_trig = by_name["presentation"]["triggers"]
    pm_trig = by_name["ppt-master"]["triggers"]
    assert pres_trig, "presentation 必须有 triggers（否则无法自动注入）"
    assert pm_trig, "ppt-master 必须有 triggers"

    # 分工：宽泛的 ppt 归 ppt-master，初稿意图词归 presentation
    assert "ppt" in [t.strip().lower() for t in pm_trig.split(",")]
    pres_words = [t.strip().lower() for t in pres_trig.split(",") if t.strip()]
    assert "ppt" not in pres_words, (
        "presentation 不应保留裸 'ppt' 触发词——会与 ppt-master 双注入")
    assert any(("初稿" in w) or ("快速" in w) or ("大纲" in w) for w in pres_words), (
        "presentation 应只承接初稿类意图")

    # 实际匹配
    assert "ppt-master" in [s["name"] for s in tools.match_skills("用 ppt-master 做份演示")]
    assert "presentation" in [s["name"] for s in tools.match_skills("快速PPT")]
