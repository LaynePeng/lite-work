#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors
"""检查「禁止补充文件」硬性规则：扫描 产出物/<品类>/ 顶层文件，
命中违例命名（*补充* / *追加* / *addendum* / *supplement*）即报警并以非零码退出。

修订/补充信息的正确做法：合并进下一版本（new_artifact → 写 _v(N+1) →
archive_artifact 归档旧版），而不是新增补充文件。

用法：
    python scripts/lint_artifacts.py [--project-root .]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C  # noqa: E402


def main() -> int:
    C._utf8_stdout()
    ap = argparse.ArgumentParser(description="检查补充文件等违例命名")
    ap.add_argument("--project-root", default=".", help="项目根目录（默认当前目录）")
    args = ap.parse_args()

    root = Path(args.project_root).expanduser().resolve()
    violations: list = []
    for cat_dir in C.iter_category_roots(root):
        if cat_dir.name in (C.WORK_SUBDIR, C.ARCHIVE_SUBDIR):
            continue
        for f in sorted(cat_dir.iterdir()):
            if f.is_file() and C.is_forbidden_name(f.name):
                violations.append(f.relative_to(root).as_posix())

    if violations:
        print("发现违例：以下文件疑似「补充文件」（应合并为下一版本，而不是新增文件）：")
        for rel in violations:
            print(f"  ✗ {rel}")
        print(
            "修复方式：读取对应当前版本 → 合并补充内容 → "
            "用 new_artifact.py 分配 _v(N+1) 写入 → archive_artifact.py 归档旧版。"
        )
        return 1
    print("✔ 未发现补充文件等违例命名")
    return 0


if __name__ == "__main__":
    sys.exit(main())
