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


# ---------------------------------------------------------------- 工具执行不阻塞事件循环（回归守护）

def test_office_execute_does_not_block_event_loop(tmp_path):
    """office 工具为同步重活（matplotlib/subprocess），必须放线程池——
    直接同步调用会阻塞 asyncio 事件循环，曾导致 SSE/审批全部冻结
    （用户侧表现为应用完全卡死）。此测试为该回归的守护。
    """
    import asyncio
    import time

    tools = OfficeTools(str(tmp_path))
    ticks = []

    async def heartbeat():
        # 事件循环心跳：每 50ms 记录一次，工具执行期间必须持续跳动
        for _ in range(20):
            ticks.append(time.monotonic())
            await asyncio.sleep(0.05)

    async def run_chart():
        return await tools.execute("chart_make", {
            "data": '{"labels": ["A", "B"], "values": [1, 2]}',
            "chart_type": "bar",
            "filename": "hb.png",
        })

    async def main():
        hb = asyncio.create_task(heartbeat())
        await run_chart()
        await hb

    asyncio.run(main())

    # 心跳间隔应基本均匀（最大间隔 < 1s 即视为未阻塞；matplotlib 出图
    # 本身耗时无妨——它在别的线程，事件循环必须保持响应）
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert gaps, "心跳未运行"
    assert max(gaps) < 1.0, f"事件循环被阻塞：心跳间隔 {max(gaps):.2f}s"
    assert (tmp_path / ".outputs" / "hb.png").exists()


# ---------------------------------------------------------------- 图表代码块自动渲染（工具层兜底）

def test_docx_plantuml_block_auto_render_or_keep(tmp_path):
    """content 中的 ```plantuml 代码块：有引擎→渲染成图嵌入；无引擎→保留源码文本（不丢）。"""
    tools = OfficeTools(str(tmp_path))

    async def call(name, args):
        return await tools.execute(name, args)

    content = "# 架构说明\n\n```plantuml\n@startuml\nAlice -> Bob: hi\n@enduml\n```\n\n正文结束"
    r = asyncio.run(call("docx_create", {"content": content, "filename": "带图.docx"}))
    assert "带图.docx" in r

    from docx import Document
    doc = Document(str(tmp_path / ".outputs" / "带图.docx"))
    # 无论渲染成功与否，源码文本或图片至少保留其一：
    # - 渲染成功：inline_shapes >= 1
    # - 无引擎回退：段落里保留 @startuml 源码
    all_text = "\n".join(p.text for p in doc.paragraphs)
    assert (len(doc.inline_shapes) >= 1) or ("@startuml" in all_text), \
        "plantuml 代码块既没渲染成图也没保留源码——内容丢失！"

    # diagrams 缓存目录只在渲染成功时存在
    if (tmp_path / ".outputs" / "diagrams").is_dir():
        pngs = list((tmp_path / ".outputs" / "diagrams").glob("*.png"))
        assert pngs, "diagrams 目录存在但没有 PNG"


def test_docx_mermaid_block_kept_when_render_fails(tmp_path, monkeypatch):
    """渲染失败（引擎缺失/语法错）时必须回退保留源码文本——内容不丢是硬约束。"""
    tools = OfficeTools(str(tmp_path))

    # 强制渲染失败：monkeypatch _render_diagram_block 返回 None
    monkeypatch.setattr(
        OfficeTools, "_render_diagram_block",
        lambda self, lang, code, seq: None,
    )

    async def call(name, args):
        return await tools.execute(name, args)

    content = "```mermaid\nflowchart LR\nA-->B\n```\n"
    r = asyncio.run(call("docx_create", {"content": content, "filename": "兜底.docx"}))
    assert "兜底.docx" in r

    from docx import Document
    doc = Document(str(tmp_path / ".outputs" / "兜底.docx"))
    all_text = "\n".join(p.text for p in doc.paragraphs)
    assert "flowchart LR" in all_text, "渲染失败时源码文本被丢弃了！"
    assert "A-->B" in all_text


def test_docx_normal_code_block_unaffected(tmp_path):
    """普通代码块（python 等）行为不变：仍作为等宽文本渲染。"""
    tools = OfficeTools(str(tmp_path))

    async def call():
        return await tools.execute("docx_create", {
            "content": "```python\nprint('hi')\n```",
            "filename": "代码.docx",
        })

    asyncio.run(call())
    from docx import Document
    doc = Document(str(tmp_path / ".outputs" / "代码.docx"))
    all_text = "\n".join(p.text for p in doc.paragraphs)
    assert "print('hi')" in all_text
    assert len(doc.inline_shapes) == 0  # 普通代码块不应被渲染成图


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
