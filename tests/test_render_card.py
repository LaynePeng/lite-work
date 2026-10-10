# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""富内容卡片（render_card）：工具落盘 + 读取端点 + 安全边界。

LLM 上下文纪律：工具结果只含卡片 id（HTML 不回填）；<script> 剔除；
card_id 格式校验防路径注入。
"""
from __future__ import annotations

import re

import pytest

from litework.tools.render_card import (
    RenderCardPlugin,
    card_path,
    cards_dir,
    is_valid_card_id,
)
from litework.tools.todos import current_session_id


@pytest.fixture
async def live_client(tmp_path):
    """与 test_server.py 同款：真实 uvicorn + httpx 客户端（端点测试用）。"""
    from litework.app import AgentApp
    from litework.server.app import create_app
    from tests.conftest import live_server

    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    app.refresh_model_meta = lambda: False
    fast_app = create_app(app, token=None)
    async with live_server(fast_app) as (client, server):
        yield client, app, server


class _FakeApp:
    def __init__(self, config_dir):
        self.config_dir = config_dir


async def test_render_card_persists_and_compact_result(tmp_path):
    """render_card：HTML 落盘（含元信息头 + script 剔除），result 只含卡片 id。"""
    app = _FakeApp(str(tmp_path))
    plugin = RenderCardPlugin(app)
    html = ('<div><img src="a.png"><script>alert(1)</script>'
            '<h3>运动相机对比</h3></div>')
    token = current_session_id.set("session_rc_1")
    try:
        result = await plugin.execute("render_card", {
            "html": html, "title": "运动相机对比", "height": 480,
        })
    finally:
        current_session_id.reset(token)

    m = re.search(r"\[Rich Card\]:\s*(card_[a-z0-9]+)", result)
    assert m, f"result 应含卡片 id：{result}"
    card_id = m.group(1)
    # result 不回填 HTML（上下文纪律）
    assert "运动相机对比</h3>" not in result
    assert "<script>" not in result

    path = card_path(app.config_dir, "session_rc_1", card_id)
    content = open(path, encoding="utf-8").read()
    assert "litework-card: id=" in content          # 元信息头
    assert "height=480" in content
    assert "运动相机对比" in content                  # 正文保留
    assert "<script>" not in content                  # script 已剔除
    assert '<img src="a.png">' in content            # 相对路径原样（前端 rewrite）


async def test_render_card_rejects_empty_and_oversize(tmp_path):
    app = _FakeApp(str(tmp_path))
    plugin = RenderCardPlugin(app)
    token = current_session_id.set("session_rc_2")
    try:
        assert "html 为空" in await plugin.execute("render_card", {"html": "  "})
        big = "<div>" + "x" * (300 * 1024) + "</div>"
        assert "上限" in await plugin.execute("render_card", {"html": big})
    finally:
        current_session_id.reset(token)


async def test_render_card_requires_session_context(tmp_path):
    plugin = RenderCardPlugin(_FakeApp(str(tmp_path)))
    assert "无会话上下文" in await plugin.execute("render_card", {"html": "<b>x</b>"})


def test_card_id_validation_blocks_path_injection():
    assert is_valid_card_id("card_abc123def4")
    assert not is_valid_card_id("../../etc/passwd")
    assert not is_valid_card_id("card_短")
    assert not is_valid_card_id("")
    # card_path 对非法 id 抛 ValueError（端点转 404）
    try:
        card_path("/tmp/x", "s", "../escape")
        raise AssertionError("应抛 ValueError")
    except ValueError:
        pass


async def test_card_endpoint_read_and_404(live_client, tmp_path):
    """GET /api/cards/{sid}/{id}：正常读取落盘 HTML；非法 id / 不存在 → 404。"""
    import os

    c, app, _ = live_client
    sid = "session_rc_ep"
    out_dir = cards_dir(app.config_dir, sid)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "card_abc123def4.html"), "w", encoding="utf-8") as f:
        f.write("<!-- litework-card: id=card_abc123def4 title=t height=0 -->\n<p>卡片内容</p>")

    r = await c.get(f"/api/cards/{sid}/card_abc123def4")
    assert r.status_code == 200
    assert "卡片内容" in r.json()["html"]

    # 非法 id（路径注入）→ 404
    r = await c.get(f"/api/cards/{sid}/..%2F..%2Fetc")
    assert r.status_code == 404
    # 不存在的卡片 → 404
    r = await c.get(f"/api/cards/{sid}/card_0000000000")
    assert r.status_code == 404
