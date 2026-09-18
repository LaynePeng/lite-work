# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""模型元数据：models.dev 缓存/降级 + 上下文窗口解析优先级。"""
import json
import os
import time

from litework.llm.model_meta import CACHE_TTL_SECONDS, ModelMetaService
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
    # 未收录的 provider（自定义中转）→ 按模型名回退到**官方厂商**条目
    # （deepseek 段命中官方价；不取转售商 tinfoil 的 0.3）。这是为了网关用户
    # 「切换对话框模型」能反映到计费单价，而非所有模型都落回同一个静态价。
    assert svc.get_pricing("deepseek-v4-flash", provider_id="custom_x")["input_per_mtok"] == 0.15
    # 窗口是「模型级」属性：未收录的 provider 仍可用裸模型名兜底（避免误判为 128k）
    assert svc.get_context_window("deepseek-v4-flash", provider_id="custom_x") == 1_000_000
    # registry 侧同样按 provider 解析
    r = LLMRegistry(config_dir=str(tmp_path))
    assert r.get_context_window("deepseek", "deepseek-v4-flash") == 128_000
    assert r.get_model_pricing("deepseek", "deepseek-v4-flash")["input_per_mtok"] == 0.15
    assert r.get_model_pricing("custom_x", "deepseek-v4-flash")["input_per_mtok"] == 0.15
    assert r.get_context_window("custom_x", "deepseek-v4-flash") == 1_000_000


def test_vendored_model_id_does_not_shadow_official_price(tmp_path):
    """供应商用「厂商/模型」形式的 id 时，不得顶掉官方供应商的限定键。

    models.dev 里 tokengo 把 id 写成 "deepseek/deepseek-v4-flash"，与官方 deepseek
    供应商的限定键同名；写裸名兜底键时若不加约束会抢先占据该键（setdefault 先到
    先得，且 tokengo 在 api.json 中排序靠前），把官方价/窗口顶掉。
    """
    cache = tmp_path / "models.dev.json"
    cache.write_text(json.dumps({
        # 顺序在前：使用 vendored id 的转售商
        "tokengo": {"models": {"deepseek/deepseek-v4-flash": {
            "cost": {"input": 0.098, "output": 0.196, "cache_read": 0.028},
            "limit": {"context": 1_000_000}}}},
        "deepseek": {"models": {"deepseek-v4-flash": {
            "cost": {"input": 0.15, "output": 0.6, "cache_read": 0.003},
            "limit": {"context": 1_000_000}}}},
    }), encoding="utf-8")
    svc = ModelMetaService(str(cache))
    # 官方限定键未被 vendored 裸键顶掉
    assert svc.get_pricing("deepseek-v4-flash", provider_id="deepseek")["input_per_mtok"] == 0.15
    assert svc.get_pricing("deepseek-v4-flash", provider_id="deepseek")["cache_hit_per_mtok"] == 0.003
    # 用 vendored 全名配置的客户端也能拿到官方数据（同名键）
    assert svc.get_pricing("deepseek/deepseek-v4-flash")["input_per_mtok"] == 0.15


