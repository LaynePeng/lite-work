# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""项目运行环境清单测试（W5）。

覆盖：
1. 加载器：读/写/校验/缺失回退/损坏回退；
2. 生成器：按项目类型检测（pyproject.toml / package.json / 多类型 / 无特征文件）；
3. prompt 注入：有清单注入命令摘要、无清单零影响；
4. W2 门禁集成：声明了 verify 但没跑 → verdict 降级 unknown。
"""
from __future__ import annotations

import json

import pytest

from litework.core.agent_loop import AgentLoop
from litework.core.gate import aggregate, GateSpec, Submission
from litework.core.kernel import Kernel
from litework.core.runtime_manifest import (
    generate_runtime_manifest,
    load_runtime_manifest,
    manifest_path,
    runtime_summary_for_prompt,
    save_runtime_manifest,
    verify_commands,
)
from litework.core.session_store import SessionStore
from litework.core.system_prompt import SystemPromptBuilder
from litework.tools.registry import ToolRegistry
from tests.conftest import MockLLMAdapter, tool_call


# ---------------------------------------------------------------- 加载器

def test_load_missing_returns_none(tmp_path):
    assert load_runtime_manifest(None) is None
    assert load_runtime_manifest(str(tmp_path)) is None


def test_load_valid_manifest(tmp_path):
    (tmp_path / ".litework").mkdir()
    (tmp_path / ".litework" / "project.json").write_text(json.dumps({
        "version": 2,
        "setup": ["pip install -e .", "npm install"],
        "verify": ["pytest -q"],
        "artifacts": ["dist/**"],
    }), encoding="utf-8")
    m = load_runtime_manifest(str(tmp_path))
    assert m is not None
    assert m["version"] == 2
    assert m["setup"] == ["pip install -e .", "npm install"]
    assert m["verify"] == ["pytest -q"]
    assert m["artifacts"] == ["dist/**"]


def test_load_broken_returns_none(tmp_path):
    (tmp_path / ".litework").mkdir()
    (tmp_path / ".litework" / "project.json").write_text("{broken", encoding="utf-8")
    assert load_runtime_manifest(str(tmp_path)) is None


def test_load_non_dict_returns_none(tmp_path):
    (tmp_path / ".litework").mkdir()
    (tmp_path / ".litework" / "project.json").write_text("[1,2,3]", encoding="utf-8")
    assert load_runtime_manifest(str(tmp_path)) is None


def test_normalize_handles_bad_types(tmp_path):
    """字段类型不对 → 默认空值，不抛错。"""
    (tmp_path / ".litework").mkdir()
    (tmp_path / ".litework" / "project.json").write_text(json.dumps({
        "setup": "not a list",
        "verify": 42,
        "network": "bad",
    }), encoding="utf-8")
    m = load_runtime_manifest(str(tmp_path))
    assert m is not None
    assert m["setup"] == []
    assert m["verify"] == []
    assert m["network"]["allow"] == []


# ---------------------------------------------------------------- 保存（不覆盖已有）

def test_save_does_not_overwrite(tmp_path):
    m1 = {"version": 1, "setup": ["echo one"], "verify": [], "runtime": {},
          "network": {"allow": [], "default": "allow"}, "artifacts": [], "notes": ""}
    assert save_runtime_manifest(str(tmp_path), m1) is True
    m2 = dict(m1, setup=["echo two"])
    assert save_runtime_manifest(str(tmp_path), m2) is False  # 不覆盖
    loaded = load_runtime_manifest(str(tmp_path))
    assert loaded is not None and loaded["setup"] == ["echo one"]


# ---------------------------------------------------------------- 生成器

def test_generate_python_project(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    m = generate_runtime_manifest(str(tmp_path))
    assert "pip install" in " ".join(m["setup"])
    assert any("pytest" in v for v in m["verify"])
    assert m["runtime"].get("python") == "detected"


def test_generate_node_project(tmp_path):
    (tmp_path / "package.json").write_text('{"name":"x"}', encoding="utf-8")
    m = generate_runtime_manifest(str(tmp_path))
    assert "npm install" in m["setup"]
    assert "npm test" in m["verify"]


def test_generate_multi_language(tmp_path):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    m = generate_runtime_manifest(str(tmp_path))
    # 两种类型都检测到，命令合并
    assert any("pip" in s for s in m["setup"])
    assert any("npm" in s for s in m["setup"])
    assert any("pytest" in v for v in m["verify"])
    assert any("npm test" == v for v in m["verify"])


def test_generate_empty_workspace(tmp_path):
    m = generate_runtime_manifest(str(tmp_path))
    assert m["setup"] == [] and m["verify"] == []
    assert "未检测到" in m["notes"]


# ---------------------------------------------------------------- prompt 注入

def test_summary_for_prompt_with_manifest():
    m = {"setup": ["pip install -e ."], "verify": ["pytest -q"],
         "runtime": {}, "network": {"allow": [], "default": "allow"},
         "artifacts": [], "notes": "", "version": 1}
    s = runtime_summary_for_prompt(m)
    assert "pip install -e ." in s
    assert "pytest -q" in s
    assert "Runtime Manifest" in s or "环境清单" in s


def test_summary_for_prompt_empty_manifest():
    """清单无命令（只有 notes）→ 不注入。"""
    m = {"setup": [], "verify": [], "runtime": {}, "notes": "hello"}
    assert runtime_summary_for_prompt(m) == ""


def test_summary_for_prompt_none():
    assert runtime_summary_for_prompt(None) == ""


def test_system_prompt_includes_manifest(tmp_path):
    """有清单时 system prompt 包含 verify 命令；无清单时 prompt 不变。"""
    # 无清单
    prompt_without = SystemPromptBuilder.build(str(tmp_path), [])
    assert "Runtime Manifest" not in prompt_without

    # 有清单
    (tmp_path / ".litework").mkdir()
    (tmp_path / ".litework" / "project.json").write_text(json.dumps({
        "verify": ["pytest -q"],
    }), encoding="utf-8")
    prompt_with = SystemPromptBuilder.build(str(tmp_path), [])
    assert "pytest -q" in prompt_with
    assert "Runtime Manifest" in prompt_with


# ---------------------------------------------------------------- W2 门禁集成

async def test_gate_downgrades_when_verify_declared_but_not_run(tmp_path):
    """声明了 verify 命令但没执行 → verdict 从 pass 降级为 unknown。"""
    (tmp_path / ".litework").mkdir()
    (tmp_path / ".litework" / "project.json").write_text(json.dumps({
        "verify": ["pytest -q"],
    }), encoding="utf-8")

    registry = ToolRegistry()

    async def write_file(args):
        return "[Success] 已写入"

    registry.register("write_file", "写文件", {"type": "object"}, write_file)

    adapter = MockLLMAdapter([
        ("", [tool_call("write_file", '{"filePath":"a.txt","content":"hi"}')]),
        ("完成。", []),
    ])
    kernel = Kernel("gate-verify-test")
    loop = AgentLoop(kernel=kernel, adapter=adapter, registry=registry,
                     session_store=SessionStore(str(tmp_path / "sessions")),
                     max_steps=10)
    loop.workspace = str(tmp_path)
    loop.completion_gate = "advisory"

    await loop.run_task("写个文件", system_prompt="测试")

    # 改了文件 → changed-files pass；但没跑 verify（声明了 pytest 但没执行）
    # → verify-commands 应该是 unknown（不是 pass）
    assert loop.last_gate_result is not None
    statuses = {c["id"]: c["status"] for c in loop.last_gate_result["checks"]}
    assert statuses["changed-files"] == "pass"
    # verify-commands：跑了 write_file（无退出码）→ 本来就是 unknown；
    # 关键是：即使跑了别的命令全成功，声明了 pytest 没跑也必须是 unknown
    assert loop.last_gate_result["verdict"] != "pass"


async def test_gate_passes_when_verify_command_executed(tmp_path):
    """声明了 verify 命令且已执行（退出码 0）→ verdict 保持 pass。"""
    (tmp_path / ".litework").mkdir()
    (tmp_path / ".litework" / "project.json").write_text(json.dumps({
        "verify": ["python -m pytest -q"],
    }), encoding="utf-8")
    registry = ToolRegistry()

    async def write_file(args):
        return "[Success] 已写入"

    async def execute_command(args):
        return "[Exit Code]: 0\n[STDOUT]: all tests passed"

    registry.register("write_file", "写文件", {"type": "object"}, write_file)
    registry.register("execute_command", "执行命令", {"type": "object"}, execute_command)

    adapter = MockLLMAdapter([
        ("", [tool_call("write_file", '{"filePath":"a.py","content":"x"}')]),
        ("", [tool_call("execute_command", '{"command":"python -m pytest -q"}')]),
        ("全部通过，完成。", []),
    ])
    kernel = Kernel("gate-verify-pass")
    loop = AgentLoop(kernel=kernel, adapter=adapter, registry=registry,
                     session_store=SessionStore(str(tmp_path / "sessions")),
                     max_steps=10)
    loop.workspace = str(tmp_path)
    loop.completion_gate = "advisory"

    await loop.run_task("写个文件并测试", system_prompt="测试")

    assert loop.last_gate_result is not None
    assert loop.last_gate_result["verdict"] == "pass"

# ---------------------------------------------------------------- scaffold 集成

def test_scaffold_generates_manifest(tmp_path):
    """scaffold_project（新建项目 / 存量项目补建）自动生成 .litework/project.json。"""
    from litework.tools.project_scaffold import scaffold_project

    # 模拟一个 Python 项目
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")

    result = scaffold_project(str(tmp_path))
    assert ".litework/project.json" in result["created"]

    m = load_runtime_manifest(str(tmp_path))
    assert m is not None
    assert any("pytest" in v for v in m["verify"])

    # 幂等：再跑一次不会覆盖
    result2 = scaffold_project(str(tmp_path))
    assert ".litework/project.json" in result2["kept"]


def test_scaffold_preserves_existing_manifest(tmp_path):
    """已有的清单（用户自定义过）不被 scaffold 覆盖。"""
    from litework.tools.project_scaffold import scaffold_project

    (tmp_path / ".litework").mkdir()
    (tmp_path / ".litework" / "project.json").write_text(json.dumps({
        "verify": ["my-custom-verify"],
    }), encoding="utf-8")

    result = scaffold_project(str(tmp_path))
    assert ".litework/project.json" in result["kept"]
    m = load_runtime_manifest(str(tmp_path))
    assert m is not None and m["verify"] == ["my-custom-verify"]
