"""定价：kimi 走 models.dev + 定价源走代理。

两个真实缺陷：

1. **裸名键被转售商抢占**：models.dev 里同一个模型有 30+ 家转售，价格差异极大
   甚至为 0（sensenova 的 kimi-k3 标 0，moonshotai 官方是 3/15）。`_flatten`
   原用 `setdefault`，裸名键「谁先遍历到用谁」——依赖上游 JSON 的排列顺序，
   结果 kimi 成本被算成 0。修法：裸名键按「官方厂商 > 有有效定价 > 其余」选优。
2. **定价源没走代理**：models.dev 有 scope=meta 代理，而定价插件抓各家官网
   用裸 `httpx.get`（无 proxy/trust_env），需要代理的网络里只有它失败。
"""
from __future__ import annotations

import json
import pathlib

import pytest

from litework.llm.model_meta import _flatten, _entry_pricing


# models.dev 的形状：provider → models → model_id → entry
SAMPLE = {
    # 转售商先出现且标 0 价（真实数据里 sensenova 就是这样）
    "sensenova": {"models": {"kimi-k3": {"cost": {
        "input": 0, "output": 0, "cache_read": 0}}}},
    "greenpt": {"models": {"kimi-k3": {"cost": {
        "input": 3.762, "output": 18.81, "cache_read": 0.9405}}}},
    # 官方厂商
    "moonshotai": {"models": {"kimi-k3": {"cost": {
        "input": 3, "output": 15, "cache_read": 0.3}}}},
    "deepseek": {"models": {"deepseek-flash": {"cost": {
        "input": 0.3, "output": 1.2, "cache_read": 0.006}}}},
    # 无官方条目的模型：应取首个有有效定价者（而不是 0 价那个）
    "cheapish": {"models": {"mystery-x": {"cost": {
        "input": 0, "output": 0, "cache_read": 0}}}},
    "seller": {"models": {"mystery-x": {"cost": {
        "input": 1.5, "output": 6, "cache_read": 0.1}}}},
}


class TestBareKeyPrefersOfficial:
    """裸名键必须优先取官方厂商的价，而不是"谁先遍历到用谁"。"""

    def test_official_vendor_wins_over_reseller_zero(self):
        flat = _flatten(SAMPLE)
        entry = flat["kimi-k3"]
        assert entry["cost"]["input"] == 3, "应取 moonshotai 官方价 3，而非 sensenova 的 0"
        assert entry["cost"]["output"] == 15

    def test_provider_qualified_key_still_exact(self):
        """限定键不受选优影响：sensenova/kimi-k3 仍指向它自己的价。"""
        flat = _flatten(SAMPLE)
        assert flat["sensenova/kimi-k3"]["cost"]["input"] == 0
        assert flat["moonshotai/kimi-k3"]["cost"]["input"] == 3

    def test_no_official_falls_back_to_first_priced(self):
        """没有官方条目时，取"有有效定价"的供应商而不是 0 价那个。"""
        flat = _flatten(SAMPLE)
        entry = flat["mystery-x"]
        assert entry["cost"]["input"] == 1.5, "应跳过 0 价，取 seller 的 1.5"

    def test_official_with_zero_still_wins_rank(self):
        """官方标 0（免费模型）时仍以官方为准——不因价格为 0 被判无效。"""
        data = {
            "reseller": {"models": {"free-model": {"cost": {
                "input": 9, "output": 9, "cache_read": 0}}}},
            "moonshotai": {"models": {"free-model": {"cost": {
                "input": 0, "output": 0, "cache_read": 0}}}},
        }
        flat = _flatten(data)
        assert flat["free-model"]["cost"]["input"] == 0, "官方免费应保留 0，不被转售商覆盖"


class TestEntryPricing:
    def test_needs_input_and_output(self):
        assert _entry_pricing({"cost": {"input": 1, "output": 2}}) is not None
        assert _entry_pricing({"cost": {"input": 1}}) is None
        assert _entry_pricing({"cost": None}) is None
        assert _entry_pricing({}) is None

    def test_maps_cache_read(self):
        p = _entry_pricing({"cost": {"input": 1, "output": 2, "cache_read": 0.1}})
        assert p == {"input_per_mtok": 1.0, "output_per_mtok": 2.0,
                     "cache_hit_per_mtok": 0.1}


class TestPricingProxy:
    """定价源抓取必须走 scope=meta 的代理（与 models.dev 同口径）。"""

    def test_configure_accepts_proxy(self, tmp_path):
        from litework.llm.pricing_provider import PricingProvider
        import inspect

        sig = inspect.signature(PricingProvider.configure)
        assert "proxy_url" in sig.parameters
        assert "ssl_verify" in sig.parameters

    def test_plugin_stores_proxy_and_verify(self, tmp_path, monkeypatch):
        """插件 configure 必须记住代理与 TLS 开关，sync 才能用上。"""
        import importlib.util
        import sys

        from litework.llm.pricing_provider import PricingProvider

        plugins_dir = (pathlib.Path(__file__).resolve().parent.parent
                       / "litework/builtin_plugins/pricing-plugin")
        # 插件内部有相对导入需求：临时把它所在目录挂进 sys.path
        monkeypatch.syspath_prepend(str(plugins_dir))
        spec = importlib.util.spec_from_file_location(
            "pricing_plugin_under_test", plugins_dir / "plugin.py")
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules["pricing_plugin_under_test"] = mod
        spec.loader.exec_module(mod)

        cls = next(
            v for v in vars(mod).values()
            if isinstance(v, type) and issubclass(v, PricingProvider)
            and v is not PricingProvider
        )
        inst = cls()
        inst.configure(str(tmp_path), proxy_url="http://127.0.0.1:7890",
                       ssl_verify=False)
        assert inst.proxy_url == "http://127.0.0.1:7890"
        assert inst.ssl_verify is False

        # 再次 configure 不传代理 → 清空（避免沿用上一次）
        inst.configure(str(tmp_path))
        assert inst.proxy_url == ""


class TestKimiUsesModelsDev:
    """kimi 不再需要独立官方源（models.dev 的 moonshotai 条目价格准确）。"""

    def test_kimi_source_removed(self):
        import pathlib

        data = json.loads((pathlib.Path(__file__).resolve().parent.parent
                           / "litework/builtin_plugins/pricing-plugin/pricing.json"
                           ).read_text(encoding="utf-8"))
        assert "kimi" not in data["sources"], (
            "kimi 源应已移除——models.dev 的 moonshotai 官方条目提供同样准确的价"
        )
        assert "deepseek" in data["sources"], "DeepSeek 因分时计费仍需专源"

    def test_deepseek_source_keeps_peak_offpeak(self):
        import pathlib

        data = json.loads((pathlib.Path(__file__).resolve().parent.parent
                           / "litework/builtin_plugins/pricing-plugin/pricing.json"
                           ).read_text(encoding="utf-8"))
        ds = data["sources"]["deepseek"]
        assert ds.get("peak_window"), "DeepSeek 分时计费需要 peak_window"
        for model in ds["models"].values():
            assert "off_peak" in model, "DeepSeek 每个模型都应有空闲档"
