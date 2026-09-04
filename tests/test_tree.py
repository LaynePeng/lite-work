"""目录树 + Git 状态服务测试：结构化树 / A/M/D/U 状态字母 / 目录改动标记 / 路径越界。"""
from __future__ import annotations

import os
import subprocess

import httpx
import pytest

from litework.server.tree import git_snapshot, list_tree


@pytest.fixture(autouse=True)
def no_git_cache(monkeypatch):
    """关闭 git 状态 TTL 缓存，保证每个断言都拿到最新状态。"""
    monkeypatch.setattr("litework.server.tree.GIT_CACHE_TTL", -1)


def _git(repo, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@test.dev")
    (repo / "a.txt").write_text("a", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


# ---------------------------------------------------------------- 基础结构

def test_non_git_workspace(tmp_path):
    (tmp_path / "hello.txt").write_text("hi", encoding="utf-8")
    data = list_tree(str(tmp_path), "")
    assert data["branch"] is None
    assert data["has_repo"] is False
    names = {e["name"]: e for e in data["entries"]}
    assert names["hello.txt"]["type"] == "file"
    assert names["hello.txt"]["status"] is None


def test_dirs_first_then_files_sorted(tmp_path):
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "src").mkdir()
    data = list_tree(str(tmp_path), "")
    kinds = [e["type"] for e in data["entries"]]
    assert kinds == ["dir", "file", "file"]
    names = [e["name"] for e in data["entries"]]
    assert names == ["src", "a.txt", "b.txt"]


def test_gitignore_respected(tmp_path):
    (tmp_path / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("x", encoding="utf-8")
    (tmp_path / "keep.ts").write_text("k", encoding="utf-8")
    data = list_tree(str(tmp_path), "")
    names = [e["name"] for e in data["entries"]]
    assert "node_modules" not in names
    assert "keep.ts" in names


# ---------------------------------------------------------------- git 状态

def test_git_statuses_m_a_u(git_repo):
    # 修改 a.txt → M
    (git_repo / "a.txt").write_text("changed", encoding="utf-8")
    branch, smap = git_snapshot(str(git_repo))
    assert branch
    assert smap.get("a.txt") == "M"

    # 未跟踪新文件 → U
    (git_repo / "new.txt").write_text("n", encoding="utf-8")
    _, smap = git_snapshot(str(git_repo))
    assert smap.get("new.txt") == "U"

    # git add 后 → A
    _git(git_repo, "add", "new.txt")
    _, smap = git_snapshot(str(git_repo))
    assert smap.get("new.txt") == "A"


def test_git_deleted_file_shown_in_tree(git_repo):
    (git_repo / "del.txt").write_text("d", encoding="utf-8")
    _git(git_repo, "add", "del.txt")
    _git(git_repo, "commit", "-q", "-m", "add del")
    os.remove(git_repo / "del.txt")

    data = list_tree(str(git_repo), "")
    names = {e["name"]: e for e in data["entries"]}
    assert names["del.txt"]["type"] == "file"
    assert names["del.txt"]["status"] == "D"


def test_dir_has_changes_flag(git_repo):
    (git_repo / "sub").mkdir()
    (git_repo / "sub" / "inner.py").write_text("i", encoding="utf-8")
    (git_repo / "sub" / "inner.py").write_text("i2", encoding="utf-8")

    data = list_tree(str(git_repo), "")
    sub = next(e for e in data["entries"] if e["name"] == "sub")
    assert sub["type"] == "dir"
    assert sub["has_changes"] is True

    # 干净的目录没有标记
    (git_repo / "clean").mkdir()
    data = list_tree(str(git_repo), "")
    clean = next(e for e in data["entries"] if e["name"] == "clean")
    assert clean["has_changes"] is False


def test_git_rename_status(git_repo):
    (git_repo / "r.txt").write_text("r", encoding="utf-8")
    _git(git_repo, "add", "r.txt")
    _git(git_repo, "commit", "-q", "-m", "add r")
    _git(git_repo, "mv", "r.txt", "r2.txt")

    _, smap = git_snapshot(str(git_repo))
    assert smap.get("r2.txt") == "R"
    assert "r.txt" not in smap


def test_subdir_listing_and_status(git_repo):
    (git_repo / "pkg").mkdir()
    (git_repo / "pkg" / "mod.py").write_text("m", encoding="utf-8")
    _git(git_repo, "add", "pkg")
    _git(git_repo, "commit", "-q", "-m", "add pkg")
    (git_repo / "pkg" / "mod.py").write_text("m2", encoding="utf-8")

    data = list_tree(str(git_repo), "pkg")
    mod = next(e for e in data["entries"] if e["name"] == "mod.py")
    assert mod["status"] == "M"


# ---------------------------------------------------------------- 安全

def test_path_traversal_rejected(git_repo):
    with pytest.raises(ValueError):
        list_tree(str(git_repo), "../..")
    with pytest.raises(ValueError):
        list_tree(str(git_repo), "..")


def test_missing_dir_rejected(git_repo):
    with pytest.raises(ValueError):
        list_tree(str(git_repo), "no_such_dir")


# ---------------------------------------------------------------- HTTP 端点

async def test_tree_json_endpoint(tmp_path):
    from litework.app import AgentApp
    from litework.server.app import create_app

    app = AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lite-work"))
    (tmp_path / "hello.txt").write_text("hi", encoding="utf-8")
    (tmp_path / "src").mkdir()

    transport = httpx.ASGITransport(app=create_app(app, token=None))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/api/workspace/tree-json")
        assert r.status_code == 200
        body = r.json()
        assert body["git"]["has_repo"] is False
        names = {e["name"]: e for e in body["entries"]}
        assert names["hello.txt"]["type"] == "file"
        assert names["src"]["type"] == "dir"

        # 子目录懒加载
        r = await c.get("/api/workspace/tree-json", params={"path": "src"})
        assert r.status_code == 200
        assert r.json()["path"] == "src"

        # 路径越界 → 400
        r = await c.get("/api/workspace/tree-json", params={"path": "../.."})
        assert r.status_code == 400