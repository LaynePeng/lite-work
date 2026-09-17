# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""插件列表加固测试：单个坏插件不得让整张列表失败（设置页"插件加载失败连累模型页"的根因之一）。

覆盖：
- 语法错误插件 / 依赖安装失败的目录插件 → 列表仍返回，且该插件带 error 字段；
- 好插件不受影响（错误隔离）；
- 列表结果缓存 + 装/删插件后失效（_invalidate_plugin_cache）。
"""
from __future__ import annotations

import json
import os


def _write_plugin(root: str, name: str, body: str) -> str:
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "plugin.py"), "w", encoding="utf-8") as f:
        f.write(body)
    return d


GOOD_PLUGIN = '''
from litework.core.types import ToolDefinition
from litework.tools.plugin import ToolPlugin


class GoodPlugin(ToolPlugin):
    name = "goodplug"
    description = "好插件"
    version = "1.0.0"

    def __init__(self):
        pass

    def get_tools(self):
        return [ToolDefinition(name="t_good", description="d", parameters={})]
'''

BROKEN_SYNTAX = '''
def this is not valid python
'''


def test_bad_plugin_does_not_break_list(tmp_path):
    from litework.tools.plugin_loader import list_plugins

    cfg = str(tmp_path)
    plugins_dir = os.path.join(cfg, "plugins")
    os.makedirs(plugins_dir, exist_ok=True)
    _write_plugin(plugins_dir, "goodplug", GOOD_PLUGIN)
    _write_plugin(plugins_dir, "brokenplug", BROKEN_SYNTAX)

    meta = list_plugins(cfg)  # 不应抛异常
    by = {p["name"]: p for p in meta}
    assert "goodplug" in by and "brokenplug" in by
    # 好插件正常
    assert by["goodplug"]["error"] == ""
    assert "t_good" in by["goodplug"]["tools"]
    # 坏插件带可见错误，而不是让整张列表 500
    assert by["brokenplug"]["error"], "坏插件应带 error 字段"


def test_plugins_list_cached_and_invalidated(tmp_path):
    from litework.app import AgentApp

    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lc"))
    plugins_dir = os.path.join(app.config_dir, "plugins")
    os.makedirs(plugins_dir, exist_ok=True)
    _write_plugin(plugins_dir, "goodplug", GOOD_PLUGIN)

    first = app.plugins_list()
    assert any(p["name"] == "goodplug" for p in first)

    # 外部新增插件：TTL 内走缓存（读不到新插件），失效后能看到
    # 注意：列表项 name 取目录名（非类属性 name）
    _write_plugin(plugins_dir, "late", GOOD_PLUGIN.replace("goodplug", "lateplug"))
    assert all(p["name"] != "late" for p in app.plugins_list())
    app._invalidate_plugin_cache()
    assert any(p["name"] == "late" for p in app.plugins_list())


def test_local_plugin_load_failure_cached_as_empty(tmp_path, monkeypatch):
    """本地插件加载整体异常时按空处理并缓存，不 500、不每次重试。"""
    from litework import app as app_mod

    app = app_mod.AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lc"))
    calls = {"n": 0}

    def boom(config_dir):
        calls["n"] += 1
        raise RuntimeError("plugin dir exploded")

    monkeypatch.setattr(app_mod, "load_plugins", boom, raising=False)
    # 通过模块属性注入：_ensure_local_plugins 内部 from .tools.plugin_loader import load_plugins
    import litework.tools.plugin_loader as pl
    monkeypatch.setattr(pl, "load_plugins", boom)

    assert app.plugins_builtin()  # 不抛异常
    assert app._local_plugins == []
    app.plugins_builtin()
    assert calls["n"] == 1, "失败应缓存，不重复重试"
    # 失效后可重试
    app._invalidate_plugin_cache()
    app.plugins_builtin()
    assert calls["n"] == 2
