"""办公场景接口测试：产出物列表 / 预览 / 原始文件 / 下载 / 上传。

依赖 office 主依赖（python-docx / openpyxl / python-pptx / reportlab / matplotlib）。
"""
from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from litework.app import AgentApp
from litework.server.app import create_app
from litework.tools.office import OfficeTools


@pytest.fixture
def client_and_workspace(tmp_path):
    ws = str(tmp_path)
    app = AgentApp(workspace=ws, config_dir=str(tmp_path / ".lite-work"))
    fast = create_app(app, token=None)
    with TestClient(fast) as client:
        yield client, ws


def _make_outputs(ws: str) -> None:
    """在工作区 .outputs/ 下生成各类办公文件。"""
    tools = OfficeTools(ws)

    import asyncio

    async def run():
        await tools.execute("docx_create", {
            "content": "# 标题\n\n正文段落\n\n| A | B |\n|---|---|\n| 1 | 2 |",
            "filename": "文档.docx", "title": "测试",
        })
        await tools.execute("xlsx_create", {
            "data": json.dumps([{"姓名": "张三", "分数": 90}]), "filename": "表格.xlsx",
        })
        await tools.execute("pptx_create", {
            "slides": json.dumps([{"title": "背景", "bullets": ["要点一"]}]),
            "filename": "演示.pptx", "title": "汇报",
        })
        await tools.execute("chart_make", {
            "data": json.dumps({"labels": ["A", "B"], "values": [1, 2]}),
            "chart_type": "bar", "filename": "图表.png",
        })
        await tools.execute("pdf_create", {"content": "内容", "filename": "报告.pdf", "title": "报告"})

    asyncio.run(run())


# ---------------------------------------------------------------- /api/outputs

def test_outputs_listing(client_and_workspace):
    client, ws = client_and_workspace
    _make_outputs(ws)
    r = client.get("/api/outputs")
    assert r.status_code == 200
    items = r.json()["items"]
    names = {i["name"] for i in items}
    assert {"文档.docx", "表格.xlsx", "演示.pptx", "图表.png", "报告.pdf"} <= names
    for i in items:
        assert i["source"] == "outputs"
        assert i["path"].startswith(".outputs/")
        assert i["size"] > 0


# ---------------------------------------------------------------- /api/files/preview

def test_preview_image_is_media(client_and_workspace):
    client, ws = client_and_workspace
    _make_outputs(ws)
    r = client.get("/api/files/preview", params={"path": ".outputs/图表.png"})
    assert r.status_code == 200
    data = r.json()
    assert data["kind"] == "media"
    assert data["media_type"] == "image/png"
    assert data["raw_url"].startswith("/api/files/raw?path=")


def test_preview_pdf_is_media(client_and_workspace):
    client, ws = client_and_workspace
    _make_outputs(ws)
    r = client.get("/api/files/preview", params={"path": ".outputs/报告.pdf"})
    assert r.status_code == 200
    assert r.json()["kind"] == "media"
    assert r.json()["media_type"] == "application/pdf"


def test_preview_xlsx_is_table(client_and_workspace):
    client, ws = client_and_workspace
    _make_outputs(ws)
    r = client.get("/api/files/preview", params={"path": ".outputs/表格.xlsx"})
    assert r.status_code == 200
    data = r.json()
    assert data["kind"] == "table"
    assert data["rows"][0] == ["姓名", "分数"]
    assert data["rows"][1] == ["张三", "90"]


def test_preview_docx_is_text(client_and_workspace):
    client, ws = client_and_workspace
    _make_outputs(ws)
    r = client.get("/api/files/preview", params={"path": ".outputs/文档.docx"})
    assert r.status_code == 200
    data = r.json()
    assert data["kind"] == "text"
    assert "标题" in data["text"]
    assert "正文段落" in data["text"]
    assert "[表格 1]" in data["text"]


def test_preview_pptx_is_slides(client_and_workspace):
    client, ws = client_and_workspace
    _make_outputs(ws)
    r = client.get("/api/files/preview", params={"path": ".outputs/演示.pptx"})
    assert r.status_code == 200
    data = r.json()
    assert data["kind"] == "slides"
    titles = [s["title"] for s in data["slides"]]
    assert "背景" in titles


def test_preview_path_escape_blocked(client_and_workspace):
    client, _ = client_and_workspace
    r = client.get("/api/files/preview", params={"path": "../../etc/passwd"})
    assert r.status_code in (403, 404)


# ---------------------------------------------------------------- /api/files/raw 与下载

def test_raw_image_inline(client_and_workspace):
    client, ws = client_and_workspace
    _make_outputs(ws)
    r = client.get("/api/files/raw", params={"path": ".outputs/图表.png"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert len(r.content) > 0


def test_download_docx(client_and_workspace):
    client, ws = client_and_workspace
    _make_outputs(ws)
    r = client.get("/api/files/download", params={"path": ".outputs/文档.docx"})
    assert r.status_code == 200
    assert len(r.content) > 0


# ---------------------------------------------------------------- /api/upload

def test_upload_saves_to_uploads_dir(client_and_workspace):
    client, ws = client_and_workspace
    r = client.post(
        "/api/upload",
        files={"file": ("数据.csv", "城市,销售额\n北京,120\n", "text/csv")},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["path"].startswith(".uploads/")
    assert data["name"] == "数据.csv"
    assert os.path.isfile(os.path.join(ws, ".uploads", "数据.csv"))


def test_upload_same_name_conflict_renamed(client_and_workspace):
    client, ws = client_and_workspace
    for _ in range(2):
        r = client.post(
            "/api/upload",
            files={"file": ("数据.csv", "x\n", "text/csv")},
        )
        assert r.status_code == 200
    names = os.listdir(os.path.join(ws, ".uploads"))
    assert len(names) == 2
