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


# ---------------------------------------------------------------- git 原生合并

def _log(cwd: str) -> str:
    return _git(cwd, "log", "--oneline").stdout


def test_harvest_commits_changes_to_branch(tmp_path):
    """收割 = 把改动提交到分支（不再生成 patch），分支上留下真实提交。"""
    _init_repo(tmp_path)
    mgr = WorktreeManager(str(tmp_path))
    wt = mgr.create("feat")["path"]
    with open(os.path.join(wt, "README.md"), "w", encoding="utf-8") as f:
        f.write("# demo\n\nworktree edit\n")

    h = mgr.harvest("feat")
    assert h["committed"] is True
    assert h["commits"] >= 1
    # 分支上有 lite-work 提交，且工作区干净（改动已提交）
    assert "lite-work" in _log(wt)
    assert _git(wt, "status", "--porcelain").stdout.strip() == ""
    mgr.cleanup("feat")


def test_merge_creates_merge_commit_and_marks_merged(tmp_path):
    """合并走真实 git merge --no-ff：产生可追溯的 merge commit。"""
    _init_repo(tmp_path)
    mgr = WorktreeManager(str(tmp_path))
    wt = mgr.create("feat")["path"]
    with open(os.path.join(wt, "new.txt"), "w", encoding="utf-8") as f:
        f.write("from worktree\n")

    m = mgr.merge("feat")
    assert m["ok"] is True and m["merged"] == 1
    # 主工作区拿到文件 + 历史里有 merge commit
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "from worktree\n"
    assert "lite-work: 合并隔离工作树" in _log(str(tmp_path))
    # 分支已并入 → 可判定为已合并（AI 合并收尾据此清理）
    assert mgr.is_branch_merged("feat") is True
    assert mgr.has_merge_in_progress() is False
    mgr.cleanup("feat")


def test_merge_conflict_keeps_state_and_abort(tmp_path):
    """冲突：返回冲突文件、保持合并中状态；abort_merge 回退到合并前。"""
    _init_repo(tmp_path)
    mgr = WorktreeManager(str(tmp_path))
    wt = mgr.create("conflict")["path"]
    # worktree 与主工作区同时改同一文件的同一行
    with open(os.path.join(wt, "README.md"), "w", encoding="utf-8") as f:
        f.write("worktree version\n")
    mgr.harvest("conflict")
    with open(tmp_path / "README.md", "w", encoding="utf-8") as f:
        f.write("main version\n")
    _git(str(tmp_path), "commit", "-aqm", "main change")

    m = mgr.merge("conflict")
    assert m["ok"] is False
    assert m["conflicts"] == ["README.md"]
    assert mgr.has_merge_in_progress() is True
    # 冲突现场保留（文件带冲突标记），worktree 未丢
    assert os.path.isdir(wt)

    # 放弃合并 → 回到合并前（主分支内容为 main version）
    a = mgr.abort_merge()
    assert a["ok"] is True
    assert mgr.has_merge_in_progress() is False
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "main version\n"
    mgr.cleanup("conflict")


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
    # 依赖 symlink 不得进入提交内容（diff 里不能出现 .venv）
    assert ".venv" not in mgr.diff_text("deps")

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


# ---------------------------------------------------------------- 遗留管理（app 层）

def _make_app(tmp_path):
    from litework.app import AgentApp
    return AgentApp(workspace=str(tmp_path), config_dir=str(tmp_path / ".lc"))


def test_enable_creates_worktree_eagerly(tmp_path):
    """开启隔离模式应立即创建 worktree（而非等第一个任务）——否则 /worktree list 为空。"""
    _init_repo(tmp_path)
    app = _make_app(tmp_path)
    app.session_store.save("s1", [])
    r = app.set_session_worktree("s1", True)
    assert r["ok"] is True
    assert r["worktree"] and r["worktree"]["branch"] == "worktree-s1"
    # 磁盘上立刻可见 + list 能查到
    assert os.path.isdir(tmp_path / ".lite-work/worktrees/s1")
    assert [w["name"] for w in app.worktree_overview()["worktrees"]] == ["s1"]


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


def test_merge_worktree_prompt_mentions_branch_and_conflicts():
    """AI 合并任务的提示词：给出分支名、要求 git merge、冲突要解决而非放弃。"""
    from litework.server.tasks import _merge_worktree_prompt

    p = _merge_worktree_prompt("sess-1", "尽量保守")
    assert "worktree-sess-1" in p
    assert "git merge --no-ff" in p
    assert "冲突" in p and "merge --abort" in p
    assert "尽量保守" in p


