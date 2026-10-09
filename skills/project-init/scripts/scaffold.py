#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors
"""脚手架：初始化项目结构（幂等，绝不覆盖已有文件）。

v2 结构（成品平铺直给）：
- 素材/原始、素材/参考          （输入，Agent 只读）
- 中间产物/、归档/              （顶层可见目录）
- 交付物不建目录：Agent 生成时直接写项目根目录
- AGENTS.md（仅当不存在时，从模板生成并填入项目名）

用法（脚本位于技能目录 scripts/ 下，相对技能目录调用）：
    python scripts/scaffold.py <项目根>
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
    args = ap.parse_args()

    root = Path(args.project_root).expanduser().resolve()
    if not root.is_dir():
        print(f"错误：项目根不存在：{root}", file=sys.stderr)
        return 2

    created: list = []
    kept: list = []

    def _ensure_dir(rel_path: Path) -> None:
        if rel_path.is_dir():
            kept.append(rel_path.relative_to(root).as_posix())
        else:
            rel_path.mkdir(parents=True)
            created.append(rel_path.relative_to(root).as_posix())

    # 素材（只读输入）+ 顶层 中间产物/ 归档/
    for sub in C.MATERIAL_SUBDIRS:
        _ensure_dir(root / C.MATERIAL_DIR / sub)
    _ensure_dir(root / C.WORK_DIR)
    _ensure_dir(root / C.ARCHIVE_DIR)

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

    # 运行环境清单（W5）：.litework/project.json——按项目类型检测产出初版。
    # 仅当缺失时生成（幂等）；dev 态直接 import 主程序，打包态从 litework 包取。
    manifest = root / ".litework" / "project.json"
    if manifest.exists():
        kept.append(".litework/project.json")
    else:
        try:
            from litework.core.runtime_manifest import (
                generate_runtime_manifest, save_runtime_manifest,
            )
            if save_runtime_manifest(str(root), generate_runtime_manifest(str(root))):
                created.append(".litework/project.json")
            else:
                kept.append(".litework/project.json")
        except ImportError:
            pass  # 主程序不可用（纯技能分发场景）→ 跳过，不阻断

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
