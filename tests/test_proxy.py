# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""出网代理（core/proxy.py）：URL 归一化普适性 + scope 分流 + env 注入 + 测试端点。"""
from __future__ import annotations

import pytest

from litework.core.proxy import (
    PROXY_SCOPES,
    SCOPE_LABELS,
    normalize_proxy_url,
    proxy_env,
    proxy_for,
)


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


# ---------------------------------------------------------------- URL 归一化

def test_normalize_standard_formats():
    """标准格式原样（scheme 归一小写）。"""
    assert normalize_proxy_url("http://user:pass@1.2.3.4:8080") == "http://user:pass@1.2.3.4:8080"
    assert normalize_proxy_url("HTTP://user:pass@1.2.3.4:8080") == "HTTP://user:pass@1.2.3.4:8080"
    assert normalize_proxy_url("https://user:pass@proxy.corp.com:8443") == "https://user:pass@proxy.corp.com:8443"
    assert normalize_proxy_url("socks5://u:p@127.0.0.1:1080") == "socks5://u:p@127.0.0.1:1080"


def test_normalize_credential_first_format():
    """凭据在前、协议在后的手写习惯（用户实际格式）：重排为标准顺序。"""
    assert normalize_proxy_url("user:pass@https://1.2.3.4:8443") == "https://user:pass@1.2.3.4:8443"
    assert normalize_proxy_url("user:pass@http://1.2.3.4:8080") == "http://user:pass@1.2.3.4:8080"


def test_normalize_bare_forms():
    """无协议写法：host:port 按 http；带凭据同。"""
    assert normalize_proxy_url("user:pass@1.2.3.4:8080") == "http://user:pass@1.2.3.4:8080"
    assert normalize_proxy_url("1.2.3.4:8080") == "http://1.2.3.4:8080"
    assert normalize_proxy_url("proxy.corp.com:8080") == "http://proxy.corp.com:8080"


def test_normalize_rejects_garbage():
    """垃圾输入 → None（不猜）。"""
    assert normalize_proxy_url("") is None
    assert normalize_proxy_url("   ") is None
    assert normalize_proxy_url("not a proxy") is None
    assert normalize_proxy_url("user:pass@") is None
    assert normalize_proxy_url("http://") is None


# ---------------------------------------------------------------- scope 分流

def _cfg(enabled=True, url="http://u:p@1.2.3.4:8080", scope=("llm",), bypass=()):
    return {"proxy": {"enabled": enabled, "url": url, "scope": list(scope), "bypass": list(bypass)}}


def test_proxy_for_scope_routing():
    """scope 命中才走代理；未启用 / scope 不在列表 / URL 无效 → None。"""
    assert proxy_for(_cfg(scope=("llm",)), "llm") == "http://u:p@1.2.3.4:8080"
    assert proxy_for(_cfg(scope=("llm",)), "webfetch") is None
    assert proxy_for(_cfg(enabled=False, scope=("llm",)), "llm") is None
    assert proxy_for(_cfg(scope=("llm",), url="垃圾"), "llm") is None
    # 空配置段
    assert proxy_for({}, "llm") is None


def test_proxy_for_socks5_without_deps(monkeypatch):
    """socks5 且未安装 socksio → 退回直连（None），不抛异常。"""
    import sys
    monkeypatch.setitem(sys.modules, "socksio", None)  # import 必失败
    r = proxy_for(_cfg(url="socks5://1.2.3.4:1080", scope=("llm",)), "llm")
    assert r is None


def test_scopes_and_labels_complete():
    """六个出网面齐备（llm/webfetch/meta/market/mcp/shell），标签全覆盖。"""
    assert PROXY_SCOPES == ("llm", "webfetch", "meta", "market", "mcp", "shell")
    assert set(SCOPE_LABELS.keys()) == set(PROXY_SCOPES)


# ---------------------------------------------------------------- 子进程 env

def test_proxy_env_injection():
    """命中 scope → 大小写双变体注入；bypass → NO_PROXY。"""
    env = proxy_env(_cfg(scope=("mcp",), bypass=("internal.corp", "localhost")), "mcp")
    assert env["HTTP_PROXY"] == "http://u:p@1.2.3.4:8080"
    assert env["https_proxy"] == "http://u:p@1.2.3.4:8080"
    assert env["NO_PROXY"] == "internal.corp,localhost"
    assert env["no_proxy"] == "internal.corp,localhost"
    # scope 不命中 → 空 dict
    assert proxy_env(_cfg(scope=("mcp",)), "shell") == {}


def test_proxy_env_user_format_normalized():
    """用户实际格式（user:pass@https://host）→ 注入前完成归一化。"""
    env = proxy_env(_cfg(url="user:pass@https://1.2.3.4:8443", scope=("shell",)), "shell")
    assert env["HTTP_PROXY"] == "https://user:pass@1.2.3.4:8443"


async def test_app_save_config_syncs_proxy(tmp_path):
    """端到端：save_config({"proxy": ...}) → llm_registry 代理即时热生效。"""
    from litework.app import AgentApp

    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    app.refresh_model_meta = lambda: False
    assert app.llm_registry.proxy_url is None

    app.save_config({"proxy": {
        "enabled": True,
        "url": "user:pass@https://1.2.3.4:8443",
        "scope": ["llm", "meta"],
        "bypass": [],
    }})
    # llm scope 命中：URL 已归一化（凭据在前 → 标准顺序）
    assert app.llm_registry.proxy_url == "https://user:pass@1.2.3.4:8443"
    # meta scope 命中：meta_service 同步
    assert app.llm_registry.meta_service.proxy_url == "https://user:pass@1.2.3.4:8443"

    # 适配器重建链路：build_adapter 构造的适配器带 proxy_url
    app.llm_registry.providers["deepseek"]["api_key"] = "sk-test"
    adapter = app.llm_registry.build_adapter("deepseek")
    assert adapter.proxy_url == "https://user:pass@1.2.3.4:8443"

    # 关闭代理 → set_proxy(None)，缓存适配器废弃
    app.save_config({"proxy": {"enabled": False, "url": "", "scope": [], "bypass": []}})
    assert app.llm_registry.proxy_url is None


async def test_proxy_test_endpoint_invalid_url(live_client):
    """POST /api/proxy/test：垃圾地址 → ok=false 人话回显（不抛异常）。"""
    c, _, _ = live_client
    r = await c.post("/api/proxy/test", json={"url": "垃圾地址"})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "格式" in r.json()["text"]
