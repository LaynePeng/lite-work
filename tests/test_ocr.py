# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""OCR 专项测试（社区 ocr-plugin v1.1.0 同步副本的行为验证）。

覆盖三件套：引擎串行锁、页级软截止（部分返回）、ocr_pptx 页数上限。
引擎调用全部 mock（不依赖真实模型），只验证控制流语义。
"""
from __future__ import annotations

import threading
import time
from unittest import mock

import pytest

from litework.tools import ocr
from litework.tools.ocr import OCRTools


def _fake_file(tmp_path, name: str) -> str:
    p = tmp_path / name
    p.write_bytes(b"stub")
    return str(p)


# ------------------------------------------------------------ 引擎串行锁

def test_engine_serialized_by_lock() -> None:
    """并发两线程识别：引擎调用严格串行（同一时刻最多一个进引擎）。"""
    concurrent = 0
    peak = [0]
    lock = threading.Lock()

    def fake_engine(path: str):
        nonlocal concurrent
        with lock:
            concurrent += 1
            peak[0] = max(peak[0], concurrent)
        time.sleep(0.05)  # 放大竞争窗口
        with lock:
            concurrent -= 1
        return [], 0.0  # RapidOCR 返回 (result, elapse) 二元组

    with mock.patch.object(ocr, "_get_engine", return_value=fake_engine):
        t1 = threading.Thread(target=ocr._ocr_image_bytes, args=("a.png",))
        t2 = threading.Thread(target=ocr._ocr_image_bytes, args=("b.png",))
        t1.start(); t2.start()
        t1.join(); t2.join()

    assert peak[0] == 1, "引擎必须串行（并发=1）；并发>1 说明锁没生效"


# ------------------------------------------------------------ PDF

def _write_pdf_stub(monkeypatch: pytest.MonkeyPatch, pages: int) -> None:
    """mock pymupdf.open：返回 N 页假文档，get_pixmap 返回可 save 的假 pix。"""
    class _FakePix:
        def save(self, path: str) -> None:
            with open(path, "wb") as f:
                f.write(b"fake-png")

    class _FakePage:
        def get_pixmap(self, dpi: int = 200) -> "_FakePix":
            return _FakePix()

    fake_doc = mock.Mock()
    fake_doc.__len__ = lambda self: pages
    fake_doc.__getitem__ = lambda self, i: _FakePage()
    monkeypatch.setattr(ocr, "pymupdf", mock.Mock(open=lambda path: fake_doc))
    monkeypatch.setattr(ocr, "_HAS_PYMUPDF", True)
    monkeypatch.setattr(ocr, "_HAS_RAPIDOCR", True)


def test_pdf_soft_deadline_partial_return(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """软截止到点即停：返回已识别页 + [软超时中止] 提示（含续读指引）。"""
    _write_pdf_stub(monkeypatch, 50)
    calls = {"n": 0}

    def fake_ocr_pixmap(pix) -> str:
        calls["n"] += 1
        time.sleep(0.02)
        return f"第 {calls['n']} 页文字"

    pdf = _fake_file(tmp_path, "doc.pdf")
    with mock.patch.object(ocr, "_OCR_SOFT_DEADLINE_S", 0.1), \
         mock.patch.object(ocr, "_ocr_pixmap", side_effect=fake_ocr_pixmap):
        result = OCRTools(str(tmp_path))._ocr_document({"path": pdf, "max_pages": 50})

    assert "[软超时中止]" in result
    assert calls["n"] < 50, "到点后必须停止识别（不能跑完全部页）"
    assert "页文字" in result, "已识别部分照常返回"


def test_pdf_normal_completion_no_deadline_note(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """正常完成（未触截止）：无 [软超时中止] 提示。"""
    _write_pdf_stub(monkeypatch, 3)
    pdf = _fake_file(tmp_path, "doc.pdf")
    with mock.patch.object(ocr, "_ocr_pixmap", return_value="文字"):
        result = OCRTools(str(tmp_path))._ocr_document({"path": pdf, "max_pages": 3})
    assert "[软超时中止]" not in result
    assert "第 3 页" in result


def test_pdf_dpi_150(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """渲染 dpi 降为 150（质量近无损、提速约 40%）。"""
    dpis: list = []

    class _Pix:
        def save(self, path: str) -> None:
            with open(path, "wb") as f:
                f.write(b"x")

    class _Page:
        def get_pixmap(self, dpi: int = 200) -> "_Pix":
            dpis.append(dpi)
            return _Pix()

    fake_doc = mock.Mock()
    fake_doc.__len__ = lambda self: 1
    fake_doc.__getitem__ = lambda self, i: _Page()
    monkeypatch.setattr(ocr, "pymupdf", mock.Mock(open=lambda p: fake_doc))
    monkeypatch.setattr(ocr, "_HAS_PYMUPDF", True)
    monkeypatch.setattr(ocr, "_HAS_RAPIDOCR", True)

    pdf = _fake_file(tmp_path, "doc.pdf")
    with mock.patch.object(ocr, "_ocr_pixmap", return_value="t"):
        OCRTools(str(tmp_path))._ocr_document({"path": pdf})
    assert dpis == [150]


# ------------------------------------------------------------ PPTX

def _write_pptx_stub(monkeypatch: pytest.MonkeyPatch, slide_count: int, images_per_slide: int = 1) -> None:
    class _Shape:
        shape_type = "PICTURE (13)"
        left, top, width, height = 9525, 0, 9525, 9525

        @property
        def image(self):
            class _Img:
                content_type = "image/png"
                blob = b"fake-img"
            return _Img()

    class _Slide:
        shapes = [_Shape() for _ in range(images_per_slide)]

    class _Prs:
        slides = [_Slide() for _ in range(slide_count)]

    import sys
    fake_pptx = mock.Mock()
    fake_pptx.Presentation = lambda path: _Prs()
    monkeypatch.setitem(sys.modules, "pptx", fake_pptx)
    monkeypatch.setattr(ocr, "_HAS_RAPIDOCR", True)


def test_pptx_max_pages_cap(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """页数上限：默认 20，超出的页不识别并提示。"""
    _write_pptx_stub(monkeypatch, slide_count=30)
    recognized: list = []
    pptx = _fake_file(tmp_path, "deck.pptx")

    def fake_ocr(path: str) -> str:
        recognized.append(path)
        return "文字"

    with mock.patch.object(ocr, "_ocr_image_bytes", side_effect=fake_ocr):
        result = OCRTools(str(tmp_path))._ocr_pptx({"path": pptx})

    assert len(recognized) == 20, "默认只识别前 20 页"
    assert "[页数上限]" in result
    assert "30 页" in result


def test_pptx_max_pages_param_respected(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """max_pages 参数生效（clamp 到 100 上限）。"""
    _write_pptx_stub(monkeypatch, slide_count=5)
    pptx = _fake_file(tmp_path, "deck.pptx")
    with mock.patch.object(ocr, "_ocr_image_bytes", return_value="t") as fake:
        OCRTools(str(tmp_path))._ocr_pptx({"path": pptx, "max_pages": 3})
    assert fake.call_count == 3

    # 上限 clamp：max_pages=500 → 100
    _write_pptx_stub(monkeypatch, slide_count=150)
    pptx2 = _fake_file(tmp_path, "deck2.pptx")
    with mock.patch.object(ocr, "_ocr_image_bytes", return_value="t") as fake2:
        OCRTools(str(tmp_path))._ocr_pptx({"path": pptx2, "max_pages": 500})
    assert fake2.call_count == 100


def test_pptx_soft_deadline(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PPT 页循环同样有软截止（到点停止 + 提示）。"""
    _write_pptx_stub(monkeypatch, slide_count=50)
    pptx = _fake_file(tmp_path, "deck.pptx")

    def slow_ocr(path: str) -> str:
        time.sleep(0.02)
        return "t"

    with mock.patch.object(ocr, "_OCR_SOFT_DEADLINE_S", 0.1), \
         mock.patch.object(ocr, "_ocr_image_bytes", side_effect=slow_ocr):
        result = OCRTools(str(tmp_path))._ocr_pptx({"path": pptx, "max_pages": 50})
    assert "[软超时中止]" in result


# ------------------------------------------------------------ 同步治理红线

def test_synced_copy_has_no_apache_header() -> None:
    """社区同步副本不得携带 Apache 头（AGENTS.md §6 许可红线）。"""
    with open("litework/tools/ocr.py", encoding="utf-8") as f:
        head = f.read(400)
    assert "SPDX-License-Identifier: Apache" not in head
    assert "lite-work-plugins" in head, "应保留同步溯源注释"


def test_builtin_wrapper_version_matches_community() -> None:
    """主仓库包装类版本与社区 manifest 一致（1.1.0）。"""
    from litework.tools.plugin import OcrPlugin
    assert OcrPlugin.version == "1.1.0"
