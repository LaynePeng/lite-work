#!/usr/bin/env python3
"""极简 Markdown 解析：产出结构化块，供 docx / pdf 渲染器共用。

块类型（tuple 首元素为 kind）：

- ``("h", level, text)``
- ``("p", text)``
- ``("ul", text)`` / ``("ol", text)``
- ``("code", lang, code)``
- ``("table", rows)``   rows 为二维列表（已剔除分隔行）
- ``("img", alt, path)``
"""
from __future__ import annotations

import re

_SEP = re.compile(r"^[\s\-:|]+$")


def parse(md_text: str) -> list:
    blocks = []
    lines = (md_text or "").split("\n")
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]

        # 表格
        if line.strip().startswith("|") and line.strip().endswith("|"):
            rows, cols = [], None
            while (i < n and lines[i].strip().startswith("|")
                   and lines[i].strip().endswith("|")):
                inner = lines[i].strip().strip("|")
                if _SEP.match(inner):          # 分隔行 |---|---|
                    i += 1
                    continue
                cells = [c.strip() for c in inner.split("|")]
                if cols is None:
                    cols = len(cells)
                rows.append((cells + [""] * cols)[:cols])
                i += 1
            if rows:
                blocks.append(("table", rows))
            continue

        # 代码块
        cb = re.match(r"^```[ \t]*([A-Za-z0-9_+-]*)[ \t]*$", line.strip())
        if cb:
            lang = (cb.group(1) or "").lower()
            body = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1                              # 跳过闭合 ```
            blocks.append(("code", lang, "\n".join(body)))
            continue

        if not line.strip():
            i += 1
            continue

        h = re.match(r"^(#{1,6})\s+(.+)$", line)
        if h:
            blocks.append(("h", len(h.group(1)), h.group(2).strip()))
            i += 1
            continue

        if re.match(r"^[\s]*[-*+]\s+", line):
            blocks.append(("ul", re.sub(r"^[\s]*[-*+]\s+", "", line).strip()))
            i += 1
            continue

        if re.match(r"^\s*\d+[\.\)]\s+", line):
            blocks.append(("ol", re.sub(r"^\s*\d+[\.\)]\s+", "", line).strip()))
            i += 1
            continue

        img = re.match(r"^\s*!\[([^\]]*)\]\(([^)]+)\)\s*$", line)
        if img:
            blocks.append(("img", img.group(1).strip(),
                           img.group(2).strip().strip("\"'")))
            i += 1
            continue

        blocks.append(("p", line.strip()))
        i += 1
    return blocks
