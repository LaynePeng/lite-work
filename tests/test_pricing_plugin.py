# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""定价插件（官方价 + 分时窗口）回归测试。

覆盖：官方页解析器、模型指纹（自定义中转）、分时窗口求值、插件装载
（内置/本地覆盖）、同步落盘与失败保护、resolve_pricing 分层优先级、
AgentLoop 按计价时刻选档、payload 元信息。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from litework.llm.pricing_provider import PricingProvider, is_off_peak_now
from litework.tools.plugin_loader import load_pricing_builtin

# ------------------------------------------------------------ fixtures

DEEPSEEK_HTML = """
<table style="text-align:center">
<tr><td colspan="3" style="text-align:center">MODEL</td>
    <td>deepseek-flash<sup>(1)</sup></td><td>deepseek-v4-pro<sup>(2)</sup></td></tr>
<tr><td colspan="3">CONTEXT LENGTH</td><td colspan="2">1M</td></tr>
<tr><td rowspan="6">PRICING<sup>(3)</sup></td>
    <td rowspan="2">1M INPUT TOKENS<br>(CACHE HIT)</td><td>OFF-PEAK</td><td>$0.003</td><td>$0.022</td></tr>
<tr><td>PEAK</td><td>$0.006</td><td>$0.044</td></tr>
<tr><td rowspan="2">1M INPUT TOKENS<br>(CACHE MISS)</td><td>OFF-PEAK</td><td>$0.15</td><td>$0.66</td></tr>
<tr><td>PEAK</td><td>$0.3</td><td>$1.32</td></tr>
<tr><td rowspan="2">1M OUTPUT TOKENS</td><td>OFF-PEAK</td><td>$0.6</td><td>$1.98</td></tr>
<tr><td>PEAK</td><td>$1.2</td><td>$3.96</td></tr>
</table>
<p>(3) Off-peak rates are half of the peak rates. Peak hours are
01:00 - 04:00 and 06:00 - 10:00 UTC, Monday through Friday.</p>
"""

KIMI_MD = """
rows={[
["kimi-k3", "1M tokens", <>{"$"}0.30</>, <>{"$"}3.00</>, <>{"$"}15.00</>, "1,048,576 tokens"],
["kimi-k2.7-code", "1M tokens", <>{"$"}0.19</>, <>{"$"}0.95</>, <>{"$"}4.00</>, "262,144 tokens"],
]}
"""


def _plugin():
    plugins = load_pricing_builtin()
    assert plugins, "内置定价插件未加载"
    return plugins[0]


def _app(tmp_path):
    from litework.app import AgentApp

    return AgentApp(config_dir=str(tmp_path))


# ------------------------------------------------------------ 解析器


def test_deepseek_parser_reads_both_tiers():
    """官方页同时给 peak/off-peak 两档 → 两档都要解出来（不做「半价」假设）。"""
    import importlib.util

    from litework.tools.plugin_loader import builtin_plugins_root

    plugin_py = os.path.join(builtin_plugins_root(), "pricing-plugin", "plugin.py")
    spec = importlib.util.spec_from_file_location("_pricing_plugin_under_test", plugin_py)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)

    out = mod.parse_deepseek_pricing(DEEPSEEK_HTML)
    flash = out["deepseek-flash"]
    assert flash["peak"] == {"cache_hit_per_mtok": 0.006, "input_per_mtok": 0.3,
                             "output_per_mtok": 1.2}
    assert flash["off_peak"] == {"cache_hit_per_mtok": 0.003, "input_per_mtok": 0.15,
                                 "output_per_mtok": 0.6}
    pro = out["deepseek-v4-pro"]
    assert pro["peak"]["input_per_mtok"] == 1.32
    assert pro["off_peak"]["input_per_mtok"] == 0.66

    with pytest.raises(ValueError):
        mod.parse_deepseek_pricing("<html><body>改版了</body></html>")


def test_kimi_parser_reads_markdown_rows():
    import importlib.util

    from litework.tools.plugin_loader import builtin_plugins_root

    plugin_py = os.path.join(builtin_plugins_root(), "pricing-plugin", "plugin.py")
    spec = importlib.util.spec_from_file_location("_pricing_plugin_under_test2", plugin_py)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)

    out = mod.parse_kimi_pricing(KIMI_MD)
    assert out["kimi-k3"]["peak"] == {"cache_hit_per_mtok": 0.3, "input_per_mtok": 3.0,
                                      "output_per_mtok": 15.0}
    assert out["kimi-k2.7-code"]["peak"]["output_per_mtok"] == 4.0
    with pytest.raises(ValueError):
        mod.parse_kimi_pricing("页面结构变了")


