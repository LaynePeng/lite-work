#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors
"""归档旧版本：把项目根目录的交付物移到 归档/YYYY-MM-DD_<原名>。

配套用法（一次修订的标准三步）：
1. new_artifact.py 分配 _v(N+1) 文件名并写入新内容
2. archive_artifact.py 归档旧版 _vN
3. 完成（v2 结构无 INDEX，归档目录即版本台账）

用法：
    python scripts/archive_artifact.py --path 季度方案_v1.docx [--project-root .]
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C  # noqa: E402


def main() -> int:
    C._utf8_stdout()
    ap = argparse.ArgumentParser(description="归档旧版本交付物")
    ap.add_argument("--path", required=True, help="要归档的文件（相对项目根或绝对路径）")
    ap.add_argument("--project-root", default=".", help="项目根目录（默认当前目录）")
    args = ap.parse_args()

    root = Path(args.project_root).expanduser().resolve()
    if not root.is_dir():
        print(f"错误：项目根不存在：{root}", file=sys.stderr)
        return 2

    p = Path(args.path).expanduser()
    if not p.is_absolute():
        p = root / p
    p = p.resolve()
    if not p.is_file():
        print(f"错误：文件不存在：{args.path}", file=sys.stderr)
        return 2
    if p.parent == (root / C.ARCHIVE_DIR).resolve():
        print(f"跳过：已在归档目录中：{args.path}")
        return 0
    # 只归档项目根目录的直接文件（交付物都在根目录；深层路径报错提示）
    if p.parent != root:
        print(
            f"错误：v2 结构的交付物在项目根目录，归档目标应为根目录文件：{args.path}\n"
            f"（存量旧结构 产出物/ 下的文件请保持在原结构内，不跨结构归档）",
            file=sys.stderr,
        )
        return 2

    archive = root / C.ARCHIVE_DIR
    archive.mkdir(parents=True, exist_ok=True)
    dest = archive / f"{C.today()}_{p.name}"
    i = 2
    while dest.exists():
        dest = archive / f"{C.today()}_{i}_{p.name}"
        i += 1
    shutil.move(str(p), str(dest))

    print(dest.relative_to(root).as_posix())
    return 0


if __name__ == "__main__":
    sys.exit(main())
