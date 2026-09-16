# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""隔离工作树（WorktreeManager）测试：创建/收割/合并/丢弃/降级/依赖 symlink。"""
from __future__ import annotations

import os
import subprocess

import pytest

from litework.tools.worktree_manager import WorktreeManager


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)


def _init_repo(path: str) -> None:
    """初始化一个含初始提交的 git 仓库。"""
    path = str(path)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")
    with open(os.path.join(path, "README.md"), "w", encoding="utf-8") as f:
        f.write("# demo\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")


# ---------------------------------------------------------------- 基础检测

def test_is_git_repo_and_dirty(tmp_path):
    assert WorktreeManager(str(tmp_path)).is_git_repo() is False
    _init_repo(tmp_path)
    mgr = WorktreeManager(str(tmp_path))
    assert mgr.is_git_repo() is True
    assert mgr.is_dirty() is False

    # 未提交改动 → dirty
    with open(tmp_path / "new.txt", "w", encoding="utf-8") as f:
        f.write("x")
    assert mgr.is_dirty() is True


def test_non_git_create_returns_not_ok(tmp_path):
    r = WorktreeManager(str(tmp_path)).create("wt1")
    assert r["ok"] is False
    assert "git" in r["reason"]


def test_create_rejects_unsafe_name(tmp_path):
    _init_repo(tmp_path)
    mgr = WorktreeManager(str(tmp_path))
    assert mgr.create("../evil")["ok"] is False
    assert mgr.create("a/b")["ok"] is False


# ---------------------------------------------------------------- 生命周期

def test_create_harvest_merge_discard(tmp_path):
    _init_repo(tmp_path)
    mgr = WorktreeManager(str(tmp_path))

    r = mgr.create("feature")
    assert r["ok"] is True
    wt = r["path"]
    assert os.path.isdir(wt)
    assert r["branch"] == "worktree-feature"

    # 在 worktree 中修改文件（模拟 Agent 干活）
    with open(os.path.join(wt, "README.md"), "w", encoding="utf-8") as f:
        f.write("# demo\n\nworktree change\n")
    with open(os.path.join(wt, "added.txt"), "w", encoding="utf-8") as f:
        f.write("new file\n")

    st = mgr.status("feature")
    assert st["exists"] is True
    paths = {f["path"] for f in st["files"]}
    assert "README.md" in paths and "added.txt" in paths
    assert st["adds"] > 0

    # 合并回主工作区
    m = mgr.merge("feature")
    assert m["ok"] is True
    assert os.path.isfile(tmp_path / "added.txt")
    assert "worktree change" in (tmp_path / "README.md").read_text(encoding="utf-8")

    # 丢弃：worktree 目录与分支都不应存在
    d = mgr.discard("feature")
    assert d["ok"] is True
    assert not os.path.isdir(wt)
    branches = _git(str(tmp_path), "branch", "--list", "worktree-feature").stdout
    assert "worktree-feature" not in branches


def test_discard_leaves_main_workspace_untouched(tmp_path):
    """核心承诺：丢弃后主工作区毫发无伤。"""
    _init_repo(tmp_path)
    mgr = WorktreeManager(str(tmp_path))
    r = mgr.create("exp")
    wt = r["path"]
    with open(os.path.join(wt, "README.md"), "w", encoding="utf-8") as f:
        f.write("destroyed\n")
    mgr.discard("exp")
    # 主工作区文件保持初始内容
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "# demo\n"
    assert mgr.is_dirty() is False


def test_merge_no_changes(tmp_path):
    _init_repo(tmp_path)
    mgr = WorktreeManager(str(tmp_path))
    mgr.create("empty")
    m = mgr.merge("empty")
    assert m["ok"] is True
    assert m.get("merged", 0) == 0


def test_create_reuses_existing_worktree(tmp_path):
    _init_repo(tmp_path)
    mgr = WorktreeManager(str(tmp_path))
    a = mgr.create("reuse")
    b = mgr.create("reuse")
    assert a["ok"] and b["ok"]
    assert a["path"] == b["path"]
    mgr.cleanup("reuse")


# ---------------------------------------------------------------- 依赖 symlink

