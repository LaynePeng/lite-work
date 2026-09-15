#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors
"""分配下一个版本路径：产出物/<品类>/<主题>_v(N+1).<ext>，并登记 INDEX。

版本号只增不减（统计当前目录 + 归档里出现过的最大 _vN），不复用旧号。
打印出的相对路径即本次交付物的写入目标——请把内容写到这里，
不要新建「补充」文件（见 AGENTS.md 文档更新规则）。

用法：
    python scripts/new_artifact.py --category 报告 --name 季度方案 --ext docx \
        [--project-root .] [--note 说明] [--no-index]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C  # noqa: E402


def main() -> int:
    C._utf8_stdout()
    ap = argparse.ArgumentParser(description="分配下一版本交付物路径")
    ap.add_argument("--category", required=True, help="品类（如 报告/专利）")
    ap.add_argument("--name", required=True, help="交付物主题名（不含版本号）")
    ap.add_argument("--ext", required=True, help="扩展名（不含点，如 docx/pdf）")
    ap.add_argument("--project-root", default=".", help="项目根目录（默认当前目录）")
    ap.add_argument("--note", default="", help="说明（写入 INDEX）")
    ap.add_argument("--no-index", action="store_true", help="不更新 INDEX.md")
    args = ap.parse_args()

    root = Path(args.project_root).expanduser().resolve()
    if not root.is_dir():
        print(f"错误：项目根不存在：{root}", file=sys.stderr)
        return 2

    name = C.sanitize_name(args.name)
    ext = C.sanitize_ext(args.ext)
    category = C.sanitize_name(args.category)
    if not name or not ext or not category:
        print("错误：--category/--name/--ext 不能为空", file=sys.stderr)
        return 2

    cat_dir = C.ensure_category(root, category)
    n = C.next_version(cat_dir, name, ext)
    file_name = f"{name}_v{n}.{ext}"
    rel = f"{C.OUTPUT_DIR}/{category}/{file_name}"

    if not args.no_index:
        C.upsert_row(cat_dir / "INDEX.md", file_name, f"v{n}", C.today(), "草稿", args.note)
    # 提示：文件本身由调用方（Agent/工具）创建，这里只分配路径
    print(rel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