def test_gateway_provider_falls_back_to_official_vendor_by_model_name(tmp_path):
    """网关/自定义实例（custom_*）：provider 精确键缺失时按模型名匹配官方厂商价。

    回归：自定义中转聚合多家模型，models.dev 没有 custom_* 的 provider 条目，
    于是所有模型都落回全局静态价——表现为「切了对话框模型，计费仍按默认模型的价」。
    """
    cache = tmp_path / "models.dev.json"
    cache.write_text(json.dumps({
        "deepseek": {"models": {"deepseek-v4-flash": {
            "cost": {"input": 0.15, "output": 0.6, "cache_read": 0.003},
            "limit": {"context": 1_000_000}}}},
        "z-ai": {"models": {"glm-5.3": {
            "cost": {"input": 1.4, "output": 4.4, "cache_read": 0.26},
            "limit": {"context": 200_000}}}},
        # 转售商在官方段之前：只认官方厂商段，不得取到转售价
        "somegateway": {"models": {"z-ai/glm-5.3": {
            "cost": {"input": 99.0, "output": 99.0, "cache_read": 9.9}}}},
    }), encoding="utf-8")
    svc = ModelMetaService(str(cache))
    # custom_* 供应商：按模型名命中官方厂商
    assert svc.get_pricing("glm-5.3", provider_id="custom_123")["input_per_mtok"] == 1.4
    assert svc.get_pricing("deepseek-v4-flash", provider_id="custom_123")["input_per_mtok"] == 0.15
    assert svc.get_context_window("glm-5.3", provider_id="custom_123") == 200_000
    # 未知模型：仍返回 None（调用方回退配置价）
    assert svc.get_pricing("totally-unknown-model", provider_id="custom_123") is None


def test_model_meta_status_reports_cache(tmp_path):
    """设置页要能看出「有没有缓存 / 索引了多少模型 / 多久前同步」。"""
    empty = ModelMetaService(str(tmp_path / "missing.json"))
    assert empty.status() == {"cached": False, "models": 0, "age_seconds": None}

    cache = tmp_path / "models.dev.json"
    cache.write_text(json.dumps({
        "deepseek/deepseek-flash": {"limit": {"context": 1_000_000}},
        "openai/gpt-4o": {"limit": {"context": 128_000}},
    }), encoding="utf-8")
    st = ModelMetaService(str(cache)).status()
    assert st["cached"] is True
    assert st["models"] == 2
    assert isinstance(st["age_seconds"], float) and st["age_seconds"] >= 0


def test_deepseek_default_model_uses_current_name():
    """注册表默认模型名跟随官方现行名；历史别名仍可解析（旧配置不必立刻改）。"""
    from litework.llm.registry import PROVIDER_META

    assert PROVIDER_META["deepseek"]["default_model"] == "deepseek-flash"
    r = LLMRegistry()
    assert r.providers["deepseek"]["model"] == "deepseek-flash"
    assert r.get_context_window("deepseek") == 1_000_000
    assert r.get_context_window("deepseek", "deepseek-v4-flash") == 1_000_000


def test_models_dev_missing_cache_falls_back(tmp_path):
    # 无缓存文件、无网络 → get_context_window 返回 None，不抛异常
    svc = ModelMetaService(str(tmp_path / "none.json"))
    assert svc.get_context_window("anything") is None
    # registry 仍可用内置表
    r = LLMRegistry(config_dir=str(tmp_path))
    assert r.get_context_window("deepseek", "deepseek-v4-flash") == 1_000_000


def test_manual_refresh_forces_fetch_bypassing_ttl(tmp_path, monkeypatch):
    """手动同步必须绕过 7 天 TTL：缓存未过期也要真正联网拉取。

    旧实现 refresh() 命中 TTL 挡板直接返回旧缓存 → 用户点「同步」时间戳不变，
    表现为「怎么点都显示 N 天前」。force=True 强制走 _fetch_and_store。
    """
    cache = tmp_path / "models.dev.json"
    cache.write_text(json.dumps({"old/model": {"limit": {"context": 1}}}), encoding="utf-8")
    svc = ModelMetaService(str(cache))

    calls = {"n": 0}

    def spy(self):
        calls["n"] += 1
        return True

    monkeypatch.setattr(ModelMetaService, "_fetch_and_store", spy)
    # 未过期 + 未强制 → 纯本地读盘，不联网
    assert svc.refresh() is True
    assert calls["n"] == 0
    # force=True → 绕过 TTL，真正拉取
    assert svc.refresh(force=True) is True
    assert calls["n"] == 1


def _set_cache_age(path, days: float) -> None:
    """把缓存文件 mtime 调旧，模拟「超过 TTL 未同步」。"""
    ts = time.time() - days * 86400
    os.utime(str(path), (ts, ts))


