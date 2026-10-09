# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors
"""project-init 脚本共享工具：路径约定 + 版本演进。纯 stdlib。

v2 目录约定（与 docs/project-structure.md 一致；参考 Muse/dots 的 Artifacts
思路——成品平铺直给，过程文件退到幕后）：
- 交付物直接放项目根目录（`<主题>_vN.<ext>`），不建 产出物/ 目录树；
- 中间产物/ 与 归档/ 为顶层可见目录；
- 存量项目的 产出物/ 旧结构保留识别（兼容，不迁移）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# 目录约定
MATERIAL_DIR = "素材"
MATERIAL_SUBDIRS = ("原始", "参考")
OUTPUT_DIR = "产出物"          # 仅存量项目识别用（兼容），新项目不再创建
WORK_DIR = "中间产物"           # 顶层：草稿/检索/渲染中间件（非交付）
ARCHIVE_DIR = "归档"            # 顶层：旧版本（YYYY-MM-DD_<原名>）

# 交付物扩展名（lint 用：只对疑似交付物检查「禁止补充文件」）
DELIVERABLE_EXTS = {
    ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".pdf",
    ".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp",
    ".md", ".txt", ".csv", ".html", ".zip", ".mmd", ".puml",
}

# 「禁止补充文件」硬性规则的违例命名（lint 用；大小写不敏感）
FORBIDDEN_PATTERNS = ("补充", "追加", "addendum", "supplement")


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


def sanitize_name(name: str) -> str:
    """清理交付物主题名：去空白/路径分隔/非法字符。"""
    name = (name or "").strip()
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    return name.strip()


def sanitize_ext(ext: str) -> str:
    ext = (ext or "").strip().lstrip(".").lower()
    return re.sub(r"[^A-Za-z0-9]", "", ext)


def ensure_dirs(root: Path) -> None:
    """确保顶层目录存在（幂等）：素材/{原始,参考}、中间产物/、归档/。"""
    for sub in MATERIAL_SUBDIRS:
        (root / MATERIAL_DIR / sub).mkdir(parents=True, exist_ok=True)
    (root / WORK_DIR).mkdir(parents=True, exist_ok=True)
    (root / ARCHIVE_DIR).mkdir(parents=True, exist_ok=True)


def next_version(root: Path, name: str, ext: str) -> int:
    """根目录 + 归档/ 中出现过的最大 _vN + 1（版本号只增不减，不复用）。

    兼容存量项目：同时统计旧结构 产出物/<品类>/ 下的同名文件。
    """
    pat = re.compile(rf"^{re.escape(name)}_v(\d+)\.{re.escape(ext)}$")
    date_prefix = re.compile(r"^\d{4}-\d{2}-\d{2}_(.+)$")
    best = 0

    def _scan(base: Path, depth: int = 0) -> None:
        nonlocal best
        if not base.is_dir() or depth > 2:
            return
        for f in base.iterdir():
            if f.is_file():
                fname = f.name
                m = date_prefix.match(fname)
                if m:
                    fname = m.group(1)
                mm = pat.match(fname)
                if mm:
                    best = max(best, int(mm.group(1)))
            elif f.is_dir() and f.name not in (MATERIAL_DIR, WORK_DIR):
                _scan(f, depth + 1)

    _scan(root)          # 根目录交付物（v2）
    _scan(root / ARCHIVE_DIR)
    _scan(root / OUTPUT_DIR)   # 存量项目旧结构（只读统计）
    return best + 1


def is_forbidden_name(file_name: str) -> bool:
    low = file_name.lower()
    return any(p in low for p in FORBIDDEN_PATTERNS)


def is_deliverable(file_name: str) -> bool:
    ext = Path(file_name).suffix.lower()
    return ext in DELIVERABLE_EXTS
