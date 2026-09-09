# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""协作模式插件测试：安装 → 发现 → 选择 → 配方注入 → API 列表。

模式包格式（社区 lite-work-plugins 仓库 plugins/collab-*）：
    collab-<mode>/
      plugin.py    # CollabModePlugin 子类（mode_name/display_name/description）
      recipe.md    # 模式配方（选中时替换内置派生指引）
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from litework.app import AgentApp
from litework.server.app import create_app

MODE_PKG_PLUGIN = '''from litework.orchestration.collab_policy import CollabModePlugin


class MeetingCollabMode(CollabModePlugin):
    name = "collab-meeting"
    version = "1.0.0"
    description = "协作模式：会议"
    mode_name = "meeting"
    display_name = "会议（群聊共识）"
'''

MODE_RECIPE = "# 协作模式：会议（群聊共识）\n\n轮流发言，看到彼此观点后收敛。\n"


@pytest.fixture
def app_with_mode_plugin(tmp_path):
    config_dir = tmp_path / ".lite-work"
    pkg = config_dir / "plugins" / "collab-meeting"
    pkg.mkdir(parents=True)
    (pkg / "plugin.py").write_text(MODE_PKG_PLUGIN, encoding="utf-8")
    (pkg / "recipe.md").write_text(MODE_RECIPE, encoding="utf-8")
    # 插件自带 logo（icon.svg）：对话框选择器经 /api/plugins/{name}/icon 加载
    (pkg / "icon.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48"><circle cx="24" cy="24" r="20" fill="#4f8cff"/></svg>',
        encoding="utf-8",
    )
    app = AgentApp(workspace=str(tmp_path), config_dir=str(config_dir))
    app.refresh_model_meta = lambda: False
    return app


def test_collab_mode_plugin_discovered(app_with_mode_plugin):
    """本地插件目录中的 CollabModePlugin 被发现，基类自身不被实例化。"""
    modes = app_with_mode_plugin.collab_modes()
    assert [m.mode_name for m in modes] == ["meeting"]
    assert modes[0].display_name == "会议（群聊共识）"


def test_get_collab_policy_resolves_installed_mode(app_with_mode_plugin):
    """config.collab_policy 命中已安装模式 → 插件适配策略 + recipe.md 配方。"""
    from litework.orchestration.collab_policy import (
        DefaultCollabPolicy,
        PluginCollabPolicy,
        UserRecipeCollabPolicy,
        get_collab_policy,
    )

    app = app_with_mode_plugin
    # 默认
    assert isinstance(get_collab_policy(app), DefaultCollabPolicy)
    # 选中已安装模式
    app.config["collab_policy"] = "meeting"
    policy = get_collab_policy(app)
    assert isinstance(policy, PluginCollabPolicy)
    assert "轮流发言" in policy.recipe()
    # Tier 1 原始配方文本优先级最高
    app.config["collab_recipe"] = "自定义配方"
    assert isinstance(get_collab_policy(app), UserRecipeCollabPolicy)
    # 未知模式名回退 default
    del app.config["collab_recipe"]
    app.config["collab_policy"] = "no-such-mode"
    assert isinstance(get_collab_policy(app), DefaultCollabPolicy)


def test_collab_modes_api_lists_builtin_and_installed(app_with_mode_plugin):
    app = app_with_mode_plugin
    fast = create_app(app, token=None)
    with TestClient(fast) as client:
        r = client.get("/api/collab/modes")
        assert r.status_code == 200
        modes = r.json()["modes"]
        by_name = {m["name"]: m for m in modes}
        assert by_name["default"]["source"] == "builtin"
        assert by_name["review"]["source"] == "builtin"
        assert by_name["meeting"]["source"] == "plugin"
        assert by_name["meeting"]["display_name"] == "会议（群聊共识）"


def test_selected_mode_replaces_spawn_agent_recipe(app_with_mode_plugin):
    """选中模式后，spawn_agent 工具描述携带该模式配方（替换内置路由指引）。"""
    app = app_with_mode_plugin
    app.config["collab_policy"] = "meeting"
    registry = app.build_registry()
    spawn = next(t for t in registry.get_tools() if t.name == "spawn_agent")
    assert "轮流发言" in spawn.description
    assert "模式选择（按任务特征路由）" not in spawn.description  # 内置指引被替换


def test_mode_plugin_hooks_fire_isolated(tmp_path):
    """插件钩子（on_agent_complete）在子 Agent 终态时触发，异常被隔离。"""
    import asyncio

    from litework.orchestration.agent_manager import SessionAgentManager
    from litework.orchestration.collab_policy import CollabContext, fire_collab_hook

    config_dir = tmp_path / ".lite-work"
    pkg = config_dir / "plugins" / "collab-boom"
    pkg.mkdir(parents=True)
    (pkg / "plugin.py").write_text(
        "from litework.orchestration.collab_policy import CollabModePlugin\n"
        "class BoomMode(CollabModePlugin):\n"
        "    name = 'collab-boom'\n"
        "    version = '1.0.0'\n"
        "    mode_name = 'boom'\n"
        "    fired = []\n"
        "    async def on_agent_complete(self, ctx):\n"
        "        self.fired.append(ctx.session_id)\n"
        "        raise RuntimeError('钩子爆炸（应被隔离）')\n",
        encoding="utf-8",
    )
    app = AgentApp(workspace=str(tmp_path), config_dir=str(config_dir))
    app.config["collab_policy"] = "boom"
    (app.agent_managers.setdefault("s1", SessionAgentManager(app, "s1")))

    fired_mode = app.collab_modes()[0]

    async def run():
        # 直接经内核入口触发（真实路径是 _enqueue_notification 里的调用）
        fire_collab_hook(app, "on_agent_complete",
                         CollabContext(app=app, session_id="s1"))
        await asyncio.sleep(0.2)  # fire-and-forget 任务执行窗口

    asyncio.run(run())
    assert fired_mode.fired == ["s1"]  # 钩子已执行
    # 异常未向外传播（run 正常返回即证明）；策略仍可继续解析