def test_expired_cache_still_resolves_window_and_price(tmp_path):
    """缓存过期 ≠ 数据消失：过期缓存照用，窗口不许掉回 128K 兜底。

    回归（用户报障）：models.dev 缓存满 7 天（CACHE_TTL_SECONDS）的那一刻，
    过期缓存被整个丢弃 → 自定义中转的模型查不到窗口 → 静默回退供应商默认
    128K。用户侧表现是长会话 token 预算从 ~1M 突降到 115200（0.9×128K），
    随即 `Exceeded token budget`；设置页则显示「0 个模型」。
    """
    cache = tmp_path / "models.dev.json"
    cache.write_text(json.dumps({
        "z-ai": {"models": {"glm-5.3": {
            "cost": {"input": 1.4, "output": 4.4, "cache_read": 0.26},
            "limit": {"context": 1_048_576}}}},
    }), encoding="utf-8")
    _set_cache_age(cache, days=8)          # 超过 7 天 TTL

    svc = ModelMetaService(str(cache))
    # 窗口与价格（含「按模型名回退官方厂商」的网关路径）都继续可用
    assert svc.get_context_window("glm-5.3", provider_id="custom_9") == 1_048_576
    assert svc.get_pricing("glm-5.3", provider_id="custom_9")["input_per_mtok"] == 1.4
    # 状态仍报「有缓存」，年龄交给前端提示「建议同步」
    st = svc.status()
    assert st["cached"] is True
    assert st["models"] > 0            # 索引条数（含 provider 限定键 + 裸名键）
    assert st["age_seconds"] > CACHE_TTL_SECONDS

    r = LLMRegistry(config_dir=str(tmp_path))
    assert r.get_context_window("custom_9", "glm-5.3") == 1_048_576


def test_expired_cache_survives_failed_sync(tmp_path):
    """手动同步失败（离线/接口异常）不清空旧数据，过期缓存继续兜底。

    conftest 的 autouse fixture 把 `_fetch_and_store` 短路成「失败」，正好等价于
    断网时点「同步」：refresh 返回 False（前端提示同步失败），但窗口/价格解析
    照旧——不能因为一次同步失败就把 1M 窗口打成 128K。
    """
    cache = tmp_path / "models.dev.json"
    cache.write_text(json.dumps({
        "deepseek/deepseek-v4.1-flash": {"limit": {"context": 1_048_576}},
    }), encoding="utf-8")
    _set_cache_age(cache, days=30)

    svc = ModelMetaService(str(cache))
    assert svc.refresh(force=True) is False     # 同步确实失败
    assert svc.get_context_window("deepseek-v4.1-flash") == 1_048_576
    assert svc.status()["cached"] is True


def test_expired_cache_without_force_still_fetches(tmp_path, monkeypatch):
    """缓存过期时 refresh() 不得走「纯读盘」捷径，必须真正联网刷新。"""
    cache = tmp_path / "models.dev.json"
    cache.write_text(json.dumps({"old/model": {"limit": {"context": 1}}}), encoding="utf-8")
    _set_cache_age(cache, days=8)
    svc = ModelMetaService(str(cache))

    calls = []

    def spy(self):
        calls.append(1)
        self._index = {"fresh/model": {"limit": {"context": 42}}}
        return True

    monkeypatch.setattr(ModelMetaService, "_fetch_and_store", spy)
    assert svc.refresh() is True
    assert calls == [1]


def test_refresh_models_dev_forwards_force(tmp_path):
    """registry 层透传 force（AgentApp 手动同步 → registry → service）。"""
    r = LLMRegistry(config_dir=str(tmp_path))
    seen = {}

    class _Fake:
        def refresh(self, force=False):
            seen["force"] = force
            return True

    r.meta_service = _Fake()  # type: ignore[assignment]
    assert r.refresh_models_dev(force=True) is True
    assert seen["force"] is True
    assert r.refresh_models_dev() is True
    assert seen["force"] is False
