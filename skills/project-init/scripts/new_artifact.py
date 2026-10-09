#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors
"""分配下一个版本路径：<主题>_v(N+1).<ext>（项目根目录），并确保目录结构完整。

版本号只增不减（统计根目录 + 归档/ 出现过的最大 _vN，兼容旧结构统计），
不复用旧号。打印出的相对路径即本次交付物的写入目标——请把内容写到这里，
不要新建「补充」文件（见 AGENTS.md 文档更新规则）。

用法：
    python scripts/new_artifact.py --name 季度方案 --ext docx \
        [--project-root .]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C  # noqa: E402


def main() -> int:
    C._utf8_stdout()
    ap = argparse.ArgumentParser(description="分配下一版本交付物路径（项目根目录）")
    ap.add_argument("--name", required=True, help="交付物主题名（不含版本号）")
    ap.add_argument("--ext", required=True, help="扩展名（不含点，如 docx/pdf）")
    ap.add_argument("--project-root", default=".", help="项目根目录（默认当前目录）")
    args = ap.parse_args()

    root = Path(args.project_root).expanduser().resolve()
    if not root.is_dir():
        print(f"错误：项目根不存在：{root}", file=sys.stderr)
        return 2

    name = C.sanitize_name(args.name)
    ext = C.sanitize_ext(args.ext)
    if not name or not ext:
        print("错误：--name/--ext 不能为空", file=sys.stderr)
        return 2

    C.ensure_dirs(root)
    n = C.next_version(root, name, ext)
    file_name = f"{name}_v{n}.{ext}"

    # 提示：文件本身由调用方（Agent/工具）创建，这里只分配路径
    print(file_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