def test_mode_plugin_installs_via_community_flow(tmp_path):
    """端到端：模拟社区安装（本地目录 → plugins/）后模式立即可选。"""
    import shutil

    src_pkg = tmp_path / "src-pkg"
    src_pkg.mkdir()
    (src_pkg / "plugin.py").write_text(MODE_PKG_PLUGIN, encoding="utf-8")
    (src_pkg / "recipe.md").write_text(MODE_RECIPE, encoding="utf-8")

    config_dir = tmp_path / ".lite-work"
    app = AgentApp(workspace=str(tmp_path), config_dir=str(config_dir))
    assert app.collab_modes() == []

    # install_from_source（社区/本地目录统一入口；GitHub 子目录安装时
    # 目录名即插件名，本地目录场景显式传 name 保持同语义）
    results = app.plugins_install(str(src_pkg), name="collab-meeting")
    assert any(p["name"] == "collab-meeting" for p in results)
    modes = app.collab_modes()
    assert [m.mode_name for m in modes] == ["meeting"]


# ---------------------------------------------------------------- 图标与会话级选择

def test_plugin_icon_endpoint(app_with_mode_plugin):
    """插件自带 icon.svg → /api/plugins/{name}/icon 返回图片；无图标 404；路径穿越被拒。"""
    app = app_with_mode_plugin
    fast = create_app(app, token=None)
    with TestClient(fast) as client:
        r = client.get("/api/plugins/collab-meeting/icon")
        assert r.status_code == 200
        assert "svg" in (r.headers.get("content-type") or "")
        # 未安装的插件 → 404（前端回退默认图标）
        r = client.get("/api/plugins/no-such-plugin/icon")
        assert r.status_code == 404
        # 路径穿越被安全校验拒绝
        r = client.get("/api/plugins/..%2F..%2Fetc/icon")
        assert r.status_code == 404


def test_collab_modes_api_includes_icon_url(app_with_mode_plugin):
    app = app_with_mode_plugin
    fast = create_app(app, token=None)
    with TestClient(fast) as client:
        r = client.get("/api/collab/modes")
        meeting = next(m for m in r.json()["modes"] if m["name"] == "meeting")
        assert meeting["icon_url"] == "/api/plugins/collab-meeting/icon"
        default = next(m for m in r.json()["modes"] if m["name"] == "default")
        assert default["icon_url"] is None


def test_session_collab_mode_set_clear_and_unknown(app_with_mode_plugin):
    """对话框选择器：会话级模式写入/清除/未知模式拒绝。"""
    app = app_with_mode_plugin
    fast = create_app(app, token=None)
    with TestClient(fast) as client:
        sid = client.post("/api/sessions", json={}).json()["session_id"]

        r = client.post(f"/api/sessions/{sid}/collab", json={"mode": "meeting"})
        assert r.status_code == 200 and r.json()["mode"] == "meeting"
        assert app.session_store.load(sid).metadata["collab_mode"] == "meeting"

        # 未知模式 → 400（前端选择器不会出现，但 API 层面挡住）
        r = client.post(f"/api/sessions/{sid}/collab", json={"mode": "no-such"})
        assert r.status_code == 400

        # 清除
        r = client.post(f"/api/sessions/{sid}/collab", json={"mode": None})
        assert r.status_code == 200 and r.json()["mode"] is None
        assert "collab_mode" not in app.session_store.load(sid).metadata

        r = client.post("/api/sessions/no-such/collab", json={"mode": "meeting"})
        assert r.status_code == 404


def test_session_collab_mode_overrides_global(app_with_mode_plugin):
    """会话级覆盖优先于全局 config：get_collab_policy(session_id=...) 解析。"""
    from litework.orchestration.collab_policy import (
        DefaultCollabPolicy,
        PluginCollabPolicy,
        get_collab_policy,
    )

    app = app_with_mode_plugin
    # 全局配置为 default，会话选了 meeting → 会话覆盖生效
    sid = app.session_store.save("s1", [], {}) or "s1"
    app.session_store.update_metadata("s1", {"collab_mode": "meeting"})
    policy = get_collab_policy(app, session_id="s1")
    assert isinstance(policy, PluginCollabPolicy)
    assert "轮流发言" in policy.recipe()
    # 无覆盖的会话 → 全局 default
    app.session_store.save("s2", [], {})
    assert isinstance(get_collab_policy(app, session_id="s2"), DefaultCollabPolicy)


def test_task_prompt_includes_session_collab_mode(app_with_mode_plugin):
    """会话选定模式后，任务 system prompt 注入该模式配方（TaskHandle 路径）。"""
    import time

    app = app_with_mode_plugin
    app.session_store.save("s-collab", [], {})
    app.session_store.update_metadata("s-collab", {"collab_mode": "meeting"})
    app._mock_adapter = __import__("tests.conftest", fromlist=["MockLLMAdapter"]).MockLLMAdapter(
        [("好的。", [])]
    )
    fast = create_app(app, token=None)
    with TestClient(fast) as client:
        r = client.post("/api/chat", json={"session_id": "s-collab", "prompt": "开始"})
        assert r.status_code == 200
        msgs = []
        for _ in range(200):
            snap = app.session_store.load("s-collab")
            msgs = snap.messages if snap else []
            if any(m.role == "assistant" for m in msgs):
                break
            time.sleep(0.05)
        system = next((m for m in msgs if m.role == "system"), None)
        assert system is not None
        assert "本会话协作模式" in system.content
        assert "轮流发言" in system.content