def test_dependencies_symlinked_into_worktree(tmp_path):
    """node_modules / .venv 等被 gitignore 的依赖自动 symlink 进 worktree。"""
    _init_repo(tmp_path)
    # 造一个假的 node_modules（gitignore 掉，不进 git）
    with open(tmp_path / ".gitignore", "w", encoding="utf-8") as f:
        f.write("node_modules/\n.venv/\n.env\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.txt").write_text("x", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    _git(str(tmp_path), "add", ".gitignore")
    _git(str(tmp_path), "commit", "-q", "-m", "gitignore")

    mgr = WorktreeManager(str(tmp_path))
    wt = mgr.create("deps")["path"]
    assert os.path.islink(os.path.join(wt, "node_modules"))
    assert os.path.islink(os.path.join(wt, ".venv"))
    assert os.path.islink(os.path.join(wt, ".env"))
    # symlink 指向主工作区真实目录（共享同一份依赖）
    assert os.path.realpath(os.path.join(wt, "node_modules")) == os.path.realpath(
        str(tmp_path / "node_modules"))
    mgr.cleanup("deps")


def test_worktreeinclude_extra_entries(tmp_path):
    """项目根 .worktreeinclude 里列出的额外路径也会被 symlink 进去。"""
    _init_repo(tmp_path)
    (tmp_path / "mysecrets.json").write_text("{}", encoding="utf-8")
    with open(tmp_path / ".gitignore", "w", encoding="utf-8") as f:
        f.write("mysecrets.json\n")
    with open(tmp_path / ".worktreeinclude", "w", encoding="utf-8") as f:
        f.write("# 额外依赖\nmysecrets.json\n")
    _git(str(tmp_path), "add", ".gitignore", ".worktreeinclude")
    _git(str(tmp_path), "commit", "-q", "-m", "inc")

    mgr = WorktreeManager(str(tmp_path))
    wt = mgr.create("inc")["path"]
    assert os.path.islink(os.path.join(wt, "mysecrets.json"))
    mgr.cleanup("inc")


def test_dep_symlinks_excluded_from_harvest_and_merge(tmp_path):
    """回归：自动 symlink 的依赖（.venv 等）不得进入变更列表 / 合并补丁。

    .gitignore 的 `.venv/` 只匹配目录，不匹配同名 symlink——若不显式排除，
    会把「指向自身的 symlink」当作 Agent 改动合并回主工作区。
    """
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".venv/\nnode_modules/\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / "node_modules").mkdir()
    _git(str(tmp_path), "add", ".gitignore")
    _git(str(tmp_path), "commit", "-q", "-m", "gi")

    mgr = WorktreeManager(str(tmp_path))
    wt = mgr.create("deps")["path"]
    # Agent 只改一个真实文件
    with open(os.path.join(wt, "README.md"), "w", encoding="utf-8") as f:
        f.write("# demo\n\nchanged\n")

    files = [p for _, p in mgr.harvest("deps")["files"]]
    assert files == ["README.md"], f"依赖 symlink 混入变更列表: {files}"
    # patch 里不得出现 .venv
    assert ".venv" not in mgr.harvest("deps")["patch"]

    m = mgr.merge("deps")
    assert m["ok"] and m["merged"] == 1
    # 主工作区的 .venv 仍是真实目录（未被 symlink 覆盖）
    assert os.path.isdir(tmp_path / ".venv") and not os.path.islink(tmp_path / ".venv")
    mgr.cleanup("deps")


def test_slash_suffixed_dep_symlink_excluded(tmp_path):
    """无斜杠的 gitignore 模式（.env）下 symlink 也必须排除。"""
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    _git(str(tmp_path), "add", ".gitignore")
    _git(str(tmp_path), "commit", "-q", "-m", "gi")

    mgr = WorktreeManager(str(tmp_path))
    wt = mgr.create("env")["path"]
    with open(os.path.join(wt, "README.md"), "w", encoding="utf-8") as f:
        f.write("# demo\n\nx\n")
    files = [p for _, p in mgr.harvest("env")["files"]]
    assert files == ["README.md"], f".env symlink 混入: {files}"
    mgr.cleanup("env")


# ---------------------------------------------------------------- 枚举 / 暂存

def test_list_active(tmp_path):
    _init_repo(tmp_path)
    mgr = WorktreeManager(str(tmp_path))
    assert mgr.list_active() == []
    mgr.create("a")
    mgr.create("b")
    names = {w["name"] for w in mgr.list_active()}
    assert names == {"a", "b"}
    mgr.cleanup("a")
    mgr.cleanup("b")


def test_stash_and_pop(tmp_path):
    _init_repo(tmp_path)
    mgr = WorktreeManager(str(tmp_path))
    mgr.stash_workspace("lw-auto")
    # 干净工作区：stash 应返回 False
    assert mgr.stash_workspace("lw-auto-2") is False
    # 有改动时 stash 成功，pop 恢复
    (tmp_path / "wip.txt").write_text("wip", encoding="utf-8")
    assert mgr.stash_workspace("lw-auto-3") is True
    assert not (tmp_path / "wip.txt").exists()
    assert mgr.pop_stash() is True
    assert (tmp_path / "wip.txt").exists()


# ---------------------------------------------------------------- 遗留管理（app 层）

def _make_app(tmp_path):
    from litework.app import AgentApp
    return AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lc"))


def test_off_cleans_empty_worktree_keeps_dirty(tmp_path):
    """关闭隔离模式：空 worktree 自动清理；有改动的保留（不擅自丢数据）。"""
    _init_repo(tmp_path)
    app = _make_app(tmp_path)
    mgr = app.worktree_manager()

    # 会话 A：开了 worktree 但没改动 → 关闭时应被清理
    app.session_store.save("s-empty", [])
    app.set_session_worktree("s-empty", True)
    mgr.create("s-empty")
    assert os.path.isdir(tmp_path / ".lite-work/worktrees/s-empty")
    app.set_session_worktree("s-empty", False)
    assert not os.path.isdir(tmp_path / ".lite-work/worktrees/s-empty")

    # 会话 B：有改动 → 关闭后保留
    app.session_store.save("s-dirty", [])
    app.set_session_worktree("s-dirty", True)
    wt = mgr.create("s-dirty")["path"]
    (tmp_path / ".lite-work/worktrees/s-dirty/note.txt").write_text("x", encoding="utf-8")
    app.set_session_worktree("s-dirty", False)
    assert os.path.isdir(wt)
    assert app.session_worktree_enabled("s-dirty") is False


def test_worktree_clean_default_keeps_dirty(tmp_path):
    """遗留清理：默认只删无改动的；include_dirty=True 全删。"""
    _init_repo(tmp_path)
    app = _make_app(tmp_path)
    mgr = app.worktree_manager()

    mgr.create("empty1")
    mgr.create("empty2")
    wt = mgr.create("dirty")["path"]
    (tmp_path / ".lite-work/worktrees/dirty/f.txt").write_text("x", encoding="utf-8")

    r = app.worktree_clean()
    assert sorted(r["removed"]) == ["empty1", "empty2"]
    assert r["kept"] == ["dirty"]
    assert os.path.isdir(wt)

    r2 = app.worktree_clean(include_dirty=True)
    assert r2["removed"] == ["dirty"]
    assert not os.path.isdir(wt)


def test_worktree_clean_single_name(tmp_path):
    """按名字只清一个：其余保留；有改动的需 include_dirty 才删。"""
    _init_repo(tmp_path)
    app = _make_app(tmp_path)
    mgr = app.worktree_manager()

    mgr.create("a")
    mgr.create("b")
    wb = mgr.create("b")["path"]
    (tmp_path / ".lite-work/worktrees/b/f.txt").write_text("x", encoding="utf-8")

    # 清单个空壳 a：只删 a，b 保留
    r = app.worktree_clean(name="a")
    assert r["removed"] == ["a"]
    assert os.path.isdir(tmp_path / ".lite-work/worktrees/a") is False
    assert os.path.isdir(wb)

    # 指定 b 但有改动、未 include_dirty → 归入 kept，不删
    r2 = app.worktree_clean(name="b")
    assert r2["removed"] == [] and r2["kept"] == ["b"]
    assert os.path.isdir(wb)

    # 指定 b 且 include_dirty → 删除
    r3 = app.worktree_clean(name="b", include_dirty=True)
    assert r3["removed"] == ["b"]
    assert not os.path.isdir(wb)

    # 不存在的名字：空结果，不报错
    assert app.worktree_clean(name="nope") == {"removed": [], "kept": []}
