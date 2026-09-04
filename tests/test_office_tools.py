"""Office 工具单测：docx_append 迭代写作 + data_analyze 文件直读。"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from litework.tools.office import OfficeTools


def _run(tool: str, args: dict) -> str:
    tools = OfficeTools(os.getcwd())
    return asyncio.run(tools.execute(tool, args))


# ---------------------------------------------------------------- docx_append

def test_docx_append_roundtrip(tmp_path, monkeypatch):
    tools = OfficeTools(str(tmp_path))

    async def call(name, args):
        return await tools.execute(name, args)

    # 1) 先创建
    r1 = asyncio.run(call("docx_create", {
        "content": "# 方案\n\n第一版内容",
        "filename": "方案.docx",
    }))
    assert "方案.docx" in r1

    # 2) 追加章节
    r2 = asyncio.run(call("docx_append", {
        "path": ".outputs/方案.docx",
        "content": "\n# 补充章节\n\n追加的正文段落",
    }))
    assert "已追加内容" in r2

    # 3) 校验：原内容与新内容都在
    from docx import Document
    doc = Document(str(tmp_path / ".outputs" / "方案.docx"))
    texts = [p.text for p in doc.paragraphs]
    joined = "\n".join(texts)
    assert "第一版内容" in joined
    assert "追加的正文段落" in joined
    # 标题段落存在（补充章节渲染为 heading）
    assert any("补充章节" in t for t in texts)


def test_docx_append_page_break_and_errors(tmp_path):
    tools = OfficeTools(str(tmp_path))

    async def call(name, args):
        return await tools.execute(name, args)

    asyncio.run(call("docx_create", {"content": "初始", "filename": "d.docx"}))

    # 分页符参数可用
    r = asyncio.run(call("docx_append", {
        "path": ".outputs/d.docx", "content": "新页内容", "page_break": True,
    }))
    assert "已追加内容" in r

    # 文件不存在
    r2 = asyncio.run(call("docx_append", {"path": ".outputs/nope.docx", "content": "x"}))
    assert "不存在" in r2
    # 非 docx 扩展名
    r3 = asyncio.run(call("docx_append", {"path": ".outputs/x.txt", "content": "x"}))
    assert "仅支持" in r3
    # 空内容
    r4 = asyncio.run(call("docx_append", {"path": ".outputs/d.docx", "content": "  "}))
    assert "为空" in r4
    # 路径越界
    r5 = asyncio.run(call("docx_append", {"path": "../../etc/hosts", "content": "x"}))
    assert "越界" in r5 or "不存在" in r5 or "仅支持" in r5


# ---------------------------------------------------------------- data_analyze 文件直读

def test_data_analyze_xlsx_path(tmp_path):
    # 准备一个 xlsx 文件（模拟用户上传）
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["城市", "销售额"])
    ws.append(["北京", 100])
    ws.append(["上海", 200])
    uploads = tmp_path / ".uploads"
    uploads.mkdir()
    wb.save(str(uploads / "销售.xlsx"))

    tools = OfficeTools(str(tmp_path))

    async def call():
        return await tools.execute("data_analyze", {
            "path": ".uploads/销售.xlsx",
            "instructions": "描述性统计",
        })

    r = asyncio.run(call())
    assert "数据分析结果" in r
    assert "2 行" in r  # 2 行 × 2 列
    assert "城市" in r


def test_data_analyze_csv_path_and_sheet_error(tmp_path):
    csv_path = tmp_path / ".uploads" / "数据.csv"
    csv_path.parent.mkdir()
    csv_path.write_text("名称,数量\n甲,1\n乙,2\n", encoding="utf-8")

    tools = OfficeTools(str(tmp_path))

    async def call(args):
        return await tools.execute("data_analyze", args)

    # CSV 直读
    r = asyncio.run(call({"path": ".uploads/数据.csv", "instructions": "概览"}))
    assert "数据分析结果" in r
    assert "甲" in r or "名称" in r

    # 文件不存在
    r2 = asyncio.run(call({"path": ".uploads/nope.csv", "instructions": "概览"}))
    assert "不存在" in r2
    # 不支持的扩展名
    (tmp_path / ".uploads" / "f.txt").write_text("x", encoding="utf-8")
    r3 = asyncio.run(call({"path": ".uploads/f.txt", "instructions": "概览"}))
    assert "不支持" in r3
    # 路径越界
    r4 = asyncio.run(call({"path": "../../etc/passwd", "instructions": "概览"}))
    assert "越界" in r4

    # data 参数仍然兼容（不带 path）
    r5 = asyncio.run(call({
        "data": "名称,数量\n甲,1\n乙,2\n", "instructions": "概览",
    }))
    assert "数据分析结果" in r5
