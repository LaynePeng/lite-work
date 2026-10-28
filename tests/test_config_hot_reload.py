# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""配置热生效：保存后常驻挂件必须看到新值，而不是等重启或下一个任务。

这里守的是三类回归（都属于「改了配置要重启」）：

1. **对象身份**：`_write_config_merged` 曾把 `self.config` 重新绑定成新 dict，
   而 `WatchScheduler(config=self.config)` 之类的长期引用会永久停在第一次保存
   时的快照上——插件配置改了，盯梢探针仍读到旧值。
2. **构造期快照**：审批门/ShellPlugin 在 `__init__` 里把超时拷成成员，
   不主动回写就永远停在旧值。
3. **插件成员参数**：插件在 `install()` 里把配置拷贝进成员（限流/缓存/路径），
   靠 `app.config` 就地更新拿不到新值——需要 `on_config_changed` 钩子。
"""
from __future__ import annotations

import json
from typing import Any, Dict

import pytest

from litework.app import AgentApp

PLUGIN_NAME = "email-plugin"


def _make_app(tmp_path, config: Dict[str, Any] | None = None) -> AgentApp:
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        json.dumps(config if config is not None else {"max_steps": 100}),
        encoding="utf-8",
    )
    return AgentApp(config_dir=str(cfg_dir))


def test_config_object_identity_survives_save(tmp_path):
    """保存配置后 self.config 仍是同一个对象（长期持有者才能看到新值）。"""
    app = _make_app(tmp_path)
    held = app.config

    app.save_config({PLUGIN_NAME: {"email_user": "a@x.com"}})
    assert app.config is held
    app.save_config({PLUGIN_NAME: {"email_user": "b@x.com"}})
    assert app.config is held
    # 第二次保存的新值，持有旧引用的一方也看得到
    assert held[PLUGIN_NAME] == {"email_user": "b@x.com"}


def test_watch_scheduler_view_tracks_plugin_config(tmp_path):
    """盯梢调度器按引用持有 app.config：插件配置改动必须对它即时可见。

    探针通过 `ProbeContext.config` 读插件配置（astock/email 的盯盘开关、
    发信阈值都在这里），所以这条链路是「插件配置热生效」的主干。
    """
    app = _make_app(tmp_path)
    # 模拟 start_watch_scheduler 的 config=self.config
    scheduler_view = app.config

    app.save_config({PLUGIN_NAME: {"watch_enabled": True}})
    assert (scheduler_view.get(PLUGIN_NAME) or {}).get("watch_enabled") is True

    # 用户在设置页暂停盯盘 → 探针下一次探测就该看到 False
    app.save_config({PLUGIN_NAME: {"watch_enabled": False}})
    assert (scheduler_view.get(PLUGIN_NAME) or {}).get("watch_enabled") is False


def test_disk_merge_still_keeps_foreign_keys(tmp_path):
    """原地合并不能破坏既有语义：磁盘上多出来的键（插件/用户手写）要保留。"""
    app = _make_app(tmp_path, {"max_steps": 100, "third-party-plugin": {"token": "t"}})
    app.save_config({"tool_timeout": 30})

    on_disk = json.loads((tmp_path / "cfg" / "config.json").read_text(encoding="utf-8"))
    assert on_disk["tool_timeout"] == 30
    assert on_disk["third-party-plugin"] == {"token": "t"}
    assert on_disk["max_steps"] == 100


def test_approval_timeout_applies_without_restart(tmp_path):
    """审批门的超时是构造期快照：保存后必须立刻生效（含提问门）。"""
    app = _make_app(tmp_path)
    assert app.approval_gate.timeout_seconds == 600
    assert app.question_gate.timeout_seconds == 600

    app.save_config({"approval_timeout": 120})
    assert app.approval_gate.timeout_seconds == 120
    assert app.question_gate.timeout_seconds == 120

    # 非法值不该让保存失败，回落到默认 600
    app.save_config({"approval_timeout": "abc"})
    assert app.approval_gate.timeout_seconds == 600


def test_tool_timeout_invalidates_shell_plugin_cache(tmp_path):
    """ShellPlugin 构造时固化 tool_timeout：保存后清缓存，下个内核按新值重建。"""
    app = _make_app(tmp_path)
    app._shell_plugins["/tmp/ws"] = object()  # 占位：模拟已有缓存实例

    app.save_config({"tool_timeout": 45})
    assert app._shell_plugins == {}


def test_plugin_config_change_reaches_live_plugin_hook(tmp_path, monkeypatch):
    """插件配置保存 → 真插件实例的 on_config_changed 被调用（不带重启）。"""
    from litework.core.types import Plugin

    calls: list[Dict[str, Any]] = []

    class _Probe(Plugin):
        name = PLUGIN_NAME

        def on_config_changed(self, config: Dict[str, Any]) -> None:
            calls.append(dict(config))

    probe = _Probe()
    app = _make_app(tmp_path)
    monkeypatch.setattr(app, "_ensure_local_plugins", lambda: [probe])
    monkeypatch.setattr(
        "litework.tools.plugin_loader.load_builtin_plugins", lambda: []
    )

    app.save_config({PLUGIN_NAME: {"email_user": "me@x.com"}})

    assert len(calls) == 1
    # 回调拿到的是全量配置，插件自己按命名空间取值
    assert calls[0][PLUGIN_NAME] == {"email_user": "me@x.com"}


def test_plugin_hook_only_fires_for_changed_namespace(tmp_path, monkeypatch):
    """只通知本次改动过的插件命名空间（其余插件不被无谓打扰）。"""
    from litework.core.types import Plugin

    hits: list[str] = []

    class _A(Plugin):
        name = "plugin-a"

        def on_config_changed(self, config: Dict[str, Any]) -> None:
            hits.append("a")

    class _B(Plugin):
        name = "plugin-b"

        def on_config_changed(self, config: Dict[str, Any]) -> None:
            hits.append("b")

    app = _make_app(tmp_path)
    monkeypatch.setattr(app, "_ensure_local_plugins", lambda: [_A(), _B()])
    monkeypatch.setattr(
        "litework.tools.plugin_loader.load_builtin_plugins", lambda: []
    )

    app.save_config({"plugin-a": {"x": 1}})
    assert hits == ["a"]

    hits.clear()
    app.save_config({"tool_timeout": 30})  # 与插件无关的键
    assert hits == []


def test_plugin_hook_failure_does_not_block_save(tmp_path, monkeypatch):
    """插件钩子抛异常不能影响配置保存（配置已落盘，插件问题记日志）。"""
    from litework.core.types import Plugin

    class _Bad(Plugin):
        name = PLUGIN_NAME

        def on_config_changed(self, config: Dict[str, Any]) -> None:
            raise RuntimeError("插件内部错误")

    app = _make_app(tmp_path)
    monkeypatch.setattr(app, "_ensure_local_plugins", lambda: [_Bad()])
    monkeypatch.setattr(
        "litework.tools.plugin_loader.load_builtin_plugins", lambda: []
    )

    app.save_config({PLUGIN_NAME: {"email_user": "me@x.com"}})

    on_disk = json.loads((tmp_path / "cfg" / "config.json").read_text(encoding="utf-8"))
    assert on_disk[PLUGIN_NAME] == {"email_user": "me@x.com"}


@pytest.mark.asyncio
async def test_config_endpoint_hot_applies_approval_timeout(tmp_path):
    """端到端：POST /api/config → 审批门立刻用新超时（无需重启）。"""
    from httpx import ASGITransport, AsyncClient

    from litework.server.app import create_app

    agent_app = _make_app(tmp_path)
    fastapi_app = create_app(agent_app)
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/config", json={"updates": {"approval_timeout": 90}})
        assert r.status_code == 200
    assert agent_app.approval_gate.timeout_seconds == 90
