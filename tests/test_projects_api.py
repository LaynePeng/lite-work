"""项目概念 API 测试：最近项目列表 / 打开切换 / 移除 / 新建即记录。"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from litework.app import AgentApp
from litework.server.app import create_app


@pytest.fixture
def client_and_app(tmp_path):
    ws = str(tmp_path)
    app = AgentApp(workspace=ws, config_dir=str(tmp_path / ".lite-work"))
    fast = create_app(app, token=None)
    with TestClient(fast) as client:
        yield client, app, ws


def test_recent_projects_roundtrip(client_and_app):
    client, app, ws = client_and_app
    # 准备两个项目目录（codeB 带 .git → kind=code）
    proj_a = os.path.join(ws, "projA")
    code_b = os.path.join(ws, "codeB")
    os.makedirs(proj_a)
    os.makedirs(os.path.join(code_b, ".git"))

    # 打开 codeB（POST /api/projects/recent：切换 + 置顶记录）
    r = client.post("/api/projects/recent", json={"path": code_b})
    assert r.status_code == 200
    assert r.json()["kind"] == "code"

    # 普通切换 workspace 也记录
    r2 = client.post("/api/workspace", json={"path": proj_a})
    assert r2.status_code == 200

    # 列表：新→旧，codeB 带 is_git 标记
    r3 = client.get("/api/projects/recent")
    assert r3.status_code == 200
    items = r3.json()["items"]
    assert [i["name"] for i in items] == ["projA", "codeB"]
    assert items[0]["kind"] == "project"
    assert items[1]["kind"] == "code" and items[1]["is_git"] is True

    # 移除
    r4 = client.delete("/api/projects/recent", params={"path": code_b})
    assert r4.status_code == 200
    assert [i["name"] for i in client.get("/api/projects/recent").json()["items"]] == ["projA"]


def test_recent_projects_dedup_and_missing_dir(client_and_app):
    client, app, ws = client_and_app
    proj = os.path.join(ws, "p1")
    os.makedirs(proj)
    # 重复打开去重置顶
    client.post("/api/projects/recent", json={"path": proj})
    client.post("/api/projects/recent", json={"path": proj})
    items = client.get("/api/projects/recent").json()["items"]
    assert len([i for i in items if i["path"] == proj]) == 1

    # 不存在的目录报 400
    r = client.post("/api/projects/recent", json={"path": os.path.join(ws, "nope")})
    assert r.status_code == 400

    # 目录删除后列表自动剔除
    import shutil
    shutil.rmtree(proj)
    assert all(i["path"] != proj for i in client.get("/api/projects/recent").json()["items"])


def test_create_project_remembered(client_and_app):
    client, app, ws = client_and_app
    r = client.post("/api/projects/create", json={"parent": ws, "name": "新项目", "git": False})
    assert r.status_code == 200
    # 新建即进入最近列表
    items = client.get("/api/projects/recent").json()["items"]
    assert any(i["name"] == "新项目" for i in items)
