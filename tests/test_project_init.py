# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""project-init 技能脚本测试：脚手架幂等、版本自增、归档命名与 INDEX 维护、
补充文件 lint。全部通过 subprocess 调用真实脚本（不访问网络）。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "project-init" / "scripts"


def run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / args[0]), *args[1:]],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(cwd) if cwd else None, timeout=60,
    )


def test_scaffold_creates_structure(tmp_path: Path):
    r = run("scaffold.py", str(tmp_path))
    assert r.returncode == 0, r.stderr
    for sub in ("原始", "参考"):
        assert (tmp_path / "素材" / sub).is_dir()
    assert (tmp_path / "产出物" / "INDEX.md").is_file()
    for cat in ("报告", "演示", "表格数据", "图表", "专利", "论文"):
        cat_dir = tmp_path / "产出物" / cat
        assert cat_dir.is_dir()
        assert (cat_dir / "中间产物").is_dir()
        assert (cat_dir / "归档").is_dir()
        assert (cat_dir / "INDEX.md").is_file()
    agents = tmp_path / "AGENTS.md"
    assert agents.is_file()
    assert tmp_path.name in agents.read_text(encoding="utf-8")
    assert "禁止新增补充文件" in agents.read_text(encoding="utf-8")


def test_scaffold_idempotent_and_keeps_agents(tmp_path: Path):
    run("scaffold.py", str(tmp_path))
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# 已有的项目规范\n", encoding="utf-8")
    r = run("scaffold.py", str(tmp_path))
    assert r.returncode == 0
    assert agents.read_text(encoding="utf-8") == "# 已有的项目规范\n"
    assert "保留" in r.stdout


def test_scaffold_custom_categories(tmp_path: Path):
    r = run("scaffold.py", str(tmp_path), "--categories", "专利,演示")
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "产出物" / "专利").is_dir()
    assert (tmp_path / "产出物" / "演示").is_dir()
    assert not (tmp_path / "产出物" / "报告").exists()


def _new(root: Path, category: str, name: str, ext: str, note: str = "") -> str:
    r = run("new_artifact.py", "--category", category, "--name", name,
            "--ext", ext, "--project-root", str(root), "--note", note)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip().splitlines()[-1]


def test_new_artifact_version_increments(tmp_path: Path):
    run("scaffold.py", str(tmp_path))
    p1 = _new(tmp_path, "报告", "季度方案", "docx", "初稿")
    assert p1 == "产出物/报告/季度方案_v1.docx"
    (tmp_path / p1).write_bytes(b"v1")
    p2 = _new(tmp_path, "报告", "季度方案", "docx", "修订")
    assert p2 == "产出物/报告/季度方案_v2.docx"
    (tmp_path / p2).write_bytes(b"v2")
    index = (tmp_path / "产出物" / "报告" / "INDEX.md").read_text(encoding="utf-8")
    assert "| 季度方案_v1.docx | v1 |" in index
    assert "| 季度方案_v2.docx | v2 |" in index
    # 版本号只增不减：归档 v1 后再分配仍是 v3
    run("archive_artifact.py", "--path", p1, "--project-root", str(tmp_path))
    p3 = _new(tmp_path, "报告", "季度方案", "docx")
    assert p3 == "产出物/报告/季度方案_v3.docx"


def test_new_artifact_creates_category(tmp_path: Path):
    run("scaffold.py", str(tmp_path))
    p = _new(tmp_path, "调研", "市场分析", "md")
    assert p == "产出物/调研/市场分析_v1.md"
    assert (tmp_path / "产出物" / "调研" / "归档").is_dir()
    assert (tmp_path / "产出物" / "调研" / "INDEX.md").is_file()


def test_archive_moves_and_updates_index(tmp_path: Path):
    import re

    run("scaffold.py", str(tmp_path))
    p1 = _new(tmp_path, "报告", "季度方案", "docx")
    (tmp_path / p1).write_bytes(b"v1")
    p2 = _new(tmp_path, "报告", "季度方案", "docx")
    (tmp_path / p2).write_bytes(b"v2")

    r = run("archive_artifact.py", "--path", p1, "--project-root", str(tmp_path))
    assert r.returncode == 0, r.stderr
    dest = r.stdout.strip().splitlines()[-1]
    # 归档命名：归档/YYYY-MM-DD_季度方案_v1.docx
    assert re.match(r"^产出物/报告/归档/\d{4}-\d{2}-\d{2}_季度方案_v1\.docx$", dest)
    assert (tmp_path / dest).is_file()
    assert not (tmp_path / p1).exists()
    assert (tmp_path / p2).exists()

    index = (tmp_path / "产出物" / "报告" / "INDEX.md").read_text(encoding="utf-8")
    assert "| 季度方案_v1.docx |" not in index            # 主表行已移除
    assert re.search(r"\| \d{4}-\d{2}-\d{2}_季度方案_v1\.docx \|", index)  # 归档表已有
    assert "| 季度方案_v2.docx | v2 |" in index


def test_archive_rejects_outside_outputs(tmp_path: Path):
    run("scaffold.py", str(tmp_path))
    outside = tmp_path / "随便.txt"
    outside.write_text("x", encoding="utf-8")
    r = run("archive_artifact.py", "--path", "随便.txt", "--project-root", str(tmp_path))
    assert r.returncode == 2
    assert outside.exists()


def test_lint_flags_supplement_files(tmp_path: Path):
    run("scaffold.py", str(tmp_path))
    bad = tmp_path / "产出物" / "报告" / "方案_补充说明.md"
    bad.write_text("补充", encoding="utf-8")
    r = run("lint_artifacts.py", "--project-root", str(tmp_path))
    assert r.returncode == 1
    assert "方案_补充说明.md" in r.stdout
    # 英文 addendum 同样命中
    bad2 = tmp_path / "产出物" / "报告" / "plan_addendum.md"
    bad2.write_text("x", encoding="utf-8")
    r2 = run("lint_artifacts.py", "--project-root", str(tmp_path))
    assert r2.returncode == 1
    assert "plan_addendum.md" in r2.stdout
    # 正常版本文件不误报
    ok = tmp_path / "产出物" / "报告" / "方案_v1.md"
    ok.write_text("v1", encoding="utf-8")
    bad.unlink()
    bad2.unlink()
    r3 = run("lint_artifacts.py", "--project-root", str(tmp_path))
    assert r3.returncode == 0, r3.stdout + r3.stderr
