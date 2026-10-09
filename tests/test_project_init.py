# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""project-init 技能脚本测试（v2 结构：交付物在根目录，顶层 中间产物/归档）：
脚手架幂等、版本自增、归档命名、补充文件 lint。全部通过 subprocess 调用
真实脚本（不访问网络）。"""
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
    assert (tmp_path / "中间产物").is_dir()
    assert (tmp_path / "归档").is_dir()
    # v2 不再创建 产出物/ 目录树
    assert not (tmp_path / "产出物").exists()
    agents = tmp_path / "AGENTS.md"
    assert agents.is_file()
    assert tmp_path.name in agents.read_text(encoding="utf-8")
    assert "禁止新增补充文件" in agents.read_text(encoding="utf-8")
    assert "项目根目录" in agents.read_text(encoding="utf-8")


def test_scaffold_idempotent_and_keeps_agents(tmp_path: Path):
    run("scaffold.py", str(tmp_path))
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# 已有的项目规范\n", encoding="utf-8")
    r = run("scaffold.py", str(tmp_path))
    assert r.returncode == 0
    assert agents.read_text(encoding="utf-8") == "# 已有的项目规范\n"
    assert "保留" in r.stdout


def _new(root: Path, name: str, ext: str) -> str:
    r = run("new_artifact.py", "--name", name, "--ext", ext,
            "--project-root", str(root))
    assert r.returncode == 0, r.stderr
    return r.stdout.strip().splitlines()[-1]


def test_new_artifact_version_increments(tmp_path: Path):
    run("scaffold.py", str(tmp_path))
    p1 = _new(tmp_path, "季度方案", "docx")
    assert p1 == "季度方案_v1.docx"
    (tmp_path / p1).write_bytes(b"v1")
    p2 = _new(tmp_path, "季度方案", "docx")
    assert p2 == "季度方案_v2.docx"
    (tmp_path / p2).write_bytes(b"v2")
    # 版本号只增不减：归档 v1 后再分配仍是 v3
    run("archive_artifact.py", "--path", p1, "--project-root", str(tmp_path))
    p3 = _new(tmp_path, "季度方案", "docx")
    assert p3 == "季度方案_v3.docx"


def test_new_artifact_version_counts_archive_and_legacy(tmp_path: Path):
    """版本统计：根目录 + 归档/ + 存量 产出物/ 旧结构都计入。"""
    run("scaffold.py", str(tmp_path))
    # 归档里有 v5 → 下一版 v6
    (tmp_path / "归档" / "2026-01-01_年报_v5.pdf").write_bytes(b"x")
    p = _new(tmp_path, "年报", "pdf")
    assert p == "年报_v6.pdf"
    # 存量旧结构的同名文件也计入
    legacy = tmp_path / "产出物" / "报告"
    legacy.mkdir(parents=True)
    (legacy / "年报_v9.pdf").write_bytes(b"x")
    p2 = _new(tmp_path, "年报", "pdf")
    assert p2 == "年报_v10.pdf"


def test_archive_moves_to_toplevel_archive(tmp_path: Path):
    import re

    run("scaffold.py", str(tmp_path))
    p1 = _new(tmp_path, "季度方案", "docx")
    (tmp_path / p1).write_bytes(b"v1")
    p2 = _new(tmp_path, "季度方案", "docx")
    (tmp_path / p2).write_bytes(b"v2")

    r = run("archive_artifact.py", "--path", p1, "--project-root", str(tmp_path))
    assert r.returncode == 0, r.stderr
    dest = r.stdout.strip().splitlines()[-1]
    # 归档命名：归档/YYYY-MM-DD_季度方案_v1.docx（顶层归档目录）
    assert re.match(r"^归档/\d{4}-\d{2}-\d{2}_季度方案_v1\.docx$", dest)
    assert (tmp_path / dest).is_file()
    assert not (tmp_path / p1).exists()
    assert (tmp_path / p2).exists()


def test_archive_rejects_nested_path(tmp_path: Path):
    """v2：只归档根目录交付物；嵌套路径（如 素材/ 或旧结构）报错不动。"""
    run("scaffold.py", str(tmp_path))
    nested = tmp_path / "素材" / "随便.txt"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text("x", encoding="utf-8")
    r = run("archive_artifact.py", "--path", "素材/随便.txt", "--project-root", str(tmp_path))
    assert r.returncode == 2
    assert nested.exists()


def test_archive_in_archive_is_noop(tmp_path: Path):
    run("scaffold.py", str(tmp_path))
    arch = tmp_path / "归档" / "2026-01-01_旧_v1.docx"
    arch.parent.mkdir(parents=True, exist_ok=True)
    arch.write_bytes(b"x")
    r = run("archive_artifact.py", "--path", "归档/2026-01-01_旧_v1.docx",
            "--project-root", str(tmp_path))
    assert r.returncode == 0
    assert "跳过" in r.stdout
    assert arch.exists()


def test_lint_flags_supplement_files(tmp_path: Path):
    run("scaffold.py", str(tmp_path))
    # 根目录交付物命中违例命名
    bad = tmp_path / "方案_补充说明.md"
    bad.write_text("补充", encoding="utf-8")
    r = run("lint_artifacts.py", "--project-root", str(tmp_path))
    assert r.returncode == 1
    assert "方案_补充说明.md" in r.stdout
    # 英文 addendum 同样命中
    bad2 = tmp_path / "plan_addendum.md"
    bad2.write_text("x", encoding="utf-8")
    r2 = run("lint_artifacts.py", "--project-root", str(tmp_path))
    assert r2.returncode == 1
    assert "plan_addendum.md" in r2.stdout
    # 正常版本文件不误报
    ok = tmp_path / "方案_v1.md"
    ok.write_text("v1", encoding="utf-8")
    bad.unlink()
    bad2.unlink()
    r3 = run("lint_artifacts.py", "--project-root", str(tmp_path))
    assert r3.returncode == 0, r3.stdout + r3.stderr


def test_lint_scans_legacy_structure(tmp_path: Path):
    """兼容存量项目：旧结构 产出物/<品类>/ 顶层同样接受 lint。"""
    run("scaffold.py", str(tmp_path))
    legacy = tmp_path / "产出物" / "报告"
    legacy.mkdir(parents=True)
    (legacy / "方案_追加.md").write_text("x", encoding="utf-8")
    r = run("lint_artifacts.py", "--project-root", str(tmp_path))
    assert r.returncode == 1
    assert "产出物/报告/方案_追加.md" in r.stdout
