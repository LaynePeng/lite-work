# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

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
    body = r.json()
    # 默认初始化结构（structure 缺省 = True）：素材/产出物/AGENTS.md 就位
    assert body["scaffolded"] is True
    assert body["kind"] == "project"
    target = body["path"]
    assert os.path.isdir(os.path.join(target, "素材", "原始"))
    assert os.path.isdir(os.path.join(target, "产出物", "报告"))
    with open(os.path.join(target, "AGENTS.md"), encoding="utf-8") as f:
        assert "新项目" in f.read()
    # 新建即进入最近列表
    items = client.get("/api/projects/recent").json()["items"]
    assert any(i["name"] == "新项目" for i in items)


def test_create_project_code_entry_no_structure(client_and_app):
    """「新建代码」：不初始化结构、kind=code，保持仓库干净。"""
    client, app, ws = client_and_app
    r = client.post("/api/projects/create", json={
        "parent": ws, "name": "codeX", "git": True, "structure": False, "kind": "code",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["scaffolded"] is False
    assert body["kind"] == "code"
    assert body["git_initialized"] is True
    target = body["path"]
    assert not os.path.exists(os.path.join(target, "AGENTS.md"))
    assert not os.path.isdir(os.path.join(target, "产出物"))


def test_create_project_kind_default_heuristic(client_and_app):
    """kind 缺省 → 按目录内容启发式（结构初始化后 → project）。"""
    client, app, ws = client_and_app
    r = client.post("/api/projects/create", json={"parent": ws, "name": "pKind", "git": False})
    assert r.status_code == 200
    assert r.json()["kind"] == "project"


# ---------------------------------------------------------------- 类型标记（kind 锁定）

def test_set_project_kind_and_lock(client_and_app):
    client, app, ws = client_and_app
    # 文档项目（带 .git，历史上会被误判为代码）
    proj = os.path.join(ws, "docs")
    os.makedirs(os.path.join(proj, ".git"))
    with open(os.path.join(proj, "论文.docx"), "wb") as f:
        f.write(b"x")
    client.post("/api/projects/recent", json={"path": proj})
    # 启发式：文档占优 → project（与 git 解耦）
    items = client.get("/api/projects/recent").json()["items"]
    assert next(i for i in items if i["name"] == "docs")["kind"] == "project"

    # 手动标记为代码并锁定
    r = client.post("/api/projects/recent/kind", json={"path": proj, "kind": "code"})
    assert r.status_code == 200 and r.json()["kind"] == "code"
    # 再次打开（启发式路径）不覆盖锁定值
    client.post("/api/projects/recent", json={"path": proj})
    items = client.get("/api/projects/recent").json()["items"]
    rec = next(i for i in items if i["name"] == "docs")
    assert rec["kind"] == "code"
    assert rec.get("kind_locked") is True


def test_set_project_kind_validation(client_and_app):
    client, app, ws = client_and_app
    assert client.post("/api/projects/recent/kind",
                       json={"path": ws, "kind": "bad"}).status_code == 400
    assert client.post("/api/projects/recent/kind",
                       json={"path": os.path.join(ws, "nope"), "kind": "code"}).status_code == 400


def test_heuristic_reclassifies_git_document_project(client_and_app):
    """复旦mem 场景：git 文档项目重新打开后自愈为 project。"""
    client, app, ws = client_and_app
    proj = os.path.join(ws, "fudan-mem")
    os.makedirs(os.path.join(proj, ".git"))
    for i in range(3):
        with open(os.path.join(proj, f"章节{i}.docx"), "wb") as f:
            f.write(b"x")
    client.post("/api/projects/recent", json={"path": proj})
    items = client.get("/api/projects/recent").json()["items"]
    rec = next(i for i in items if i["name"] == "fudan-mem")
    assert rec["kind"] == "project" and rec["is_git"] is True


# ---------------------------------------------------------------- 置顶（pin）

def test_project_pin_toggle_and_order(client_and_app):
    client, app, ws = client_and_app
    pa = os.path.join(ws, "projA")
    pb = os.path.join(ws, "projB")
    os.makedirs(pa)
    os.makedirs(pb)

    # 打开 A、B（B 更近）
    client.post("/api/projects/recent", json={"path": pa})
    client.post("/api/projects/recent", json={"path": pb})

    # pin A：A 应置顶
    r = client.post("/api/projects/pin", json={"path": pa})
    assert r.status_code == 200 and r.json()["pinned"] is True
    names = [i["name"] for i in client.get("/api/projects/recent").json()["items"]]
    assert names == ["projA", "projB"]

    # 再打开 B（重复）：pinned 的 A 仍置顶
    client.post("/api/projects/recent", json={"path": pb})
    names = [i["name"] for i in client.get("/api/projects/recent").json()["items"]]
    assert names == ["projA", "projB"]
    # pinned 字段保持
    items = client.get("/api/projects/recent").json()["items"]
    assert items[0]["pinned"] is True and items[1]["pinned"] is False

    # unpin A：A 回到未 pin 组首位（刚操作过的靠前）
    r2 = client.post("/api/projects/pin", json={"path": pa})
    assert r2.json()["pinned"] is False
    names = [i["name"] for i in client.get("/api/projects/recent").json()["items"]]
    assert names == ["projA", "projB"]
    assert all(i["pinned"] is False for i in client.get("/api/projects/recent").json()["items"])


def test_remember_keeps_pinned_state(client_and_app):
    client, app, ws = client_and_app
    pa = os.path.join(ws, "keepPin")
    os.makedirs(pa)
    client.post("/api/projects/recent", json={"path": pa})
    client.post("/api/projects/pin", json={"path": pa})
    # 重复打开不丢 pin
    client.post("/api/projects/recent", json={"path": pa})
    items = client.get("/api/projects/recent").json()["items"]
    assert items[0]["pinned"] is True

    # pin 不存在的项目 → 404
    r = client.post("/api/projects/pin", json={"path": os.path.join(ws, "nope")})
    assert r.status_code == 404


# ---------------------------------------------------------------- 目录浏览：隐藏文件过滤

def test_fs_list_hidden_files_filtered_by_default(client_and_app):
    client, app, ws = client_and_app
    # 准备隐藏文件/目录与普通文件
    os.makedirs(os.path.join(ws, "普通目录"))
    os.makedirs(os.path.join(ws, ".git"))
    with open(os.path.join(ws, "普通文件.txt"), "w") as f:
        f.write("x")
    with open(os.path.join(ws, ".DS_Store"), "w") as f:
        f.write("x")

    # 默认：隐藏条目被过滤
    r = client.get("/api/fs/list", params={"path": ws})
    assert r.status_code == 200
    data = r.json()
    assert "普通目录" in data["dirs"]
    assert ".git" not in data["dirs"]
    assert "普通文件.txt" in data["files"]
    assert ".DS_Store" not in data["files"]

    # show_hidden=true：全部显示
    r2 = client.get("/api/fs/list", params={"path": ws, "show_hidden": "true"})
    data2 = r2.json()
    assert ".git" in data2["dirs"]
    assert ".DS_Store" in data2["files"]
