"""图表转 Office 测试：docx/pptx 图片嵌入 + 渲染脚本逻辑。

- docx_create 的 Markdown 图片语法 ![alt](path) 应嵌入图片；
- pptx_create 的每页 image 字段应嵌入图片；
- render_diagram.py 的类型识别 / 源码提取逻辑应正确（纯函数，不依赖真实引擎）。
"""
from __future__ import annotations

import importlib.util
import io
import os
import sys
from pathlib import Path

import pytest

from litework.tools.office import OfficeTools

ROOT = Path(__file__).resolve().parents[1]
RENDER_SCRIPT = ROOT / "skills" / "diagram-to-office" / "render_diagram.py"


# ---------------------------------------------------------------- 工具函数

def _make_png(tmp_path: Path, name: str = "pic.png", size=(120, 60)) -> Path:
    """用 matplotlib（项目已依赖）生成一张小 PNG，避免依赖 PIL 手写。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out = tmp_path / name
    fig, ax = plt.subplots(figsize=(size[0] / 100, size[1] / 100), dpi=100)
    ax.set_title("test")
    fig.savefig(out, dpi=100)
    plt.close(fig)
    return out


def _load_render_module():
    if not RENDER_SCRIPT.is_file():
        pytest.skip("render_diagram.py 不存在")
    spec = importlib.util.spec_from_file_location("render_diagram", RENDER_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["render_diagram"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- docx 图片嵌入

def test_docx_create_embeds_markdown_image(tmp_path):
    tools = OfficeTools(str(tmp_path))
    png = _make_png(tmp_path)

    import asyncio

    async def run():
        return await tools.execute("docx_create", {
            "content": "# 架构\n\n![系统架构图](pic.png)\n\n正文",
            "filename": "图文.docx",
        })

    result = asyncio.run(run())
    assert "图文.docx" in result

    from docx import Document
    doc = Document(str(tmp_path / ".outputs" / "图文.docx"))
    assert len(doc.inline_shapes) == 1, "docx 应嵌入 1 张图片"
    # 图注文字应存在
    texts = [p.text for p in doc.paragraphs]
    assert any("系统架构图" in t for t in texts)


def test_docx_create_image_missing_is_graceful(tmp_path):
    tools = OfficeTools(str(tmp_path))

    import asyncio

    async def run():
        return await tools.execute("docx_create", {
            "content": "![不存在](no_such_file.png)",
            "filename": "缺图.docx",
        })

    result = asyncio.run(run())
    assert "缺图.docx" in result

    from docx import Document
    doc = Document(str(tmp_path / ".outputs" / "缺图.docx"))
    texts = "\n".join(p.text for p in doc.paragraphs)
    assert "图片未找到" in texts


# ---------------------------------------------------------------- pptx 图片嵌入

def test_pptx_create_embeds_slide_image(tmp_path):
    tools = OfficeTools(str(tmp_path))
    png = _make_png(tmp_path)

    import asyncio

    async def run():
        return await tools.execute("pptx_create", {
            "slides": '[{"title": "架构图", "image": "pic.png", "bullets": ["要点1"]},'
                      ' {"title": "无图页", "bullets": ["要点A"]}]',
            "filename": "图文.pptx",
        })

    result = asyncio.run(run())
    assert "图文.pptx" in result

    from pptx import Presentation
    prs = Presentation(str(tmp_path / ".outputs" / "图文.pptx"))
    pics = [sh for sh in prs.slides[0].shapes if sh.shape_type == 13]  # PICTURE
    assert len(pics) == 1, "第一页应嵌入 1 张图片"
    # 第二页无图
    assert sum(1 for sh in prs.slides[1].shapes if sh.shape_type == 13) == 0


def test_pptx_create_image_missing_is_graceful(tmp_path):
    tools = OfficeTools(str(tmp_path))

    import asyncio

    async def run():
        return await tools.execute("pptx_create", {
            "slides": '[{"title": "缺图", "image": "nope.png"}]',
            "filename": "缺图.pptx",
        })

    result = asyncio.run(run())
    assert "缺图.pptx" in result


# ---------------------------------------------------------------- 渲染脚本逻辑

def test_render_detect_plantuml():
    mod = _load_render_module()
    assert mod.detect_type("@startuml\nAlice -> Bob: hi\n@enduml", "x.txt", "auto") == "plantuml"
    assert mod.detect_type("anything", "arch.puml", "auto") == "plantuml"
    assert mod.detect_type("x", "y.txt", "plantuml") == "plantuml"


def test_render_detect_mermaid():
    mod = _load_render_module()
    assert mod.detect_type("```mermaid\nflowchart LR\nA-->B\n```", "x.md", "auto") == "mermaid"
    assert mod.detect_type("sequenceDiagram\nA->>B: hi", "flow.mmd", "auto") == "mermaid"
    assert mod.detect_type("classDiagram", "x.txt", "auto") == "mermaid"
    assert mod.detect_type("x", "y.txt", "mermaid") == "mermaid"


def test_render_extract_source():
    mod = _load_render_module()
    # Mermaid 代码块提取
    raw = "前文\n```mermaid\nflowchart LR\nA-->B\n```\n后文"
    assert mod.extract_source(raw, "mermaid") == "flowchart LR\nA-->B"
    # PlantUML 块提取
    raw = "@startuml\nAlice -> Bob\n@enduml"
    assert mod.extract_source(raw, "plantuml") == raw
    # 纯 Mermaid 定义原样保留
    assert mod.extract_source("sequenceDiagram\nA->>B", "mermaid") == "sequenceDiagram\nA->>B"


def test_render_script_cli_error_on_unknown_type(tmp_path):
    """CLI 对无法识别的类型应报错退出（不依赖真实渲染引擎）。"""
    import subprocess

    src = tmp_path / "unknown.txt"
    src.write_text("随便写点没有图的关键字的内容，应该无法识别类型。", encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(RENDER_SCRIPT), str(src), "-o", str(tmp_path / "out.png")],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode != 0
    assert "无法自动识别" in r.stderr
