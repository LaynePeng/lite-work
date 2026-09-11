# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""模型元数据：models.dev 缓存/降级 + 上下文窗口解析优先级。"""
import json
import os

from litework.llm.model_meta import ModelMetaService
from litework.llm.registry import LLMRegistry


def test_registry_static_context_window():
    r = LLMRegistry()
    assert r.get_context_window("deepseek", "deepseek-v4-flash") == 1_000_000
    assert r.get_context_window("deepseek", "deepseek-v4-pro") == 1_000_000
    assert r.get_context_window("kimi", "moonshot-v1-32k") == 32_768
    # 未收录模型 → 供应商默认
    assert r.get_context_window("anthropic", "claude-unknown-model") == 200_000
    # 未知供应商 → 兜底
    assert r.get_context_window("nope") == 128_000


def test_manual_override_wins():
    r = LLMRegistry({"active": "deepseek", "providers": {
        "deepseek": {"context_window": 65536},
    }})
    assert r.get_context_window("deepseek", "deepseek-v4-flash") == 65536


def test_provider_keeps_multiple_models_and_selected_model():
    r = LLMRegistry({"providers": {
        "custom": {"model": "gpt-5.6-sol", "models": ["gpt-5.5", "gpt-5.6-sol"]},
    }})
    assert r.providers["custom"]["model"] == "gpt-5.6-sol"
    assert r.providers["custom"]["models"] == ["gpt-5.5", "gpt-5.6-sol"]
    assert r.to_config()["providers"]["custom"]["models"] == ["gpt-5.5", "gpt-5.6-sol"]


def test_legacy_single_model_is_migrated_to_model_list():
    r = LLMRegistry({"providers": {
        "custom": {"model": "legacy-model"},
    }})
    assert r.providers["custom"]["models"] == ["legacy-model"]


def test_custom_provider_instances_keep_independent_connections():
    r = LLMRegistry({"active": "custom_yibu", "providers": {
        "custom_yibu": {
            "name": "Yibu API", "api_key": "key-one", "base_url": "https://yibuapi.com/v1",
            "model": "gpt-5.6-terra", "models": ["gpt-5.5", "gpt-5.6-terra"],
        },
        "custom_other": {
            "name": "Other API", "api_key": "key-two", "base_url": "https://example.com/v1",
            "model": "other-model", "models": ["other-model"],
        },
    }})
    assert r.build_adapter().base_url == "https://yibuapi.com/v1"
    assert r.providers["custom_other"]["api_key"] == "key-two"
    assert [p["name"] for p in r.provider_meta() if p["id"].startswith("custom_")] == ["Yibu API", "Other API"]


def test_models_dev_cache_used_and_fallback(tmp_path):
    # 有缓存 → 用缓存值
    cache = tmp_path / "models.dev.json"
    cache.write_text(json.dumps({
        "my-vendor": {"limit": {"context": 999999, "input": 999999}},
    }), encoding="utf-8")
    svc = ModelMetaService(str(cache))
    assert svc.get_context_window("my-vendor") == 999999
    # 缓存中没有该模型 → None（调用方回退内置表）
    assert svc.get_context_window("deepseek-v4-flash") is None

    # registry 挂载 meta_service 后：models.dev 优先于内置表
    r = LLMRegistry(config_dir=str(tmp_path))
    assert r.get_context_window("custom", "my-vendor") == 999999
    assert r.get_context_window("custom") == 128_000


def test_models_dev_index_keeps_provider_dimension(tmp_path):
    """同一模型被多家供应商收录：定价必须命中「配置供应商」，而非任意一家。

    models.dev 里 deepseek-v4-flash 有 30+ 家供应商在售，价格从 0 到 0.3 不等；
    早期实现只存裸 model_id 索引，后遍历到的供应商会覆盖前者，定价随机取到
    某个转售商（缓存命中价差 20 倍）。
    """
    cache = tmp_path / "models.dev.json"
    cache.write_text(json.dumps({
        "tinfoil": {"models": {"deepseek-v4-flash": {
            "cost": {"input": 0.3, "output": 0.7, "cache_read": 0.06},
            "limit": {"context": 1_000_000}}}},
        "deepseek": {"models": {"deepseek-v4-flash": {
            "cost": {"input": 0.15, "output": 0.6, "cache_read": 0.003},
            "limit": {"context": 128_000}}}},
    }), encoding="utf-8")
    svc = ModelMetaService(str(cache))
    # provider 限定匹配：各取各家的数据
    assert svc.get_pricing("deepseek-v4-flash", provider_id="deepseek")["cache_hit_per_mtok"] == 0.003
    assert svc.get_pricing("deepseek-v4-flash", provider_id="tinfoil")["input_per_mtok"] == 0.3
    assert svc.get_context_window("deepseek-v4-flash", provider_id="deepseek") == 128_000
    assert svc.get_context_window("deepseek-v4-flash", provider_id="tinfoil") == 1_000_000
    # 未收录的 provider（自定义中转）→ 定价不冒充他人报价，交回配置回退价
    assert svc.get_pricing("deepseek-v4-flash", provider_id="custom_x") is None
    # 窗口是「模型级」属性：未收录的 provider 仍可用裸模型名兜底（避免误判为 128k）
    assert svc.get_context_window("deepseek-v4-flash", provider_id="custom_x") == 1_000_000
    # registry 侧同样按 provider 解析
    r = LLMRegistry(config_dir=str(tmp_path))
    assert r.get_context_window("deepseek", "deepseek-v4-flash") == 128_000
    assert r.get_model_pricing("deepseek", "deepseek-v4-flash")["input_per_mtok"] == 0.15
    assert r.get_model_pricing("custom_x", "deepseek-v4-flash") is None
    assert r.get_context_window("custom_x", "deepseek-v4-flash") == 1_000_000


def test_models_dev_missing_cache_falls_back(tmp_path):
    # 无缓存文件、无网络 → get_context_window 返回 None，不抛异常
    svc = ModelMetaService(str(tmp_path / "none.json"))
    assert svc.get_context_window("anything") is None
    # registry 仍可用内置表
    r = LLMRegistry(config_dir=str(tmp_path))
    assert r.get_context_window("deepseek", "deepseek-v4-flash") == 1_000_000
