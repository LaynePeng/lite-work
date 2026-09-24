# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors
#
"""核心插件无关性门禁（CI 守住"核心不认识具体插件"这条纪律）。

背景：同一类错误在本仓库发生过三次——把插件专属知识写进核心：
1. 把插件的斜杠命令硬编码进 `core/commands.py` 的内置命令表；
2. 在前端 `ChatView` 里直接 `import { <PluginCard> }`；
3. 在事件负载、TS 类型与 UI 标题里写死插件名。

正解是**通用协议**：核心只认中立 schema，插件名由插件自报。
- `contributes.settings` / `contributes.panels` / `contributes.commands`（插件声明）
- `approval:request.judge_opinion`（判定意见，含插件自报的 `source`）
- 面板 JSON `{"type": "lite-tree", ...}`（通用决策树）

本门禁扫描 `litework/` 与 `web/src/` 的**代码与文档字符串**，断言不含社区插件名
（当前为 `jev`）。**注释放行**——注释里解释历史与协议是有价值的文档。

> 注：本仓库内置插件（office / ocr / pricing / collab-*）是主仓库自带代码，不在本门禁范围。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 社区插件名（不得出现在核心代码里）；新增社区插件时可追加
FORBIDDEN = ("jev",)

#: 允许出现的通用协议标识（存在性由本文件同时守住）
REQUIRED_PROTOCOLS = (
    "judge_opinion",    # 审批意见（中立字段名）
    "lite-tree",        # 面板决策树的通用 schema type
    "contributes",      # 插件声明式贡献（设置/面板/命令）
)


def _iter_py_files() -> list[Path]:
    return [p for p in (REPO_ROOT / "litework").rglob("*.py")
            if "__pycache__" not in str(p) and not p.name.startswith("test_")]


def _iter_web_files() -> list[Path]:
    out = []
    for ext in ("*.ts", "*.tsx", "*.css"):
        # 排除测试文件：夹具会合理引用插件名（门禁只守运行时核心）
        out += [p for p in (REPO_ROOT / "web" / "src").rglob(ext)
                if ".test." not in p.name]
    return out


def _py_code_and_docstrings(path: Path) -> str:
    """返回 Python 文件里**代码标识符 + 文档字符串**的拼接（不含注释）。

    实现：用 ast 收集标识符、非 docstring 字符串常量与 docstring 文本。
    """
    src = path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    docstrings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)

    parts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            parts.append(node.id)
        elif isinstance(node, ast.Attribute):
            parts.append(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            parts.append(node.name)
        elif isinstance(node, ast.arg):
            parts.append(node.arg)
        elif isinstance(node, ast.keyword):
            parts.append(node.arg or "")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            parts.append(node.value)          # 含 docstring（下面统一检查）
    parts.extend(docstrings)
    return "\n".join(parts)


def _ts_code_without_comments(path: Path) -> str:
    """返回 TS/TSX/CSS 去掉注释后的文本（`//` 行注释、`/* */` 块注释）。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"^\s*//.*$", " ", text, flags=re.M)
    text = re.sub(r"\s+//[^\n]*", " ", text)   # 行尾注释
    return text


def _scan() -> list[tuple[str, str]]:
    """返回 [(相对路径, 命中的禁用词)]。"""
    hits: list[tuple[str, str]] = []
    for path in _iter_py_files():
        blob = _py_code_and_docstrings(path)
        low = blob.lower()
        for word in FORBIDDEN:
            if word in low:
                hits.append((str(path.relative_to(REPO_ROOT)), word))
    for path in _iter_web_files():
        blob = _ts_code_without_comments(path)
        low = blob.lower()
        for word in FORBIDDEN:
            if word in low:
                hits.append((str(path.relative_to(REPO_ROOT)), word))
    return hits


def test_core_has_no_community_plugin_names():
    """核心代码 / 文档字符串里不得出现社区插件名（注释放行）。"""
    hits = _scan()
    assert not hits, (
        "核心引用了社区插件名（应改为通用协议：contributes.* / judge_opinion / lite-tree）：\n"
        + "\n".join(f"  {p}  ← 命中 {w!r}" for p, w in hits)
    )


@pytest.mark.parametrize("proto", REQUIRED_PROTOCOLS)
def test_generic_protocols_still_exist(proto: str):
    """通用协议本身必须存在（防止"为了通过门禁"而把协议也删掉）。"""
    found = False
    for path in _iter_py_files() + _iter_web_files():
        blob = (
            _py_code_and_docstrings(path) if path.suffix == ".py"
            else _ts_code_without_comments(path)
        )
        if proto in blob:
            found = True
            break
    assert found, f"通用协议 {proto!r} 在核心中找不到——不要为了通过门禁而删除协议"
