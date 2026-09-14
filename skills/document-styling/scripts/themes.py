#!/usr/bin/env python3
"""主题库：加载 / 列举 / 预览主题。

主题文件是扁平的 ``key: value``（见 ``themes/_schema.md``），不依赖 YAML。
三类来源（后者同名时优先靠前的搜索顺序）：

1. 内置预设   ``<技能目录>/themes/*.md``（随仓库分发，版权自持）
2. 用户自定义 ``~/.lite-work/themes/*.md``
3. 社区主题   ``~/.agents/skills/theme-factory/themes/*.md``（**只读**本机已装文件，
   做尽力适配；我们不再分发其内容）

用法::

    python3 themes.py list
    python3 themes.py show business-blue
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THEMES_DIR = os.path.join(SKILL_DIR, "themes")
USER_THEMES_DIR = os.path.expanduser("~/.lite-work/themes")
COMMUNITY_THEMES_DIR = os.path.expanduser("~/.agents/skills/theme-factory/themes")

DEFAULT = {
    "name": "default",
    "label": "默认",
    "description": "内置默认样式",
    "color_primary": "#1F4E79",
    "color_accent": "#2E75B6",
    "color_text": "#1A1A1A",
    "color_muted": "#6B7280",
    "color_table_head": "#1F4E79",
    "color_table_band": "#EEF3F9",
    "color_code_bg": "#F5F5F5",
    "font_zh": "宋体",
    "font_en": "Times New Roman",
    "font_heading_zh": "微软雅黑",
    "font_heading_en": "Calibri",
    "font_mono": "Courier New",
    "size_body": 12,
    "size_h1": 22,
    "size_h2": 16,
    "size_h3": 14,
    "size_small": 10.5,
    "line_spacing": 1.5,
    "para_space": 6,
    "margin_mm": 25.4,
}
_NUMERIC = {"size_body", "size_h1", "size_h2", "size_h3", "size_small",
            "line_spacing", "para_space", "margin_mm"}


def _parse_kv(path: str) -> dict:
    data = {}
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line[0] in "#-|":
                    continue
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and v:
                    data[k] = v
    except OSError:
        return {}
    return _coerce(data)


def _coerce(data: dict) -> dict:
    for k in list(data):
        if k in _NUMERIC:
            try:
                f = float(data[k])
                data[k] = int(f) if f == int(f) else f
            except (TypeError, ValueError):
                data.pop(k)
    return data


def _adapt_community(path: str):
    """尽力把 theme-factory 主题适配成我们的字段（只读本机文件）。"""
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return None
    hexes = re.findall(r"#[0-9a-fA-F]{6}", text)
    if not hexes:
        return None
    base = os.path.splitext(os.path.basename(path))[0]
    label = base
    m = re.search(r"^#{1,2}\s+(.+)$", text, re.M)
    if m:
        label = m.group(1).strip()
    data = dict(DEFAULT)
    data.update({
        "name": "tf-" + base,
        "label": label + "（社区）",
        "description": "来自本机已安装的 theme-factory 主题",
        "color_primary": hexes[0],
        "color_accent": hexes[1] if len(hexes) > 1 else hexes[0],
        "color_table_head": hexes[0],
        "color_table_band": hexes[-1],
    })
    fm = re.search(r"(?:Heading|Headings|标题)[^\n:：]*[:：]\s*([^\n]+)", text)
    if fm:
        data["font_heading_zh"] = data["font_heading_en"] = fm.group(1).strip()
    bm = re.search(r"(?:Body|正文)[^\n:：]*[:：]\s*([^\n]+)", text)
    if bm:
        data["font_zh"] = data["font_en"] = bm.group(1).strip()
    return data


def _search_paths(name: str):
    yield os.path.join(THEMES_DIR, name + ".md")
    yield os.path.join(USER_THEMES_DIR, name + ".md")
    yield os.path.join(COMMUNITY_THEMES_DIR, name + ".md")


def load(name: str) -> dict:
    """按名加载主题，缺字段用默认补齐；找不到时回退默认主题。"""
    for path in _search_paths(name):
        if not os.path.isfile(path):
            continue
        data = _parse_kv(path)
        if data:
            merged = dict(DEFAULT)
            merged.update(data)
            merged["name"] = name
            merged.setdefault("label", name)
            return merged
    d = dict(DEFAULT)
    d["name"] = name
    return d


def list_themes():
    """返回 [(name, label, description, source), ...]。"""
    out, seen = [], set()
    for d, src in ((THEMES_DIR, "builtin"),
                   (USER_THEMES_DIR, "user"),
                   (COMMUNITY_THEMES_DIR, "community")):
        if not os.path.isdir(d):
            continue
        for path in sorted(glob.glob(os.path.join(d, "*.md"))):
            base = os.path.basename(path)
            if base.startswith("_"):
                continue
            data = _adapt_community(path) if src == "community" else _parse_kv(path)
            if not data:
                continue
            name = data.get("name") or os.path.splitext(base)[0]
            if name in seen:
                continue
            seen.add(name)
            out.append((name, data.get("label", name),
                        data.get("description", ""), src))
    return out


def _cli(argv) -> int:
    if not argv or argv[0] in ("list", "ls"):
        rows = list_themes()
        if not rows:
            print("（未发现主题）")
        for name, label, desc, src in rows:
            print("%-20s %-16s [%-9s] %s" % (name, label, src, desc))
        return 0
    if argv[0] in ("show", "get") and len(argv) > 1:
        print(json.dumps(load(argv[1]), ensure_ascii=False, indent=2))
        return 0
    print("用法: themes.py list | themes.py show <name>")
    return 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
