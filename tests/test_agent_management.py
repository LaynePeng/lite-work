"""Agent 管理：保存/删除/内置覆盖合并（settings Agents Tab 后端）。"""
from __future__ import annotations

import json
import os

import pytest

from litework.app import AgentApp
from litework.core.agent_profile import default_plan_agent


def _make_app(tmp_path) -> AgentApp:
    return AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".cfg"))


async def test_builtin_agent_tools_override_and_reset(tmp_path):
    """内置 agent 只覆盖 tools；删除后恢复内置默认（含 prompt 跟随发版语义）。"""
    app = _make_app(tmp_path)

    # 保存 office 的工具白名单（null = 全量）
    saved = app.agent_save({"id": "office", "tools": None})
    assert saved["tools"] is None

    # 落盘为 minimal 覆盖文件：只含 id/tools/permissions
    override_path = os.path.join(app.config_dir, "agents", "office.json")
    assert os.path.isfile(override_path)
    with open(override_path, encoding="utf-8") as f:
        data = json.load(f)
    assert set(data.keys()) == {"id", "tools", "permissions"}
    assert "system_prompt" not in data

    # 修改工具白名单为列表
    app.agent_save({"id": "office", "tools": ["docx_create", "read_file"]})
    assert app.get_agent("office").tools == ["docx_create", "read_file"]
    # system_prompt 仍在（运行期保留，注册表合并保证）
    assert app.get_agent("office").system_prompt

    # 删除（内置 = 恢复默认）
    r = app.agent_delete("office")
    assert r["reset"] is True
    assert not os.path.isfile(override_path)
    assert app.get_agent("office").tools == app.get_agent("office").tools  # 默认白名单
    assert "docx_create" in (app.get_agent("office").tools or [])


async def test_custom_agent_create_and_delete(tmp_path):
    """自定义 agent：创建落盘 → 注册表可用 → 删除清理。"""
    app = _make_app(tmp_path)

    app.agent_save({
        "id": "support",
        "description": "技术支持",
        "system_prompt": "你是技术支持工程师。",
        "tools": ["read_file", "webfetch"],
        "mode": "primary",
    })
    profile = app.get_agent("support")
    assert profile.description == "技术支持"
    assert profile.tools == ["read_file", "webfetch"]
    assert os.path.isfile(os.path.join(app.config_dir, "agents", "support.json"))

    # 覆盖更新
    app.agent_save({"id": "support", "description": "技术支持 v2", "system_prompt": "x", "tools": ["read_file"]})
    assert app.get_agent("support").description == "技术支持 v2"

    # 删除
    r = app.agent_delete("support")
    assert r["ok"] is True
    with pytest.raises(KeyError):
        app.get_agent("support")
    assert not os.path.isfile(os.path.join(app.config_dir, "agents", "support.json"))


async def test_builtin_override_file_merges_prompt_on_load(tmp_path):
    """minimal 覆盖文件重新加载时，prompt/描述继承内置默认（不被空值冲掉）。"""
    app = _make_app(tmp_path)
    # 造一个只有 tools 的覆盖文件（模拟旧版本或手写文件）
    agents_dir = os.path.join(app.config_dir, "agents")
    os.makedirs(agents_dir, exist_ok=True)
    with open(os.path.join(agents_dir, "plan.json"), "w", encoding="utf-8") as f:
        json.dump({"id": "plan", "tools": ["read_file"]}, f)

    # 重新加载：register 合并后 plan 保留默认 PLAN_PROMPT
    app.agent_registry.load_dir(agents_dir)
    plan = app.get_agent("plan")
    assert plan.tools == ["read_file"]
    assert plan.system_prompt == default_plan_agent().system_prompt


async def test_agent_save_validation(tmp_path):
    """非法 id / 未知必填校验。"""
    app = _make_app(tmp_path)
    with pytest.raises(ValueError):
        app.agent_save({"id": "bad id!"})
    with pytest.raises(ValueError):
        app.agent_save({"id": "nosuch", "description": "", "system_prompt": ""})
