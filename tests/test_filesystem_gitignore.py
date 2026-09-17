# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""D1 回归：_file_tree 的 .gitignore 解析走模块级 mtime 缓存，且改动后立即生效。"""

from litework.tools.filesystem import FileSystemTools, _GITIGNORE_CACHE


def test_file_tree_respects_gitignore(tmp_path):
    (tmp_path / "keep.txt").write_text("ok", encoding="utf-8")
    (tmp_path / "secret.log").write_text("no", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("x", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")

    tree = FileSystemTools(str(tmp_path))._file_tree({"maxDepth": 2})
    assert "keep.txt" in tree
    assert "secret.log" not in tree, f".gitignore 未生效:\n{tree}"
    assert "node_modules" not in tree, f"默认忽略项未生效:\n{tree}"


def test_gitignore_cache_hit_and_invalidation(tmp_path):
    _GITIGNORE_CACHE.clear()
    (tmp_path / "a.log").write_text("x", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")

    tools = FileSystemTools(str(tmp_path))
    spec1 = tools._load_gitignore()
    spec2 = tools._load_gitignore()
    assert spec1 is spec2, "同一文件未变化时应命中缓存（复用同一 PathSpec）"
    assert spec1.match_file("a.log")

    # 编辑 .gitignore → 签名变化 → 立即重建（不返回陈旧规则）
    (tmp_path / ".gitignore").write_text("", encoding="utf-8")
    spec3 = FileSystemTools(str(tmp_path))._load_gitignore()
    assert spec3 is not spec1, "编辑后应重建 PathSpec"
    assert not spec3.match_file("a.log"), "新规则未立即生效"
    _GITIGNORE_CACHE.clear()


def test_gitignore_cache_keyed_by_workspace(tmp_path):
    _GITIGNORE_CACHE.clear()
    ws_a = tmp_path / "a"
    ws_b = tmp_path / "b"
    ws_a.mkdir()
    ws_b.mkdir()
    (ws_a / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (ws_b / ".gitignore").write_text("*.tmp\n", encoding="utf-8")

    spec_a = FileSystemTools(str(ws_a))._load_gitignore()
    spec_b = FileSystemTools(str(ws_b))._load_gitignore()
    assert spec_a.match_file("x.log") and not spec_a.match_file("x.tmp")
    assert spec_b.match_file("x.tmp") and not spec_b.match_file("x.log")
    _GITIGNORE_CACHE.clear()