# ------------------------------------------------------------ 分时窗口（通用求值）


def test_off_peak_window_boundaries():
    """窗口是数据：UTC 周一至周五 01:00-04:00、06:00-10:00 为高峰，其余空闲。"""
    entry = {
        "off_peak": {"input_per_mtok": 0.15},
        "peak_window": {"tz_offset_minutes": 0, "days": [0, 1, 2, 3, 4],
                        "periods": [[60, 240], [360, 600]]},
    }
    cases = [
        (datetime(2026, 9, 17, 0, 59, tzinfo=timezone.utc), True),    # 周四 00:59 空闲
        (datetime(2026, 9, 17, 1, 0, tzinfo=timezone.utc), False),    # 高峰开始
        (datetime(2026, 9, 17, 3, 59, tzinfo=timezone.utc), False),
        (datetime(2026, 9, 17, 4, 0, tzinfo=timezone.utc), True),     # 高峰结束（左闭右开）
        (datetime(2026, 9, 17, 5, 59, tzinfo=timezone.utc), True),    # 午间空隙算空闲
        (datetime(2026, 9, 17, 6, 0, tzinfo=timezone.utc), False),
        (datetime(2026, 9, 17, 9, 59, tzinfo=timezone.utc), False),
        (datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc), True),
        (datetime(2026, 9, 19, 2, 0, tzinfo=timezone.utc), True),     # 周六
    ]
    for now, expect_off in cases:
        assert is_off_peak_now(entry, now) is expect_off, now

    # 无 off_peak 档 / 无窗口定义 → 一律按基准价（False）
    assert is_off_peak_now({"peak": {}}, datetime(2026, 9, 17, 7, 0, tzinfo=timezone.utc)) is False
    assert is_off_peak_now({"off_peak": {"input_per_mtok": 0.1}},
                           datetime(2026, 9, 17, 7, 0, tzinfo=timezone.utc)) is False
    assert is_off_peak_now(None) is False


def test_off_peak_window_timezone_offset():
    """窗口可带时区偏移（数据驱动）：偏移 -480 表示按 UTC-8 的 9 点判定。"""
    entry = {
        "off_peak": {"input_per_mtok": 0.15},
        "peak_window": {"tz_offset_minutes": -480, "days": [0, 1, 2, 3, 4],
                        "periods": [[540, 600]]},   # 当地 09:00-10:00
    }
    # UTC 17:00 → 当地 09:00（周四）→ 高峰
    assert is_off_peak_now(entry, datetime(2026, 9, 17, 17, 0, tzinfo=timezone.utc)) is False
    # UTC 16:59 → 当地 08:59 → 空闲
    assert is_off_peak_now(entry, datetime(2026, 9, 17, 16, 59, tzinfo=timezone.utc)) is True


# ------------------------------------------------------------ 插件：指纹 / 查询 / 同步


def test_fingerprint_matches_custom_gateway_model(tmp_path):
    """自定义中转按模型名辨认（用户例：command code 的 deepseek）。"""
    p = _plugin()
    p.configure(str(tmp_path))
    looked = p.lookup("commandcode-go/deepseek/deepseek-v4.1-flash")
    assert looked is not None
    assert looked["model_key"] == "deepseek-flash"
    assert looked["input_per_mtok"] == 0.3
    assert looked["off_peak"]["input_per_mtok"] == 0.15
    assert looked["peak_window"]["tz_offset_minutes"] == 0
    # pro 与别名
    assert p.lookup("deepseek/deepseek-v4.1-pro")["model_key"] == "deepseek-v4-pro"
    assert p.lookup("deepseek-v4-flash")["model_key"] == "deepseek-flash"
    assert p.lookup("Kimi-K3")["model_key"] == "kimi-k3"
    # 不认识 → None（交回主程序走 models.dev / config）
    assert p.lookup("gpt-4o") is None
    assert p.lookup("") is None


def test_plugin_lookup_snapshot_then_cache_overrides(tmp_path):
    p = _plugin()
    p.configure(str(tmp_path))
    snap = p.lookup("deepseek-flash")
    assert snap["source"] == "snapshot:deepseek"
    assert snap["snapshot"] is True

    # 写入缓存（模拟已同步）→ 取缓存值并标 official
    cache = {"sources": {"deepseek": {
        "fetched_at": 9_999_999_999.0,
        "url": "x",
        "models": {"deepseek-flash": {"peak": {"input_per_mtok": 0.31,
                                               "output_per_mtok": 1.21,
                                               "cache_hit_per_mtok": 0.0061}}},
    }}}
    (tmp_path / "pricing_cache.json").write_text(json.dumps(cache), encoding="utf-8")
    p.configure(str(tmp_path))
    hit = p.lookup("deepseek-flash")
    assert hit["source"] == "official:deepseek"
    assert hit["input_per_mtok"] == 0.31
    assert hit["snapshot"] is False


