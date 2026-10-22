# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""编辑器工具测试（对应第7课（安全代码操作）：Search-Replace 模糊退避 + Unified Diff 锚点偏移）。"""
import asyncio
import os

from litework.tools.editor import BlockReplacer, DiffPatcher, EditorTools


def test_block_replacer_exact_match():
    src = "def foo():\n    return 1\n"
    ok, result, _ = BlockReplacer().replace_block(src, "    return 1", "    return 2")
    assert ok and "return 2" in result and "return 1" not in result


def test_block_replacer_fuzzy_with_indent_preservation():
    src = "def foo():\n    return 1\n\ndef bar():\n    return 2\n"
    # 搜索块缩进错误（少一个空格），模糊匹配应成功并保持原缩进
    ok, result, _ = BlockReplacer().replace_block(src, "   return 1", "   return 99")
    assert ok
    assert "    return 99" in result  # 缩进被还原为 4 空格
    assert "return 1" not in result


def test_block_replacer_no_match_reports_reason():
    ok, _, reason = BlockReplacer().replace_block("aaa", "bbb", "ccc")
    assert not ok and "未找到" in reason


def test_diff_patcher_basic():
    src = "line1\nline2\nline3\nline4\n"
    patch = "@@ -2,2 +2,2 @@\n line2\n-line3\n+line3-modified\n"
    ok, result, _ = DiffPatcher().apply_patch(src, patch)
    assert ok and "line3-modified" in result and "line3\n" not in result


def test_diff_patcher_anchor_offset():
    """LLM 给的行号偏了 3 行，锚点滑动修正仍应应用成功。"""
    src = "".join(f"line{i}\n" for i in range(1, 30))
    patch = "@@ -25,2 +25,2 @@\n line14\n-line15\n+line15-CHANGED\n"
    ok, result, _ = DiffPatcher().apply_patch(src, patch)
    assert ok and "line15-CHANGED" in result


def test_diff_patcher_anchor_not_found():
    src = "a\nb\nc\n"
    patch = "@@ -1,1 +1,1 @@\n-zzzz\n+yyyy\n"
    ok, _, error = DiffPatcher().apply_patch(src, patch)
    assert not ok and "锚点" in error


def test_editor_result_contains_file_and_line_counts(tmp_path):
    """修改结果必须带文件路径与增删行数（+N -M），并附 diff。"""
    tools = EditorTools(str(tmp_path))
    p = tmp_path / "a.ts"
    p.write_text("line1\nline2\nline3\nline4\n", encoding="utf-8")

    result = asyncio.run(tools.execute("apply_search_replace", {
        "filePath": "a.ts",
        "searchBlock": "line3",
        "replaceBlock": "line3-new\nline3-extra",
    }))
    assert result.startswith("[Patch Success]: 已更新 a.ts (+2 -1)")
    assert "a.ts" in result and "+++" in result and "@@" in result
    assert p.read_text(encoding="utf-8") == "line1\nline2\nline3-new\nline3-extra\nline4\n"


def test_editor_diff_stats_counts(tmp_path):
    """增删计数正确：+3 -2（加 3 行删 2 行）。"""
    tools = EditorTools(str(tmp_path))
    p = tmp_path / "b.ts"
    p.write_text("a\nb\nc\nd\n", encoding="utf-8")

    result = asyncio.run(tools.execute("apply_search_replace", {
        "filePath": "b.ts",
        "searchBlock": "b\nc",
        "replaceBlock": "b2\nc2\nc3",
    }))
    assert result.startswith("[Patch Success]: 已更新 b.ts (+3 -2)")

# ---------------------------------------------------------------- edit_files（复合工具）

def test_edit_files_multi_file_all_ok(tmp_path):
    """多文件多补丁一次应用：全部成功，逐条 [OK] 汇总。"""
    tools = EditorTools(str(tmp_path))
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y = 2\n", encoding="utf-8")

    result = asyncio.run(tools.execute("edit_files", {"edits": [
        {"filePath": "a.py", "searchBlock": "x = 1", "replaceBlock": "x = 10"},
        {"filePath": "b.py", "searchBlock": "y = 2", "replaceBlock": "y = 20"},
    ]}))
    assert result.startswith("[Edit Files Success]: 2/2")
    assert "[OK] a.py" in result and "[OK] b.py" in result
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 10\n"
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "y = 20\n"


def test_edit_files_validation_failure_zero_writes(tmp_path):
    """两阶段语义：第 2 条校验失败 → 磁盘零改动（第 1 条也不落盘）。"""
    tools = EditorTools(str(tmp_path))
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    result = asyncio.run(tools.execute("edit_files", {"edits": [
        {"filePath": "a.py", "searchBlock": "x = 1", "replaceBlock": "x = 10"},
        {"filePath": "a.py", "searchBlock": "不存在的块", "replaceBlock": "zzz"},
    ]}))
    assert result.startswith("[Edit Files Aborted]")
    assert "磁盘未改动" in result
    # 关键：第 1 条也不能落盘
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 1\n"


def test_edit_files_same_file_sequential_edits(tmp_path):
    """同文件多条按序应用：第 2 条的 search 针对第 1 条的结果。"""
    tools = EditorTools(str(tmp_path))
    (tmp_path / "c.py").write_text("a\nb\nc\n", encoding="utf-8")

    result = asyncio.run(tools.execute("edit_files", {"edits": [
        {"filePath": "c.py", "searchBlock": "a", "replaceBlock": "A1"},
        {"filePath": "c.py", "searchBlock": "b", "replaceBlock": "B2"},
    ]}))
    assert result.startswith("[Edit Files Success]: 2/2")
    assert (tmp_path / "c.py").read_text(encoding="utf-8") == "A1\nB2\nc\n"


def test_edit_files_stop_on_error_false_skips_bad(tmp_path):
    """stop_on_error=false：坏条目跳过，好条目照常应用。"""
    tools = EditorTools(str(tmp_path))
    (tmp_path / "d.py").write_text("m\nn\n", encoding="utf-8")

    result = asyncio.run(tools.execute("edit_files", {
        "stop_on_error": False,
        "edits": [
            {"filePath": "missing.py", "searchBlock": "x", "replaceBlock": "y"},
            {"filePath": "d.py", "searchBlock": "m", "replaceBlock": "M"},
        ]}))
    assert result.startswith("[Edit Files Success]: 1/2")
    assert "跳过" in result and "missing.py" in result
    assert (tmp_path / "d.py").read_text(encoding="utf-8") == "M\nn\n"


def test_edit_files_unified_diff_mode(tmp_path):
    """mode=unified_diff 条目混用。"""
    tools = EditorTools(str(tmp_path))
    (tmp_path / "e.py").write_text("line1\nline2\nline3\n", encoding="utf-8")

    result = asyncio.run(tools.execute("edit_files", {"edits": [{
        "filePath": "e.py", "mode": "unified_diff",
        "diff": "@@ -1,3 +1,3 @@\n line1\n-line2\n+line2-x\n line3\n",
    }]}))
    assert result.startswith("[Edit Files Success]: 1/1")
    assert "line2-x" in (tmp_path / "e.py").read_text(encoding="utf-8")


def test_edit_files_rejects_empty_and_oversize(tmp_path):
    """空列表与超上限（>20）拒绝。"""
    tools = EditorTools(str(tmp_path))
    assert asyncio.run(tools.execute("edit_files", {"edits": []})).startswith("[Edit Files Error]")
    big = [{"filePath": "a.py", "searchBlock": "x", "replaceBlock": "y"}] * 21
    assert asyncio.run(tools.execute("edit_files", {"edits": big})).startswith("[Edit Files Error]")
