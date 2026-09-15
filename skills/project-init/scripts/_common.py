# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors
"""project-init 脚本共享工具：路径约定 + INDEX.md 行级维护。纯 stdlib。

INDEX.md 行格式固定（脚本是唯一写入方之一，Agent 手工补充时也必须保持）：

    | 文件 | 版本 | 日期 | 状态 | 说明 |      ← 主表（当前/历史）
    ## 归档
    | 文件 | 归档日期 |                        ← 归档表
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# 目录约定（与 docs/project-structure.md 一致）
MATERIAL_DIR = "素材"
MATERIAL_SUBDIRS = ("原始", "参考")
OUTPUT_DIR = "产出物"
WORK_SUBDIR = "中间产物"
ARCHIVE_SUBDIR = "归档"
DEFAULT_CATEGORIES = ("报告", "演示", "表格数据", "图表", "专利", "论文")

# 「禁止补充文件」硬性规则的违例命名（lint 用；大小写不敏感）
FORBIDDEN_PATTERNS = ("补充", "追加", "addendum", "supplement")

_MAIN_ROW_SEP = re.compile(r"^\|\s*---\s*\|\s*---\s*\|\s*---\s*\|\s*---\s*\|\s*---\s*\|")
_ARCHIVE_ROW_SEP = re.compile(r"^\|\s*---\s*\|\s*---\s*\|")


def _utf8_stdout() -> None:
    """Windows 控制台默认 GBK：统一切到 UTF-8，避免中文输出报错。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def today() -> str:
    import datetime

    return datetime.date.today().isoformat()


def category_dir(root: Path, category: str) -> Path:
    return root / OUTPUT_DIR / category


def sanitize_name(name: str) -> str:
    """清理交付物主题名：去空白/路径分隔/非法字符。"""
    name = (name or "").strip()
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    return name.strip()


def sanitize_ext(ext: str) -> str:
    ext = (ext or "").strip().lstrip(".").lower()
    return re.sub(r"[^A-Za-z0-9]", "", ext)


def index_header(category: str) -> str:
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


def ensure_category(root: Path, category: str) -> Path:
    """确保品类目录（含 中间产物/ 归档/ INDEX.md）存在，返回品类目录。"""
    d = category_dir(root, category)
    (d / WORK_SUBDIR).mkdir(parents=True, exist_ok=True)
    (d / ARCHIVE_SUBDIR).mkdir(parents=True, exist_ok=True)
    index = d / "INDEX.md"
    if not index.exists():
        index.write_text(index_header(category), encoding="utf-8")
    return d


def upsert_row(index_path: Path, file_name: str, version: str, date: str,
               status: str, note: str = "") -> None:
    """主表 upsert 一行（同名文件行存在则替换，否则插到表头分隔行后）。"""
    row = f"| {file_name} | {version} | {date} | {status} | {note} |"
    lines = (index_path.read_text(encoding="utf-8") if index_path.exists() else index_header("")).splitlines()
    out: list = []
    replaced = False
    for line in lines:
        if line.startswith(f"| {file_name} |"):
            out.append(row)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        result: list = []
        inserted = False
        for line in out:
            result.append(line)
            if not inserted and _MAIN_ROW_SEP.match(line.strip()):
                result.append(row)
                inserted = True
        if not inserted:
            result.append(row)
        out = result
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")


def archive_row(index_path: Path, file_name: str, archived_name: str, date: str) -> None:
    """主表移除 file_name 行，并在「## 归档」表追加一行。"""
    lines = (index_path.read_text(encoding="utf-8") if index_path.exists() else index_header("")).splitlines()
    out = [l for l in lines if not l.startswith(f"| {file_name} |")]
    row = f"| {archived_name} | {date} |"
    insert_at = None
    in_archive = False
    for i, line in enumerate(out):
        stripped = line.strip()
        if stripped.startswith("## 归档"):
            in_archive = True
        elif in_archive and _ARCHIVE_ROW_SEP.match(stripped):
            insert_at = i + 1
            break
    if insert_at is None:
        out.extend(["", "## 归档", "", "| 文件 | 归档日期 |", "| --- | --- |", row])
    else:
        out.insert(insert_at, row)
    index_path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")


def next_version(cat_dir: Path, name: str, ext: str) -> int:
    """当前目录 + 归档目录中出现过的最大 _vN + 1（版本号只增不减，不复用）。"""
    pat = re.compile(rf"^{re.escape(name)}_v(\d+)\.{re.escape(ext)}$")
    date_prefix = re.compile(r"^\d{4}-\d{2}-\d{2}_(.+)$")
    best = 0
    for base in (cat_dir, cat_dir / ARCHIVE_SUBDIR):
        if not base.is_dir():
            continue
        for f in base.iterdir():
            if not f.is_file():
                continue
            fname = f.name
            m = date_prefix.match(fname)
            if m:
                fname = m.group(1)
            mm = pat.match(fname)
            if mm:
                best = max(best, int(mm.group(1)))
    return best + 1


def is_forbidden_name(file_name: str) -> bool:
    low = file_name.lower()
    return any(p in low for p in FORBIDDEN_PATTERNS)


def iter_category_roots(root: Path):
    """产出物/ 下的品类目录（跳过非目录项）。"""
    out_root = root / OUTPUT_DIR
    if not out_root.is_dir():
        return
    for d in sorted(out_root.iterdir()):
        if d.is_dir():
            yield d
