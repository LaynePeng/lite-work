#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors
"""脚手架：初始化项目结构（幂等，绝不覆盖已有文件）。

创建：
- 素材/原始、素材/参考                    （输入，Agent 只读）
- 产出物/INDEX.md（总览）
- 产出物/<品类>/{INDEX.md, 中间产物/, 归档/}（默认品类可用 --categories 覆盖）
- AGENTS.md（仅当不存在时，从模板生成并填入项目名）

用法（脚本位于技能目录 scripts/ 下，相对技能目录调用）：
    python scripts/scaffold.py <项目根> [--categories 报告,演示,专利]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C  # noqa: E402


def main() -> int:
    C._utf8_stdout()
    ap = argparse.ArgumentParser(description="初始化项目结构（幂等）")
    ap.add_argument("project_root", nargs="?", default=".", help="项目根目录（默认当前目录）")
    ap.add_argument("--categories", default="", help="逗号分隔的品类列表，覆盖默认品类")
    args = ap.parse_args()

    root = Path(args.project_root).expanduser().resolve()
    if not root.is_dir():
        print(f"错误：项目根不存在：{root}", file=sys.stderr)
        return 2

    categories = [c.strip() for c in args.categories.split(",") if c.strip()]
    if not categories:
        categories = list(C.DEFAULT_CATEGORIES)

    created: list = []
    kept: list = []

    # 素材（只读输入）
    for sub in C.MATERIAL_SUBDIRS:
        d = root / C.MATERIAL_DIR / sub
        if d.is_dir():
            kept.append(d.relative_to(root).as_posix())
        else:
            d.mkdir(parents=True)
            created.append(d.relative_to(root).as_posix())

    # 产出物总览
    out_root = root / C.OUTPUT_DIR
    out_index = out_root / "INDEX.md"
    if out_index.exists():
        kept.append(out_index.relative_to(root).as_posix())
    else:
        out_root.mkdir(parents=True, exist_ok=True)
        rows = "\n".join(f"| {c} |  |" for c in categories)
        out_index.write_text(
            "# 产出物 · 总览\n\n"
            "> 各品类明细见 `产出物/<品类>/INDEX.md`；本表只列品类。\n\n"
            "| 品类 | 说明 |\n| --- | --- |\n" + rows + "\n",
            encoding="utf-8",
        )
        created.append(out_index.relative_to(root).as_posix())

    # 品类目录
    for cat in categories:
        existed = C.category_dir(root, cat).is_dir()
        C.ensure_category(root, cat)
        rel = C.category_dir(root, cat).relative_to(root).as_posix()
        (kept if existed else created).append(rel)

    # AGENTS.md（只在缺失时生成，已有则提示合并）
    agents = root / "AGENTS.md"
    if agents.exists():
        kept.append("AGENTS.md")
    else:
        template = Path(__file__).resolve().parent.parent / "templates" / "AGENTS.md"
        if not template.is_file():
            print(f"错误：找不到模板 {template}", file=sys.stderr)
            return 2
        content = template.read_text(encoding="utf-8")
        content = content.replace("{{PROJECT_NAME}}", root.name)
        agents.write_text(content, encoding="utf-8")
        created.append("AGENTS.md")

    print("项目结构初始化完成（幂等，未覆盖任何已有文件）")
    for rel in created:
        print(f"  + 新建 {rel}")
    for rel in kept:
        print(f"  = 保留 {rel}")
    if not created:
        print("  （结构已完整，无需改动）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
