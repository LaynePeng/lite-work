#!/usr/bin/env python3
"""给**已有** docx 套用主题（美化 office-plugin 的 docx_create 产物）。

只改样式（字体/字号/配色/边距/页脚/表格底纹），不改动任何正文文字。

用法::

    python3 apply_docx_theme.py "产出物/方案.docx" --theme gov-red
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import themes as _themes   # noqa: E402
import docx_theme          # noqa: E402


def restyle(path: str, theme: dict) -> str:
    from docx import Document
    doc = Document(path)
    docx_theme.apply_theme(doc, theme)
    for table in doc.tables:
        docx_theme.style_table(table, theme)
    doc.save(path)
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="给已有 docx 套用主题")
    ap.add_argument("path", help="要美化的 docx 路径")
    ap.add_argument("--theme", default="business-blue", help="主题名（themes.py list）")
    a = ap.parse_args()

    if not os.path.isfile(a.path):
        print("[ERR] 文件不存在: %s" % a.path)
        return 2
    theme = _themes.load(a.theme)
    restyle(a.path, theme)
    print("[OK] 已美化 %s（主题：%s）" % (a.path, theme.get("label", a.theme)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
