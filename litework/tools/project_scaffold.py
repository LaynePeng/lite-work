# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""项目脚手架与类型判定（新建项目自动初始化 / code-project 分类）。

为什么原生实现而不调用技能脚本：打包态后端是 PyInstaller frozen 二进制，
子进程 python 不可用；脚手架必须在包内直接可执行。技能
`skills/project-init/` 面向 Agent 的日常维护（版本/归档/INDEX）与存量项目
补建，两侧目录约定保持一致（改动需同步 `_common.py` 的常量）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 目录约定（与 skills/project-init/scripts/_common.py 一致）
MATERIAL_DIR = "素材"
MATERIAL_SUBDIRS = ("原始", "参考")
OUTPUT_DIR = "产出物"
WORK_SUBDIR = "中间产物"
ARCHIVE_SUBDIR = "归档"
DEFAULT_CATEGORIES = ("报告", "演示", "表格数据", "图表", "专利", "论文")

# 代码仓库标记文件（命中即判 code，优先级最高）
_CODE_MARKER_FILES = (
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "package.json", "Cargo.toml", "go.mod", "pom.xml", "build.gradle",
    "build.gradle.kts", "Gemfile", "composer.json", "mix.exs", "deno.json",
    "CMakeLists.txt", "Makefile",
)
_CODE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".cpp",
    ".h", ".hpp", ".cs", ".rb", ".php", ".swift", ".kt",
}
_DOC_EXTS = {".docx", ".doc", ".xlsx", ".xls", ".pptx", ".pdf"}

# 浅层扫描上限：控制大目录上的判定耗时
_SCAN_MAX_DEPTH = 2
_SCAN_MAX_FILES = 500
# 扫描时跳过的目录（隐藏/依赖/运行时，与分类语义无关）
_SCAN_SKIP_DIRS = {".git", ".lite-work", "node_modules", "__pycache__", "venv", ".venv"}


def _index_header(category: str) -> str:
    return (
        f"# 产出物 · {category}\n"
        "\n"
        "> 本清单由 project-init 脚本维护（行格式固定，手工补充请保持一致）。\n"
        "\n"
        "| 文件 | 版本 | 日期 | 状态 | 说明 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "\n"
        "## 归档\n"
        "\n"
        "| 文件 | 归档日期 |\n"
        "| --- | --- |\n"
    )


def _agents_template() -> Optional[str]:
    """定位内置技能 project-init 的 AGENTS.md 模板（dev / 打包态通用）。"""
    try:
        from .skills import _builtin_skills_dir

        base = _builtin_skills_dir()
        if not base:
            return None
        path = os.path.join(base, "project-init", "templates", "AGENTS.md")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def scaffold_project(root: str, categories: Optional[List[str]] = None) -> Dict[str, List[str]]:
    """初始化项目结构（幂等，绝不覆盖已有文件）。

    - 素材/{原始,参考}
    - 产出物/INDEX.md（总览）+ 各品类 {INDEX.md, 中间产物/, 归档/}
    - AGENTS.md：仅当缺失时从技能模板生成（填入项目名）；已存在不动

    返回 {"created": [...], "kept": [...]}（相对项目根的路径）。
    """
    root_path = Path(root)
    if not root_path.is_dir():
        raise ValueError(f"项目根不存在: {root}")
    cats = [c.strip() for c in (categories or []) if c.strip()] or list(DEFAULT_CATEGORIES)

    created: List[str] = []
    kept: List[str] = []

    def _ensure_dir(rel: Path) -> None:
        if rel.is_dir():
            kept.append(rel.as_posix())
        else:
            rel.mkdir(parents=True)
            created.append(rel.as_posix())

    def _ensure_file(rel: Path, content: str) -> None:
        if rel.exists():
            kept.append(rel.as_posix())
        else:
            rel.parent.mkdir(parents=True, exist_ok=True)
            rel.write_text(content, encoding="utf-8")
            created.append(rel.as_posix())

    for sub in MATERIAL_SUBDIRS:
        _ensure_dir(root_path / MATERIAL_DIR / sub)

    _ensure_file(
        root_path / OUTPUT_DIR / "INDEX.md",
        "# 产出物 · 总览\n\n"
        "> 各品类明细见 `产出物/<品类>/INDEX.md`；本表只列品类。\n\n"
        "| 品类 | 说明 |\n| --- | --- |\n"
        + "".join(f"| {c} |  |\n" for c in cats),
    )
    for cat in cats:
        _ensure_dir(root_path / OUTPUT_DIR / cat / WORK_SUBDIR)
        _ensure_dir(root_path / OUTPUT_DIR / cat / ARCHIVE_SUBDIR)
        _ensure_file(root_path / OUTPUT_DIR / cat / "INDEX.md", _index_header(cat))

    agents_rel = root_path / "AGENTS.md"
    if agents_rel.exists():
        kept.append("AGENTS.md")
    else:
        template = _agents_template()
        if template:
            agents_rel.write_text(
                template.replace("{{PROJECT_NAME}}", root_path.name), encoding="utf-8"
            )
            created.append("AGENTS.md")

    return {"created": created, "kept": kept}


def _count_code_vs_doc(root: Path) -> Tuple[int, int]:
    """浅层统计代码文件数与文档文件数（深度 ≤2、最多 500 个文件，跳过依赖/隐藏目录）。"""
    code_n = doc_n = 0
    scanned = 0
    base_depth = len(root.resolve().parts)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).resolve().parts) - base_depth
        if depth >= _SCAN_MAX_DEPTH:
            dirnames[:] = []
        else:
            dirnames[:] = [d for d in dirnames if d not in _SCAN_SKIP_DIRS and not d.startswith(".")]
        for fname in filenames:
            scanned += 1
            ext = os.path.splitext(fname)[1].lower()
            if ext in _CODE_EXTS:
                code_n += 1
            elif ext in _DOC_EXTS:
                doc_n += 1
            if scanned >= _SCAN_MAX_FILES:
                return code_n, doc_n
    return code_n, doc_n


def classify_project_kind(path: str) -> str:
    """判定项目类型：code（代码仓库）/ project（文档/通用项目）。

    优先级：
    1. 根目录存在代码标记文件（pyproject.toml / package.json 等）→ code；
    2. 存在项目结构（素材/ 或 产出物/）→ project；
    3. 浅层扫描：文档文件多于代码文件 → project；代码文件 ≥ 文档文件 → code；
    4. 兜底：仅 .git（空仓库按代码处理）→ code；其余 → project。

    与 git 解耦：git 只影响文件树分支展示，不再决定类型（此前 layr、
    lite-work、文档项目一律因 .git 被判成「代码」）。
    """
    root = Path(path)
    if not root.is_dir():
        return "project"
    for marker in _CODE_MARKER_FILES:
        if (root / marker).is_file():
            return "code"
    if (root / MATERIAL_DIR).is_dir() or (root / OUTPUT_DIR).is_dir():
        return "project"
    code_n, doc_n = _count_code_vs_doc(root)
    if doc_n > code_n and doc_n > 0:
        return "project"
    if code_n > 0:
        return "code"
    if (root / ".git").is_dir():
        return "code"
    return "project"
