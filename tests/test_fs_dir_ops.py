# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""文件页签「目录操作」接口测试：新建文件/文件夹、目录改名、目录递归删除。

对应 UI：文件页签在**目录**上右键（此前只有单文件可右键）。
守卫与单文件操作同一套底线：只允许工作区内、`.git/` 内部禁止、越界 403。
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from litework.app import AgentApp
from litework.server.app import create_app


@pytest.fixture
def client_and_workspace(tmp_path):
    ws = str(tmp_path)
    app = AgentApp(workspace=ws, config_dir=str(tmp_path / ".lite-work"))
    fast = create_app(app, token=None)
    with TestClient(fast) as client:
        yield client, ws


# ---------------------------------------------------------------- POST /api/files/create

def test_create_file_in_directory(client_and_workspace):
    client, ws = client_and_workspace
    os.makedirs(os.path.join(ws, "src"))

    r = client.post("/api/files/create", json={"parent": "src", "name": "main.py", "kind": "file"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "path": "src/main.py", "name": "main.py", "kind": "file"}
    assert os.path.isfile(os.path.join(ws, "src", "main.py"))
    # 空文件（不写入任何内容）
    assert os.path.getsize(os.path.join(ws, "src", "main.py")) == 0


def test_create_directory_in_root(client_and_workspace):
    client, ws = client_and_workspace

    r = client.post("/api/files/create", json={"parent": "", "name": "文档", "kind": "dir"})
    assert r.status_code == 200, r.text
    assert r.json()["path"] == "文档"
    assert os.path.isdir(os.path.join(ws, "文档"))


def test_create_nested_directory_with_dot_name(client_and_workspace):
    """目录名允许 `v1.2` 这类带点的名称（无扩展名语义）。"""
    client, ws = client_and_workspace
    os.makedirs(os.path.join(ws, "release"))

    r = client.post("/api/files/create", json={"parent": "release", "name": "v1.2", "kind": "dir"})
    assert r.status_code == 200, r.text
    assert os.path.isdir(os.path.join(ws, "release", "v1.2"))


def test_create_rejects_bad_name_and_existing(client_and_workspace):
    client, ws = client_and_workspace
    os.makedirs(os.path.join(ws, "src"))
    open(os.path.join(ws, "src", "a.txt"), "w").close()

    # 空名 / 点名
    assert client.post("/api/files/create", json={"parent": "src", "name": "  "}).status_code == 400
    assert client.post("/api/files/create", json={"parent": "src", "name": ".."}).status_code == 400
    # 含路径分隔符或危险字符
    assert client.post("/api/files/create", json={"parent": "src", "name": "x/y.txt"}).status_code == 400
    assert client.post("/api/files/create", json={"parent": "src", "name": "x|y.txt"}).status_code == 400
    # 同名已存在 → 409（不覆盖）
    assert client.post("/api/files/create", json={"parent": "src", "name": "a.txt"}).status_code == 409
    # 目录同样不可重名
    os.makedirs(os.path.join(ws, "src", "pkg"))
    assert client.post("/api/files/create", json={"parent": "src", "name": "pkg", "kind": "dir"}).status_code == 409


def test_create_rejects_missing_parent_outside_and_git(client_and_workspace):
    client, ws = client_and_workspace
    os.makedirs(os.path.join(ws, ".git"))

    # 父目录不存在 → 404
    assert client.post("/api/files/create", json={"parent": "nope", "name": "a.txt"}).status_code == 404
    # 越界 → 403
    assert client.post("/api/files/create", json={"parent": "..", "name": "a.txt"}).status_code == 403
    # .git 内部 → 403
    assert client.post("/api/files/create", json={"parent": ".git", "name": "a.txt"}).status_code == 403
    # kind 非法 → 400
    assert client.post("/api/files/create", json={"parent": "", "name": "a.txt", "kind": "symlink"}).status_code == 400


# ---------------------------------------------------------------- DELETE /api/files（目录）

def test_delete_directory_requires_recursive_flag(client_and_workspace):
    client, ws = client_and_workspace
    os.makedirs(os.path.join(ws, "tmpdir"))
    open(os.path.join(ws, "tmpdir", "inner.txt"), "w").close()

    r = client.delete("/api/files", params={"path": "tmpdir"})
    assert r.status_code == 400
    assert "recursive" in r.json()["detail"]
    assert os.path.isdir(os.path.join(ws, "tmpdir"))  # 未被删除


def test_delete_directory_recursive_removes_contents(client_and_workspace):
    client, ws = client_and_workspace
    os.makedirs(os.path.join(ws, "pkg", "inner"))
    open(os.path.join(ws, "pkg", "inner", "a.txt"), "w").close()

    r = client.delete("/api/files", params={"path": "pkg", "recursive": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "path": "pkg", "kind": "dir"}
    assert not os.path.exists(os.path.join(ws, "pkg"))


def test_delete_file_still_reports_file_kind(client_and_workspace):
    """单文件删除行为不变（仅响应新增 kind 字段）。"""
    client, ws = client_and_workspace
    open(os.path.join(ws, "a.txt"), "w").close()

    r = client.delete("/api/files", params={"path": "a.txt"})
    assert r.status_code == 200
    assert r.json()["kind"] == "file"


def test_delete_rejects_workspace_root_and_git_internal(client_and_workspace):
    client, ws = client_and_workspace
    os.makedirs(os.path.join(ws, ".git"))

    # 指向工作区根（越界守卫）→ 403
    assert client.delete("/api/files", params={"path": ".", "recursive": True}).status_code == 403
    assert client.delete("/api/files", params={"path": "", "recursive": True}).status_code == 400
    # .git 内部 → 403（递归也不行）
    assert client.delete("/api/files", params={"path": ".git", "recursive": True}).status_code == 403


def test_batch_delete_still_refuses_directories(client_and_workspace):
    """批量删除维持"仅文件"语义：目录项记入 failed，不影响其余项。"""
    client, ws = client_and_workspace
    os.makedirs(os.path.join(ws, "dir1"))
    open(os.path.join(ws, "f1.txt"), "w").close()

    r = client.post("/api/files/delete-batch", json={"paths": ["dir1", "f1.txt"]})
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] == 1
    assert len(body["failed"]) == 1 and body["failed"][0]["path"] == "dir1"
    assert os.path.isdir(os.path.join(ws, "dir1"))
    assert not os.path.exists(os.path.join(ws, "f1.txt"))


# ---------------------------------------------------------------- POST /api/files/rename（目录）

def test_rename_directory_without_extension_constraint(client_and_workspace):
    client, ws = client_and_workspace
    os.makedirs(os.path.join(ws, "old"))

    r = client.post("/api/files/rename", json={"path": "old", "new_name": "v1.2"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "path": "v1.2", "name": "v1.2"}
    assert os.path.isdir(os.path.join(ws, "v1.2"))


def test_rename_directory_keeps_parent_and_rejects_conflict(client_and_workspace):
    client, ws = client_and_workspace
    os.makedirs(os.path.join(ws, "a", "old"))
    os.makedirs(os.path.join(ws, "a", "taken"))

    # 同名已存在 → 409
    assert client.post("/api/files/rename", json={"path": "a/old", "new_name": "taken"}).status_code == 409
    # 携带路径分隔符（试图挪目录）→ 400
    assert client.post("/api/files/rename", json={"path": "a/old", "new_name": "b/moved"}).status_code == 400
    # 正常改名仍留在原父目录
    r = client.post("/api/files/rename", json={"path": "a/old", "new_name": "renamed"})
    assert r.status_code == 200
    assert r.json()["path"] == "a/renamed"
    assert os.path.isdir(os.path.join(ws, "a", "renamed"))


def test_rename_file_extension_guard_unchanged(client_and_workspace):
    """回归：文件仍不允许改扩展名。"""
    client, ws = client_and_workspace
    open(os.path.join(ws, "a.js"), "w").close()

    r = client.post("/api/files/rename", json={"path": "a.js", "new_name": "a.py"})
    assert r.status_code == 400
    assert "扩展名" in r.json()["detail"]
    # 同名返回幂等
    r2 = client.post("/api/files/rename", json={"path": "a.js", "new_name": "a.js"})
    assert r2.status_code == 200 and r2.json()["path"] == "a.js"
