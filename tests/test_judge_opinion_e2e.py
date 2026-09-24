# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors
#
"""端到端：判定意见要**真的进审批事件**（而不只是后端 dict）。

现有单测只覆盖到"插件在 data 上写了 judge_opinion"（`产出物/test_jev_not_started.py`）。
这里用**真实 AgentApp + 真实 SecurityPlugin + 真实审批门**跑一遍：
删文件工具 → SecurityPlugin 请求审批 → 事件里必须带 `judge_opinion`（插件自报 source）。

这才是"审批卡上看得见意见"的端到端保证：
  插件 before_tool → data["judge_opinion"] → SecurityPlugin 暂存 → approval:request 负载
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest

from litework.app import AgentApp

PLUGIN_SRC = Path(os.environ.get("JEV_PLUGIN_SRC",
                                 Path.home() / ".lite-work" / "plugins" / "jev"))


@pytest.fixture()
def app_with_plugin(tmp_path):
    """真实 AgentApp：临时 config_dir 里装 jev 插件 + 开自动门禁。"""
    if not (PLUGIN_SRC / "plugin.py").is_file():
        pytest.skip("未安装 jev 插件（%s）" % PLUGIN_SRC)
    dest = tmp_path / "plugins" / "jev"
    dest.mkdir(parents=True)
    shutil.copy2(PLUGIN_SRC / "plugin.py", dest / "plugin.py")
    (tmp_path / "ws").mkdir()
    (tmp_path / "config.json").write_text(json.dumps({
        "jev": {"api_key": "sk-test", "base_url": "https://gw.example/v1/systemone",
                "model": "jev-1.13", "auto_gate_enabled": True,
                "confidence_threshold": 0.6, "tools_enabled": True},
    }), encoding="utf-8")

    app = AgentApp(workspace=str(tmp_path / "ws"), config_dir=str(tmp_path))
    plugins = [p for p in app._ensure_local_plugins() if getattr(p, "name", "") == "jev"]
    assert plugins, "临时目录里的 jev 插件未被子插件加载器发现"
    plugin = plugins[0]
    # 防回归：门禁里 `await self.judge(...)`——judge 必须是 async（曾因补丁丢失 async
    # 导致真实路径每次判定都 TypeError: 'dict' object can't be awaited）
    import asyncio
    assert asyncio.iscoroutinefunction(plugin.judge), "judge 必须是 async（门禁 await 它）"

    async def _stub_judge(_cfg, _tool, _args):
        """替掉真实网关调用（不联网）：判定为「拦截」但置信 0.91（< 0.95 硬拦线）。"""
        return {"choice": "block", "confidence": 0.91, "why": "该操作不可逆",
                "probabilities": {"拦截": 0.88, "放行": 0.09, "请人确认": 0.03}}

    plugin.judge = _stub_judge        # 实例级替换：门禁钩子用的是 self.judge
    return app, plugin


def test_approval_event_carries_judge_opinion(app_with_plugin, tmp_path):
    app, _plugin = app_with_plugin

    registry = app.build_registry()
    kernel = app.create_kernel("e2e-approval", registry=registry)

    captured: list = []

    def _on_approval(payload):
        captured.append(payload)
        # 立刻同意（不让测试等 600s 超时）
        app.approval_gate.resolve(payload["id"], approved=True, by="user")

    kernel.events.on("approval:request", _on_approval)

    data = {"toolName": "delete_file",
            "args": {"filePath": str(tmp_path / "victim.txt")},
            "cancel": False, "reason": ""}
    out = asyncio.run(kernel.before_tool.run(kernel.ctx, data))

    # ① SecurityPlugin 确实请求了审批（否则本用例没意义）
    assert captured, "delete_file 未触发审批（SecurityPlugin 分支没走到）"
    ev = captured[0]

    # ② 审批事件里带着判定意见（这正是审批卡要渲染的东西）
    op = ev.get("judge_opinion")
    assert isinstance(op, dict), f"审批事件缺 judge_opinion：{sorted(ev.keys())}"
    assert op["source"] == "Jev", "插件自报的展示名应在 source 里"
    assert op["level"] == "高风险" and op["choice"] == "block"
    assert op["confidence"] == 0.91 and op["threshold"] == 0.6
    assert op["why"] == "该操作不可逆"
    assert op["probabilities"] == {"拦截": 0.88, "放行": 0.09, "请人确认": 0.03}

    # ③ 0.91 < 0.95 → 插件**只给意见、不替代用户**：最终由用户同意决定
    assert out.get("cancel") is False, "0.91 不该硬拦（拦截权在用户/SecurityPlugin）"


def test_judge_opinion_absent_when_plugin_disabled(tmp_path):
    """插件未启用（或没装）时，审批事件里 judge_opinion 恒为 None，且链路正常。"""
    dest = tmp_path / "plugins"
    dest.mkdir()
    (tmp_path / "ws").mkdir()
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    app = AgentApp(workspace=str(tmp_path / "ws"), config_dir=str(tmp_path))
    registry = app.build_registry()
    kernel = app.create_kernel("e2e-no-plugin", registry=registry)

    captured: list = []

    def _on_approval(payload):
        captured.append(payload)
        app.approval_gate.resolve(payload["id"], approved=True, by="user")

    kernel.events.on("approval:request", _on_approval)

    data = {"toolName": "delete_file",
            "args": {"filePath": str(tmp_path / "victim.txt")},
            "cancel": False, "reason": ""}
    out = asyncio.run(kernel.before_tool.run(kernel.ctx, data))

    assert captured, "无插件时 delete_file 仍应走审批（核心行为不变）"
    assert captured[0].get("judge_opinion") is None, "无插件时不该有判定意见"
    assert out.get("cancel") is False