def test_ai_merge_task_runs_in_main_workspace(tmp_path):
    """AI 合并任务必须落在主工作区（隔离模式开启时也要绕过），并注入合并指令。"""
    import asyncio

    from litework.server.tasks import TaskManager
    from tests.conftest import MockLLMAdapter

    _init_repo(tmp_path)
    app = _make_app(tmp_path)
    app._mock_adapter = MockLLMAdapter([("done", [])])
    sid = "merge-sess"
    app.session_store.save(sid, [])
    app.set_session_worktree(sid, True)   # 隔离模式开启
    assert app.session_worktree_enabled(sid) is True

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        tm = TaskManager(app)
        h = tm.start(sid, "请合并", agent_id="build", merge_worktree=sid)
        # 合并任务不做隔离：工作区为主工作区，loop 隔离根为空
        assert h.workspace is None
        assert h.worktree_name is None
        assert h.loop.isolation_root is None
        assert h.merge_worktree == sid
        h.task.cancel()
        loop.run_until_complete(asyncio.gather(h.task, return_exceptions=True))
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_resume_restores_worktree_from_existing_branch(tmp_path):
    """「回到之前的 worktree」：目录被删但分支还在 → 挂回原分支（真恢复）。"""
    _init_repo(tmp_path)
    mgr = WorktreeManager(str(tmp_path))
    wt = mgr.create("resume")["path"]
    with open(os.path.join(wt, "wip.txt"), "w", encoding="utf-8") as f:
        f.write("work in progress\n")
    mgr.harvest("resume")  # 提交到分支
    # 模拟目录被手工删除（分支仍在）
    import shutil as _sh
    _sh.rmtree(wt)
    mgr._git(str(tmp_path), "worktree", "prune")
    assert not os.path.isdir(wt)
    assert mgr.branch_exists("resume") is True

    r = mgr.create("resume")
    assert r["ok"] is True and r["restored"] is True
    # 之前的提交还在（不是新建的空工作树）
    assert os.path.isfile(os.path.join(r["path"], "wip.txt"))
    assert "lite-work" in _log(r["path"])
    mgr.cleanup("resume")


def test_create_fresh_when_branch_gone(tmp_path):
    """分支与目录都没了 → 新建（restored=False），不会误报为恢复。"""
    _init_repo(tmp_path)
    mgr = WorktreeManager(str(tmp_path))
    r = mgr.create("fresh")
    assert r["ok"] is True and r["restored"] is False
    mgr.cleanup("fresh")


def test_task_start_clears_flag_when_worktree_unavailable(tmp_path):
    """隔离模式开启但 worktree 既不能建也不能恢复 → 清除标记（不静默降级）。"""
    import asyncio
    import subprocess

    from litework.server.tasks import TaskManager
    from tests.conftest import MockLLMAdapter

    _init_repo(tmp_path)
    app = _make_app(tmp_path)
    app._mock_adapter = MockLLMAdapter([("done", [])])
    sid = "gone-sess"
    app.session_store.save(sid, [])
    app.set_session_worktree(sid, True)
    # 破坏 git：删掉 .git → create 与 branch_exists 都失败
    import shutil as _sh
    _sh.rmtree(os.path.join(str(tmp_path), ".git"))
    assert app.session_worktree_enabled(sid) is True

    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    try:
        tm = TaskManager(app)
        h = tm.start(sid, "干活", agent_id="build")
        # 隔离失效 → 标记被清除，任务回落主工作区（工作区为 None）
        assert h.workspace is None
        assert app.session_worktree_enabled(sid) is False
        h.task.cancel()
        loop.run_until_complete(asyncio.gather(h.task, return_exceptions=True))
    finally:
        loop.close(); asyncio.set_event_loop(None)


def test_new_untracked_files_visible_with_line_counts(tmp_path):
    """回归：新文件必须出现在变更列表（git diff 不含未跟踪文件），且行数计入 +。"""
    _init_repo(tmp_path)
    mgr = WorktreeManager(str(tmp_path))
    wt = mgr.create("newfiles")["path"]
    with open(os.path.join(wt, "brand_new.txt"), "w", encoding="utf-8") as f:
        f.write("l1\nl2\nl3\n")

    st = mgr.status("newfiles")
    paths = {f["path"]: f["status"] for f in st["files"]}
    assert "brand_new.txt" in paths, f"新文件应从 status ?? 补回: {paths}"
    assert paths["brand_new.txt"] == "A"
    assert st["adds"] >= 3, f"新文件行数应计入 adds: {st}"
    mgr.cleanup("newfiles")


def test_status_does_not_stage_worktree_index(tmp_path):
    """回归：status() 是只读操作，不得把文件偷偷 add 进 worktree 索引。"""
    _init_repo(tmp_path)
    mgr = WorktreeManager(str(tmp_path))
    wt = mgr.create("nostage")["path"]
    with open(os.path.join(wt, "x.txt"), "w", encoding="utf-8") as f:
        f.write("hello\n")

    mgr.status("nostage")   # 之前这里会执行 git add -A
    staged = _git(wt, "diff", "--cached", "--name-only").stdout.strip()
    assert staged == "", f"status() 不应改动暂存区，却有: {staged}"
    mgr.cleanup("nostage")


def test_binary_untracked_not_line_counted(tmp_path):
    """二进制/超大未跟踪文件仍出现在列表，但不计行数（adds 不因它暴涨）。"""
    _init_repo(tmp_path)
    mgr = WorktreeManager(str(tmp_path))
    wt = mgr.create("bin")["path"]
    with open(os.path.join(wt, "blob.bin"), "wb") as f:
        f.write(b"\x00\x01\x02" * 100)

    st = mgr.status("bin")
    assert any(f["path"] == "blob.bin" for f in st["files"])
    assert st["adds"] == 0, f"二进制不应计行数: {st}"
    mgr.cleanup("bin")


def test_worktree_clean_single_name(tmp_path):
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
