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


def test_models_dev_missing_cache_falls_back(tmp_path):
    # 无缓存文件、无网络 → get_context_window 返回 None，不抛异常
    svc = ModelMetaService(str(tmp_path / "none.json"))
    assert svc.get_context_window("anything") is None
    # registry 仍可用内置表
    r = LLMRegistry(config_dir=str(tmp_path))
    assert r.get_context_window("deepseek", "deepseek-v4-flash") == 1_000_000
