# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""project_scaffold 测试：脚手架（幂等/模板/自定义品类）+ 项目类型启发式判定。"""
from __future__ import annotations

from pathlib import Path

from litework.tools.project_scaffold import (
    DEFAULT_CATEGORIES,
    MATERIAL_DIR,
    OUTPUT_DIR,
    classify_project_kind,
    scaffold_project,
)


# ---------------------------------------------------------------- scaffold

def test_scaffold_creates_structure(tmp_path: Path):
    r = scaffold_project(str(tmp_path))
    for sub in ("原始", "参考"):
        assert (tmp_path / MATERIAL_DIR / sub).is_dir()
    assert (tmp_path / OUTPUT_DIR / "INDEX.md").is_file()
    for cat in DEFAULT_CATEGORIES:
        cat_dir = tmp_path / OUTPUT_DIR / cat
        assert (cat_dir / "中间产物").is_dir()
        assert (cat_dir / "归档").is_dir()
        assert (cat_dir / "INDEX.md").is_file()
    # AGENTS.md 从内置技能模板生成（填入项目名）
    agents = tmp_path / "AGENTS.md"
    assert agents.is_file()
    content = agents.read_text(encoding="utf-8")
    assert tmp_path.name in content
    assert "{{PROJECT_NAME}}" not in content
    assert "禁止新增补充文件" in content
    assert "created" in r and "kept" in r
    assert "AGENTS.md" in r["created"]


def test_scaffold_idempotent_and_keeps_agents(tmp_path: Path):
    scaffold_project(str(tmp_path))
    (tmp_path / "AGENTS.md").write_text("# 已有规范\n", encoding="utf-8")
    r2 = scaffold_project(str(tmp_path))
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == "# 已有规范\n"
    assert r2["created"] == []
    assert "AGENTS.md" in r2["kept"]


def test_scaffold_custom_categories(tmp_path: Path):
    scaffold_project(str(tmp_path), categories=["专利", "调研"])
    assert (tmp_path / OUTPUT_DIR / "专利").is_dir()
    assert (tmp_path / OUTPUT_DIR / "调研").is_dir()
    assert not (tmp_path / OUTPUT_DIR / "报告").exists()


def test_scaffold_rejects_missing_root(tmp_path: Path):
    import pytest

    with pytest.raises(ValueError):
        scaffold_project(str(tmp_path / "nope"))


# ---------------------------------------------------------------- classify

def test_classify_code_markers(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    assert classify_project_kind(str(tmp_path)) == "code"
    (tmp_path / "pyproject.toml").unlink()
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    assert classify_project_kind(str(tmp_path)) == "code"


def test_classify_code_marker_wins_over_structure(tmp_path: Path):
    # 代码仓库即使有 产出物/（Agent 跑过办公工具）也仍是代码
    (tmp_path / "go.mod").write_text("", encoding="utf-8")
    (tmp_path / OUTPUT_DIR).mkdir()
    assert classify_project_kind(str(tmp_path)) == "code"


def test_classify_project_structure(tmp_path: Path):
    (tmp_path / MATERIAL_DIR).mkdir()
    assert classify_project_kind(str(tmp_path)) == "project"
    (tmp_path / MATERIAL_DIR).rmdir()
    (tmp_path / OUTPUT_DIR).mkdir()
    assert classify_project_kind(str(tmp_path)) == "project"


def test_classify_document_files_dominate(tmp_path: Path):
    # 文档项目：.git 存在（误 init）但内容是文档 → project（复旦mem 场景）
    (tmp_path / ".git").mkdir()
    for i in range(5):
        (tmp_path / f"笔记{i}.docx").write_bytes(b"x")
    (tmp_path / "总结.pdf").write_bytes(b"x")
    assert classify_project_kind(str(tmp_path)) == "project"


def test_classify_code_files_dominate(tmp_path: Path):
    for i in range(4):
        (tmp_path / f"m{i}.py").write_text("x", encoding="utf-8")
    (tmp_path / "readme.md").write_text("x", encoding="utf-8")
    assert classify_project_kind(str(tmp_path)) == "code"


def test_classify_git_only_fallback_code(tmp_path: Path):
    # 空仓库（新建代码）：.git 兜底为 code
    (tmp_path / ".git").mkdir()
    assert classify_project_kind(str(tmp_path)) == "code"


def test_classify_empty_and_missing(tmp_path: Path):
    # 空目录 → project；不存在的目录 → project（不抛异常）
    assert classify_project_kind(str(tmp_path)) == "project"
    assert classify_project_kind(str(tmp_path / "nope")) == "project"


def test_classify_scan_skips_hidden_and_deep(tmp_path: Path):
    # node_modules 里的代码文件不参与判定；深度 >2 的文件也不参与
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "a.js").write_text("x", encoding="utf-8")
    (tmp_path / "a" / "b" / "c").mkdir(parents=True)
    (tmp_path / "a" / "b" / "c" / "deep.py").write_text("x", encoding="utf-8")
    (tmp_path / "方案.docx").write_bytes(b"x")
    assert classify_project_kind(str(tmp_path)) == "project"
