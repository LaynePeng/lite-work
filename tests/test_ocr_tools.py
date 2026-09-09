# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""OCR 工具冒烟测试：真实引擎识别渲染出的文字图片。"""
from __future__ import annotations

import asyncio
import os

import pytest

from litework.tools.ocr import _HAS_RAPIDOCR, OCRTools

pytestmark = pytest.mark.skipif(not _HAS_RAPIDOCR, reason="rapidocr-onnxruntime 未安装")


def _render_text_png(path: str, text: str) -> str:
    """用 matplotlib 渲染一张白底黑字图片（OCR 引擎可识别）。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(4, 1), dpi=150)
    fig.patch.set_facecolor("white")
    fig.text(0.5, 0.5, text, ha="center", va="center", fontsize=22, color="black")
    fig.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path


async def test_ocr_image_recognizes_text(tmp_path):
    png = _render_text_png(str(tmp_path / "hello.png"), "HELLO OCR 123")
    (tmp_path / ".uploads").mkdir(exist_ok=True)
    os.replace(png, str(tmp_path / ".uploads" / "hello.png"))

    tools = OCRTools(str(tmp_path))
    r = await tools.execute("ocr_image", {"path": ".uploads/hello.png"})

    assert "[OCR OK]" in r
    # 大写字母 + 数字易识别（去空格比较，容错小写化）
    text = r.replace(" ", "").upper()
    assert "HELLO" in text and "123" in text, f"识别结果: {r[:300]}"


async def test_ocr_image_errors(tmp_path):
    tools = OCRTools(str(tmp_path))
    # 文件不存在
    r = await tools.execute("ocr_image", {"path": ".uploads/nope.png"})
    assert "不存在" in r
    # 路径越界
    r2 = await tools.execute("ocr_image", {"path": "../../etc/hosts"})
    assert "越界" in r2 or "不存在" in r2
    # 不支持的扩展名
    f = tmp_path / ".uploads" / "f.txt"
    f.parent.mkdir(exist_ok=True)
    f.write_text("x", encoding="utf-8")
    r3 = await tools.execute("ocr_image", {"path": ".uploads/f.txt"})
    assert "不支持" in r3


async def test_ocr_document_pdf(tmp_path):
    """pymupdf 渲染 PDF 页 → OCR（pymupdf 缺失时跳过）。"""
    from litework.tools.ocr import _HAS_PYMUPDF
    if not _HAS_PYMUPDF:
        pytest.skip("pymupdf 未安装")

    import pymupdf

    uploads = tmp_path / ".uploads"
    uploads.mkdir(exist_ok=True)
    pdf_path = str(uploads / "doc.pdf")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "PDF OCR 456", fontsize=24)
    doc.save(pdf_path)
    doc.close()

    tools = OCRTools(str(tmp_path))
    r = await tools.execute("ocr_document", {"path": ".uploads/doc.pdf", "max_pages": 1})
    assert "第 1 页" in r
    text = r.replace(" ", "").upper()
    assert "PDF" in text and "456" in text, f"识别结果: {r[:300]}"
