# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""批量删除接口测试：POST /api/files/delete-batch 与 POST /api/sessions/delete-batch。

契约：单项失败不中断整批，记入 failed；整批级校验（空数组 / 超上限）→ 400。
"""
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


def _write(path: str, data: bytes = b"x") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _create_sessions(client: TestClient, n: int) -> list:
    ids = []
    for i in range(n):
        r = client.post("/api/sessions", json={"name": f"批量测试-{i}"})
        assert r.status_code == 200
        ids.append(r.json()["session_id"])
    return ids


# ------------------------------------------------- POST /api/files/delete-batch

def test_batch_delete_multiple_files(client_and_app):
    client, _, ws = client_and_app
    paths = []
    for i in range(3):
        rel = f"产出物/批量_{i}.txt"
        _write(os.path.join(ws, rel), b"data")
        paths.append(rel)

    r = client.post("/api/files/delete-batch", json={"paths": paths})
    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": True, "deleted": 3, "failed": []}
    for rel in paths:
        assert not os.path.exists(os.path.join(ws, rel))


def test_batch_delete_mixed_existing_and_missing(client_and_app):
    """混合存在/不存在：failed 记录原样路径与原因，存在者仍被删除。"""
    client, _, ws = client_and_app
    _write(os.path.join(ws, "产出物", "存在.txt"), b"x")

    r = client.post("/api/files/delete-batch", json={
        "paths": ["产出物/存在.txt", "产出物/不存在.txt"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["deleted"] == 1
    assert len(body["failed"]) == 1
    assert body["failed"][0]["path"] == "产出物/不存在.txt"
    assert "不存在" in body["failed"][0]["error"]
    # 存在者被真删
    assert not os.path.exists(os.path.join(ws, "产出物", "存在.txt"))


def test_batch_delete_rejects_git_internal_and_escape_and_dir(client_and_app):
    """底线项：.git 内部 / .. 越界 / 目录 → 全部记 failed，磁盘文件原样保留。"""
    client, _, ws = client_and_app
    _write(os.path.join(ws, ".git", "config"), b"[core]\n")
    _write(os.path.join(ws, "src", "pkg", "a.txt"), b"x")
    os.makedirs(os.path.join(ws, "src", "pkg_dir"), exist_ok=True)
    _write(os.path.join(ws, "正常.txt"), b"x")

    r = client.post("/api/files/delete-batch", json={
        "paths": [
            ".git/config",
            "../../etc/passwd",
            "src/pkg_dir",
            "正常.txt",
        ],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["deleted"] == 1
    failed = {f["path"]: f["error"] for f in body["failed"]}
    assert set(failed) == {".git/config", "../../etc/passwd", "src/pkg_dir"}
    assert ".git" in failed[".git/config"]
    assert "越界" in failed["../../etc/passwd"]
    assert "目录" in failed["src/pkg_dir"]
    # 被拒项均未被删
    assert os.path.isfile(os.path.join(ws, ".git", "config"))
    assert os.path.isdir(os.path.join(ws, "src", "pkg_dir"))
    assert not os.path.exists(os.path.join(ws, "正常.txt"))


def test_batch_delete_empty_paths_400(client_and_app):
    client, _, _ = client_and_app
    r = client.post("/api/files/delete-batch", json={"paths": []})
    assert r.status_code == 400


def test_batch_delete_over_limit_400(client_and_app):
    client, _, ws = client_and_app
    r = client.post("/api/files/delete-batch", json={"paths": [f"f{i}.txt" for i in range(501)]})
    assert r.status_code == 400
    # 上限内（500）应被接受（文件不存在 → failed，而非整批 400）
    r2 = client.post("/api/files/delete-batch", json={"paths": [f"f{i}.txt" for i in range(500)]})
    assert r2.status_code == 200
    assert r2.json()["deleted"] == 0
    assert len(r2.json()["failed"]) == 500


# --------------------------------------------- POST /api/sessions/delete-batch

def test_batch_delete_sessions(client_and_app):
    """API 建多个会话后批量删：deleted 正确，store 里确实没了。"""
    client, app, _ = client_and_app
    ids = _create_sessions(client, 3)

    r = client.post("/api/sessions/delete-batch", json={"ids": ids})
    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": True, "deleted": 3, "failed": []}
    for sid in ids:
        assert app.session_store.load(sid) is None


def test_batch_delete_sessions_with_missing_id(client_and_app):
    client, app, _ = client_and_app
    ids = _create_sessions(client, 2)

    r = client.post("/api/sessions/delete-batch", json={"ids": [*ids, "session_nope"]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["deleted"] == 2
    assert body["failed"] == [{"id": "session_nope", "error": "会话不存在"}]
    for sid in ids:
        assert app.session_store.load(sid) is None


def test_batch_delete_sessions_empty_ids_400(client_and_app):
    client, _, _ = client_and_app
    r = client.post("/api/sessions/delete-batch", json={"ids": []})
    assert r.status_code == 400