def test_plugin_sync_writes_cache_and_failure_keeps_old(tmp_path, monkeypatch):
    import httpx

    p = _plugin()
    p.configure(str(tmp_path))

    class _Resp:
        status_code = 200
        text = KIMI_MD

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    res = p.sync("kimi")
    assert res["ok"] is True and res["models"] == 2
    assert (tmp_path / "pricing_cache.json").exists()
    assert p.lookup("kimi-k3")["source"] == "official:kimi"

    # 解析失败 → 报错且保留旧缓存（不让坏页面把好数据冲掉）
    class _Bad:
        status_code = 200
        text = "<html>改版了</html>"

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Bad())
    res2 = p.sync("kimi")
    assert res2["ok"] is False and "解析失败" in res2["error"]
    assert p.lookup("kimi-k3")["source"] == "official:kimi"

    # 未知源 → 明确报错
    assert p.sync("nope")["ok"] is False


def test_plugin_status_stale_flag(tmp_path):
    p = _plugin()
    p.configure(str(tmp_path))
    st = {s["id"]: s for s in p.status()}
    assert set(st) >= {"deepseek", "kimi"}
    # 无缓存 → stale（前端提示「建议同步」），但有内置快照兜底
    assert st["deepseek"]["stale"] is True
    assert st["deepseek"]["snapshot_date"]
    assert p.lookup("deepseek-flash") is not None


def test_local_plugin_overrides_builtin(tmp_path):
    """~/.lite-work/plugins/ 同名包覆盖内置（社区更新机制，无需改主程序）。"""
    plugin_dir = tmp_path / "plugins" / "pricing-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text(
        "from litework.llm.pricing_provider import PricingProvider\n\n\n"
        "class MyPricing(PricingProvider):\n"
        "    name = 'pricing-plugin'\n"
        "    version = '9.9.9'\n"
        "    description = 'test override'\n\n"
        "    def lookup(self, model_id):\n"
        "        if 'deepseek' in (model_id or ''):\n"
        "            return {'input_per_mtok': 42.0, 'output_per_mtok': 43.0,\n"
        "                    'cache_hit_per_mtok': 44.0, 'source': 'plugin:test'}\n"
        "        return None\n\n"
        "    def status(self):\n"
        "        return []\n\n"
        "    def sync(self, source_id):\n"
        "        return {'ok': True}\n",
        encoding="utf-8")

    app = _app(tmp_path)
    provider = app.pricing_provider()
    assert provider is not None
    assert provider.version == "9.9.9"
    assert getattr(provider, "_is_local_override", False) is True
    # 覆盖生效：自定义价直接用在 deepseek 模型上
    pricing = app.resolve_pricing("deepseek", "deepseek-flash")
    assert pricing["input_per_mtok"] == 42.0
    assert pricing["source"] == "plugin:test"


# ------------------------------------------------------------ resolve_pricing 优先级


