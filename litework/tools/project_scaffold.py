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

# 目录约定（与 skills/project-init/scripts/_common.py 一致）。
# v2 新项目结构（参考 Muse/dots 的 Artifacts 思路：成品平铺直给）：
# - 交付物直接放项目根目录（不建 产出物/ 目录树）；
# - 中间产物/ 与 归档/ 提升为顶层目录；
# - 存量项目的 产出物/ 旧结构保留识别（兼容，不迁移）。
MATERIAL_DIR = "素材"
MATERIAL_SUBDIRS = ("原始", "参考")
OUTPUT_DIR = "产出物"          # 仅存量项目识别用，新项目不再创建
WORK_DIR = "中间产物"           # 顶层：草稿/检索/渲染中间件（非交付）
ARCHIVE_DIR = "归档"            # 顶层：旧版本（YYYY-MM-DD_<原名>）

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

    v2 结构（成品平铺直给）：
    - 素材/{原始,参考}
    - 中间产物/、归档/（顶层，可见目录）
    - 交付物不建目录：Agent 生成时直接写项目根目录
    - AGENTS.md：仅当缺失时从技能模板生成（填入项目名）；已存在不动

    categories 参数保留只为兼容旧调用方签名，v2 结构不再使用。
    返回 {"created": [...], "kept": [...]}（相对项目根的路径）。
    """
    root_path = Path(root)
    if not root_path.is_dir():
        raise ValueError(f"项目根不存在: {root}")

    created: List[str] = []
    kept: List[str] = []

    def _ensure_dir(rel: Path) -> None:
        if rel.is_dir():
            kept.append(rel.as_posix())
        else:
            rel.mkdir(parents=True)
            created.append(rel.as_posix())

    for sub in MATERIAL_SUBDIRS:
        _ensure_dir(root_path / MATERIAL_DIR / sub)
    _ensure_dir(root_path / WORK_DIR)
    _ensure_dir(root_path / ARCHIVE_DIR)

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

    # 运行环境清单：.litework/project.json——按项目类型检测产出初版。
    # 仅当缺失时生成（幂等）；桌面端「新建项目」与 Agent 手动 scaffold 共用此入口。
    from ..core.runtime_manifest import generate_runtime_manifest, save_runtime_manifest
    if save_runtime_manifest(str(root_path), generate_runtime_manifest(str(root_path))):
        created.append(".litework/project.json")
    else:
        kept.append(".litework/project.json")

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
    2. 存在项目结构（素材/ 或 中间产物/ 或 归档/，或旧结构的 产出物/）→ project；
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
    if any((root / d).is_dir() for d in (MATERIAL_DIR, WORK_DIR, ARCHIVE_DIR, OUTPUT_DIR)):
        return "project"
    code_n, doc_n = _count_code_vs_doc(root)
    if doc_n > code_n and doc_n > 0:
        return "project"
    if code_n > 0:
        return "code"
    if (root / ".git").is_dir():
        return "code"
    return "project"
