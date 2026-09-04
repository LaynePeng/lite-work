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