def test_resolve_pricing_priority_and_deepseek_guard(tmp_path):
    """① 用户覆盖 > ② 官方（插件）> ③ models.dev > ④ config；deepseek 不用 models.dev。"""
    app = _app(tmp_path)
    (tmp_path / "models.dev.json").write_text(json.dumps({
        "deepseek/deepseek-flash": {"cost": {"input": 0.15, "output": 0.6, "cache_read": 0.003}},
        "openai/gpt-5.6": {"cost": {"input": 4.0, "output": 20.0, "cache_read": 0.4}},
    }), encoding="utf-8")
    app.refresh_model_meta()

    # ② 官方（内置快照）优先于 models.dev 的空闲价
    ds = app.resolve_pricing("deepseek", "deepseek-flash")
    assert ds["input_per_mtok"] == 0.3            # 峰值，而非 models.dev 的 0.15
    assert ds["source"].startswith("snapshot:")
    assert ds["off_peak"]["input_per_mtok"] == 0.15
    assert ds["peak_window"]["periods"] == [[60, 240], [360, 600]]

    # 自定义中转的 deepseek 同样按官方价（不落 models.dev / config）
    cg = app.resolve_pricing("custom_x", "deepseek/deepseek-v4.1-flash")
    assert cg["input_per_mtok"] == 0.3
    assert "off_peak" in cg

    # ③ 非 deepseek（models.dev 有数据）→ models.dev
    oa = app.resolve_pricing("openai", "gpt-5.6")
    assert oa["input_per_mtok"] == 4.0
    assert oa["source"] == "models.dev"

    # ① 用户覆盖最高优先
    app.config["pricing_overrides"] = {
        "deepseek/deepseek-flash": {"input_per_mtok": 1.23, "output_per_mtok": 4.56,
                                    "cache_hit_per_mtok": 0.07},
        "unknown-model": {"input_per_mtok": 9.0, "output_per_mtok": 9.0,
                          "cache_hit_per_mtok": 0.9},
    }
    app._pricing_provider_loaded = True   # 覆盖与插件无关，避免重复装载
    ov = app.resolve_pricing("deepseek", "deepseek-flash")
    assert ov["input_per_mtok"] == 1.23 and ov["source"] == "override"
    assert "off_peak" not in ov           # 覆盖未给分时档 → 不分时
    # 裸模型名键对所有供应商生效（未知模型 → config 之上的逃生门）
    shadow = app.resolve_pricing("whatever", "unknown-model")
    assert shadow["input_per_mtok"] == 9.0
    app.config["pricing_overrides"] = {}

    # ④ 谁都不认识 → config 回退价（现状保持）
    unknown = app.resolve_pricing("whatever", "totally-unknown")
    assert unknown["source"] == "config"
    assert unknown["input_per_mtok"] == 0.3


# ------------------------------------------------------------ AgentLoop 选档


def test_agent_loop_selects_off_peak_at_billing_time(tmp_path, monkeypatch):
    import litework.core.agent_loop as agent_loop_mod
    from litework.core.agent_loop import AgentLoop, pricing_payload

    app = _app(tmp_path)
    pricing = app.resolve_pricing("deepseek", "deepseek-flash")

    loop = AgentLoop.__new__(AgentLoop)
    loop.pricing = pricing

    monkeypatch.setattr(agent_loop_mod, "is_off_peak_now", lambda *a, **k: False)
    peak_cost = loop._cost_of(1_000_000, 1_000_000, 1_000_000)
    assert abs(peak_cost - (0.3 + 0.006 + 1.2)) < 1e-9

    monkeypatch.setattr(agent_loop_mod, "is_off_peak_now", lambda *a, **k: True)
    off_cost = loop._cost_of(1_000_000, 1_000_000, 1_000_000)
    assert abs(off_cost - (0.15 + 0.003 + 0.6)) < 1e-9
    assert abs(peak_cost - off_cost * 2) < 1e-9       # 空闲 = 半价

    payload = pricing_payload(loop.pricing)
    assert payload["source"].startswith("snapshot:")
    assert payload["off_peak"]["input_per_mtok"] == 0.15
    assert payload["off_peak_active"] is True          # monkeypatch 后为 True
    assert payload["stale"] is False

    # 无 off_peak 档（如 models.dev 价）→ 恒定基准价 + payload 无分时字段
    loop.pricing = {"input_per_mtok": 4.0, "output_per_mtok": 20.0,
                    "cache_hit_per_mtok": 0.4, "source": "models.dev"}
    assert abs(loop._cost_of(1_000_000, 0, 0) - 4.0) < 1e-9
    plain = pricing_payload(loop.pricing)
    assert "off_peak" not in plain and "off_peak_active" not in plain


# ------------------------------------------------------------ 插件形态（ToolPlugin）


def test_pricing_plugin_is_tool_plugin():
    """定价插件是 ToolPlugin（进插件页 / kernel 装配），get_tools() 为空。"""
    from litework.tools.plugin import ToolPlugin
    from litework.tools.plugin_loader import load_collab_builtin, load_tool_builtin

    p = _plugin()
    assert isinstance(p, ToolPlugin)
    assert isinstance(p, PricingProvider)
    assert p.get_tools() == []

    # 统一内置发现：工具内置含定价；协作模式内置不含（否则按 mode_name 访问会出错）
    assert any(x.name == "pricing-plugin" for x in load_tool_builtin())
    assert all(x.name != "pricing-plugin" for x in load_collab_builtin())


def test_plugins_builtin_lists_pricing_plugin(tmp_path):
    """/api/plugins/builtin 数据源含 pricing-plugin（插件页可见，kind=tool）。"""
    app = _app(tmp_path)
    items = {p["name"]: p for p in app.plugins_builtin()}
    assert "pricing-plugin" in items
    entry = items["pricing-plugin"]
    assert entry["kind"] == "tool"
    assert entry["version"] == "1.0.0"
    assert entry["tools"] == []
    assert entry["builtin"] is True
