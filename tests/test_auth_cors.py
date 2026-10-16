# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""P0-2 安全测试：Bearer 鉴权与 CORS 白名单。

鉴权默认策略在 CLI 入口（自动生成 token），create_app 的 token=None 表示
库级显式关闭（测试/嵌入场景）——这里的测试覆盖两层：
- create_app(token=...)：无/错凭证 401，正确凭证放行（含 SSE 端点）
- CORS：白名单内 origin 放行，未知 origin 拒绝（预检 400、响应无 ACAO）
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from litework.app import AgentApp
from litework.cli import _load_or_create_token
from litework.server.app import create_app


def test_generated_token_persists_across_restarts(tmp_path):
    """未显式指定令牌时：首启生成并落盘，重启复用同一令牌。

    回归背景：此前每次启动都 `secrets.token_urlsafe()` 新生成随机令牌，
    手机/浏览器扫码配对只在当次进程内有效——重启后必然 401，表现为
    「第二次进去又要输 token」。
    """
    cfg = str(tmp_path / "cfg")
    first = _load_or_create_token(cfg)
    second = _load_or_create_token(cfg)
    assert first and first == second
    with open(os.path.join(cfg, "server-token"), encoding="utf-8") as f:
        assert f.read().strip() == first


def test_explicit_token_file_is_reused(tmp_path):
    """已有 server-token 时直接复用（不被新随机值覆盖）。"""
    cfg = str(tmp_path / "cfg")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, "server-token"), "w", encoding="utf-8") as f:
        f.write("fixed-token\n")
    assert _load_or_create_token(cfg) == "fixed-token"
    assert _load_or_create_token(cfg) == "fixed-token"


def test_token_generation_survives_unwritable_dir(tmp_path, monkeypatch):
    """落盘失败（目录不可写）不阻断启动：退化为进程内随机令牌。"""
    cfg = str(tmp_path / "ro")

    def boom(*_a, **_kw):
        raise OSError("read-only")

    monkeypatch.setattr(os, "makedirs", boom)
    token = _load_or_create_token(cfg)
    assert token


@pytest.fixture
def app_and_client(tmp_path):
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    fast = create_app(app, token="secret-token")
    with TestClient(fast) as client:
        yield app, client


def test_token_auth_rejects_missing_or_wrong_token(app_and_client):
    _, client = app_and_client
    r = client.get("/api/status")
    assert r.status_code == 401
    r = client.get("/api/status", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    r = client.get("/api/status", headers={"Authorization": "secret-token"})
    assert r.status_code == 401  # 必须带 Bearer 前缀


def test_token_auth_accepts_correct_token(app_and_client):
    app, client = app_and_client
    r = client.get("/api/status", headers={"Authorization": "Bearer secret-token"})
    assert r.status_code == 200
    body = r.json()
    assert body["token_auth"] is True
    assert body["version"]  # 正常业务负载


def test_token_auth_guards_sse_endpoint(app_and_client):
    _, client = app_and_client
    r = client.get("/api/tasks/nonexistent/events")
    assert r.status_code == 401  # 鉴权先于 404（任务不存在）


def test_token_none_means_explicit_disable(tmp_path):
    """token=None = 库级显式关闭（CLI 层负责默认开启，见 cli.py）。"""
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    fast = create_app(app, token=None)
    with TestClient(fast) as client:
        r = client.get("/api/status")
        assert r.status_code == 200
        assert r.json()["token_auth"] is False


# ---------------------------------------------------------------- CORS 白名单

DEV_ORIGIN = "http://localhost:5173"


def test_cors_preflight_allowed_for_dev_origin(tmp_path):
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    fast = create_app(app, token=None)
    with TestClient(fast) as client:
        r = client.options(
            "/api/status",
            headers={
                "Origin": DEV_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert r.status_code == 200
        assert r.headers.get("access-control-allow-origin") == DEV_ORIGIN


def test_cors_preflight_rejected_for_unknown_origin(tmp_path):
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    fast = create_app(app, token=None)
    with TestClient(fast) as client:
        r = client.options(
            "/api/status",
            headers={
                "Origin": "http://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        # 预检被 CORS 中间件直接拒绝，不会触达业务路由
        assert r.status_code == 400
        assert r.headers.get("access-control-allow-origin") is None


def test_cors_simple_request_from_unknown_origin_gets_no_acao(tmp_path):
    """非预检请求会执行，但未知 origin 拿不到 ACAO 头 → 浏览器读不到响应。"""
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    fast = create_app(app, token=None)
    with TestClient(fast) as client:
        r = client.get("/api/status", headers={"Origin": "http://evil.example"})
        assert r.status_code == 200  # 服务端执行
        assert r.headers.get("access-control-allow-origin") is None  # 浏览器侧不可读


def test_cors_extra_origins_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LITEWORK_CORS_ORIGINS", "https://my.lan.host:9000")
    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    fast = create_app(app, token=None)
    with TestClient(fast) as client:
        r = client.options(
            "/api/status",
            headers={
                "Origin": "https://my.lan.host:9000",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert r.status_code == 200
        assert r.headers.get("access-control-allow-origin") == "https://my.lan.host:9000"
