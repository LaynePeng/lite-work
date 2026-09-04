"""成本估算定价测试：per-model 定价（models.dev）+ 缓存命中/未命中分段计价。"""
import json

from litework.core.agent_loop import AgentLoop
from litework.core.kernel import Kernel
from litework.llm.model_meta import ModelMetaService


def _loop(pricing):
    return AgentLoop(Kernel("cost-test"), adapter=None, registry=None, pricing=pricing)


def _meta_service(tmp_path, models):
    cache = tmp_path / "models.dev.json"
    cache.write_text(json.dumps(models), encoding="utf-8")
    return ModelMetaService(str(cache))


def test_cost_charges_cache_hit_at_discount():
    loop = _loop({"input_per_mtok": 1.6, "output_per_mtok": 4.8, "cache_hit_per_mtok": 0.16})
    # 900K 命中 + 100K 未命中 + 50K 输出
    stats = {"cache_hit_tokens": 900_000, "cache_miss_tokens": 100_000, "output_tokens": 50_000}
    cost = loop._estimate_cost(stats)
    expected = 0.1 * 1.6 + 0.9 * 0.16 + 0.05 * 4.8  # 0.16 + 0.144 + 0.24 = 0.544
    assert abs(cost - expected) < 1e-9
    # 对照：旧算法（全按 input 全价）会算 1.5*1.6+0.24 = 2.64，高估近 5 倍
    assert cost < 2.64


def test_cost_defaults_cache_price_to_tenth_of_input():
    # 未配置 cache_hit_per_mtok：默认 input 的 10%
    loop = _loop({"input_per_mtok": 2.0, "output_per_mtok": 6.0})
    stats = {"cache_hit_tokens": 1_000_000, "cache_miss_tokens": 0, "output_tokens": 0}
    assert abs(loop._estimate_cost(stats) - 0.2) < 1e-9


def test_cost_falls_back_to_input_tokens_without_usage():
    # 估算兜底（无 usage）：hit/miss 均为 0，按 input_tokens 全价
    loop = _loop({"input_per_mtok": 1.0, "output_per_mtok": 2.0, "cache_hit_per_mtok": 0.1})
    stats = {"input_tokens": 500_000, "cache_hit_tokens": 0, "cache_miss_tokens": 0, "output_tokens": 100_000}
    assert abs(loop._estimate_cost(stats) - (0.5 * 1.0 + 0.1 * 2.0)) < 1e-9


def test_zero_pricing_is_zero():
    # 显式零价（pricing=None 会启用内置默认价，故此处显式传零）
    loop = _loop({"input_per_mtok": 0, "output_per_mtok": 0, "cache_hit_per_mtok": 0})
    stats = {"cache_hit_tokens": 100, "cache_miss_tokens": 100, "output_tokens": 100}
    assert loop._estimate_cost(stats) == 0.0


def test_models_dev_pricing_per_model(tmp_path):
    """定价来自 models.dev 的 per-model 真实数据（三级 ID 匹配）。"""
    svc = _meta_service(tmp_path, {
        "deepseek/deepseek-v4-flash": {"limit": {"context": 128000},
                                        "cost": {"input": 0.14, "output": 0.28, "cache_read": 0.028}},
        "openai/gpt-4o": {"limit": {"context": 128000},
                          "cost": {"input": 2.5, "output": 10, "cache_read": 1.25}},
        "weird/model-no-cost": {"limit": {"context": 32000}},
    })
    # 全名匹配
    assert svc.get_pricing("deepseek/deepseek-v4-flash") == {
        "input_per_mtok": 0.14, "output_per_mtok": 0.28, "cache_hit_per_mtok": 0.028}
    # 裸模型名 + provider 拼接匹配
    assert svc.get_pricing("gpt-4o", provider_id="openai")["input_per_mtok"] == 2.5
    # 无 cost 数据 / 未知模型 → None（调用方回退静态价）
    assert svc.get_pricing("weird/model-no-cost") is None
    assert svc.get_pricing("unknown-model") is None
    assert svc.get_pricing("") is None


def test_per_model_pricing_changes_cost(tmp_path):
    """同一份 usage，deepseek-flash 与 gpt-4o 的模型价算出不同成本。"""
    svc = _meta_service(tmp_path, {
        "deepseek/deepseek-v4-flash": {"cost": {"input": 0.14, "output": 0.28, "cache_read": 0.028}},
        "openai/gpt-4o": {"cost": {"input": 2.5, "output": 10, "cache_read": 1.25}},
    })
    stats = {"cache_hit_tokens": 800_000, "cache_miss_tokens": 200_000, "output_tokens": 50_000}
    costs = {}
    for model_id in ("deepseek/deepseek-v4-flash", "openai/gpt-4o"):
        loop = _loop(svc.get_pricing(model_id))
        costs[model_id] = loop._estimate_cost(stats)
    # flash: 0.2*0.14 + 0.8*0.028 + 0.05*0.28 = 0.0644
    assert abs(costs["deepseek/deepseek-v4-flash"] - 0.0644) < 1e-9
    # gpt-4o: 0.2*2.5 + 0.8*1.25 + 0.05*10 = 2.0
    assert abs(costs["openai/gpt-4o"] - 2.0) < 1e-9
    # 两个模型成本差 31 倍——静态全局价不可能同时对
