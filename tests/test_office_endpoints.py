# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""办公场景接口测试：产出物列表 / 预览 / 原始文件 / 下载 / 上传。

依赖 office 主依赖（python-docx / openpyxl / python-pptx / reportlab / matplotlib）。
"""
from __future__ import annotations

import json
import os
import shutil

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
    """在工作区 产出物/ 下生成各类办公文件。"""
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
    body = r.json()
    groups = body["groups"]
    # 平铺写入 产出物/ 根目录 → 归入「未分类」一组
    assert len(groups) == 1
    assert groups[0]["name"] == "未分类"
    assert groups[0]["source"] == "outputs"
    items = groups[0]["items"]
    names = {i["name"] for i in items}
    assert {"文档.docx", "表格.xlsx", "演示.pptx", "图表.png", "报告.pdf"} <= names
    assert body["total"] == len(items)
    for i in items:
        assert i["source"] == "outputs"
        assert i["path"].startswith("产出物/")
        assert i["size"] > 0
        assert "category" in i and "ext" in i


def test_outputs_inbox_groups_by_dir_and_keeps_latest_version(client_and_workspace):
    """收件箱：按类型目录分组、只留最新版本、跳过 归档/中间产物/构建产物。"""
    client, ws = client_and_workspace
    base = os.path.join(ws, "产出物")
    os.makedirs(os.path.join(base, "图表", "中间产物"), exist_ok=True)
    os.makedirs(os.path.join(base, "图表", "归档"), exist_ok=True)
    os.makedirs(os.path.join(base, "演示"), exist_ok=True)
    # 图表：同一交付物两个版本 + 一份 LaTeX 中间产物 + 归档旧版
    _write(os.path.join(base, "图表", "系统框图_v1.png"), b"a")
    _write(os.path.join(base, "图表", "系统框图_v2.png"), b"bb")
    _write(os.path.join(base, "图表", "系统框图_v2.aux"), b"x")
    _write(os.path.join(base, "图表", "中间产物", "tmp.png"), b"x")
    _write(os.path.join(base, "图表", "归档", "系统框图_v0.png"), b"x")
    _write(os.path.join(base, "演示", "简报_v3.pptx"), b"ppt")

    body = client.get("/api/outputs").json()
    by_group = {g["name"]: g for g in body["groups"]}
    assert set(by_group) == {"图表", "演示"}
    # 只留 v2（v1 不出现），.aux 与 归档/中间产物 均不出现
    chart_names = [i["name"] for i in by_group["图表"]["items"]]
    assert chart_names == ["系统框图_v2.png"]
    assert by_group["图表"]["items"][0]["version"] == 2.0
    assert [i["name"] for i in by_group["演示"]["items"]] == ["简报_v3.pptx"]


def test_outputs_skips_office_lock_files(client_and_workspace):
    """Office 打开文档时产生的 `~$xxx.docx` 锁文件不是交付物，不应混进收件箱。

    这类文件带交付物扩展名（.docx/.xlsx…），若不按前缀过滤会被当成一份独立产出物
    （版本号解析后基名为 `~$xxx`，与真实文件并列出现）。
    """
    client, ws = client_and_workspace
    base = os.path.join(ws, "产出物")
    _write(os.path.join(base, "交底书_v9.docx"), b"doc")
    _write(os.path.join(base, "~$交底书_v9.docx"), b"lock")
    _write(os.path.join(base, "报告", "报告_v2.xlsx"), b"xlsx")
    _write(os.path.join(base, "报告", "~$报告_v2.xlsx"), b"lock")

    body = client.get("/api/outputs").json()
    names = {i["name"] for g in body["groups"] for i in g["items"]}
    assert names == {"交底书_v9.docx", "报告_v2.xlsx"}
    assert body["total"] == 2
    assert all(not i["name"].startswith("~$") for g in body["groups"] for i in g["items"])


def _write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


# ---------------------------------------------------------------- /api/files/preview

def test_preview_image_is_media(client_and_workspace):
    client, ws = client_and_workspace
    _make_outputs(ws)
    r = client.get("/api/files/preview", params={"path": "产出物/图表.png"})
    assert r.status_code == 200
    data = r.json()
    assert data["kind"] == "media"
    assert data["media_type"] == "image/png"
    assert data["raw_url"].startswith("/api/files/raw?path=")


def test_preview_pdf_is_media(client_and_workspace):
    client, ws = client_and_workspace
    _make_outputs(ws)
    r = client.get("/api/files/preview", params={"path": "产出物/报告.pdf"})
    assert r.status_code == 200
    assert r.json()["kind"] == "media"
    assert r.json()["media_type"] == "application/pdf"


def test_preview_xlsx_is_table(client_and_workspace):
    client, ws = client_and_workspace
    _make_outputs(ws)
    r = client.get("/api/files/preview", params={"path": "产出物/表格.xlsx"})
    assert r.status_code == 200
    data = r.json()
    assert data["kind"] == "table"
    assert data["rows"][0] == ["姓名", "分数"]
    assert data["rows"][1] == ["张三", "90"]


def test_preview_docx_is_text(client_and_workspace):
    client, ws = client_and_workspace
    _make_outputs(ws)
    r = client.get("/api/files/preview", params={"path": "产出物/文档.docx"})
    assert r.status_code == 200
    data = r.json()
    assert data["kind"] == "text"
    assert "标题" in data["text"]
    assert "正文段落" in data["text"]
    assert "[表格 1]" in data["text"]


def test_preview_pptx_is_slides(client_and_workspace):
    client, ws = client_and_workspace
    _make_outputs(ws)
    r = client.get("/api/files/preview", params={"path": "产出物/演示.pptx"})
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
    r = client.get("/api/files/raw", params={"path": "产出物/图表.png"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert len(r.content) > 0


def test_download_docx(client_and_workspace):
    client, ws = client_and_workspace
    _make_outputs(ws)
    r = client.get("/api/files/download", params={"path": "产出物/文档.docx"})
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
    assert data["path"].startswith("素材/")
    assert data["name"] == "数据.csv"
    assert os.path.isfile(os.path.join(ws, "素材", "数据.csv"))


def test_upload_same_name_conflict_renamed(client_and_workspace):
    client, ws = client_and_workspace
    for _ in range(2):
        r = client.post(
            "/api/upload",
            files={"file": ("数据.csv", "x\n", "text/csv")},
        )
        assert r.status_code == 200
    names = os.listdir(os.path.join(ws, "素材"))
    assert len(names) == 2


# ---------------------------------------------------------------- 产出物 ZIP / 清理

def test_outputs_zip_download(client_and_workspace):
    client, ws = client_and_workspace
    _make_outputs(ws)
    # 上传一个素材，验证 include_uploads 参数
    client.post("/api/upload", files={"file": ("素材.csv", "a,b\n1,2\n", "text/csv")})

    r = client.get("/api/outputs/zip")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    import io
    import zipfile
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    # 仅 产出物（5 个产出物），不含 uploads
    assert any(n.startswith("产出物/") for n in names)
    assert not any(n.startswith("素材/") for n in names)
    assert len([n for n in names if n.startswith("产出物/")]) == 5

    # include_uploads=True 时包含素材
    r2 = client.get("/api/outputs/zip", params={"include_uploads": "true"})
    assert r2.status_code == 200
    zf2 = zipfile.ZipFile(io.BytesIO(r2.content))
    assert any(n.startswith("素材/素材.csv") for n in zf2.namelist())


def test_outputs_zip_empty_404(client_and_workspace):
    client, _ = client_and_workspace
    r = client.get("/api/outputs/zip")
    assert r.status_code == 404


def test_clear_outputs(client_and_workspace):
    client, ws = client_and_workspace
    _make_outputs(ws)
    client.post("/api/upload", files={"file": ("素材.csv", "x\n", "text/csv")})

    # 默认 scope=outputs：只清 产出物
    r = client.delete("/api/outputs")
    assert r.status_code == 200
    assert r.json()["deleted"] == 5
    assert os.listdir(os.path.join(ws, "产出物")) == []
    assert os.path.isfile(os.path.join(ws, "素材", "素材.csv"))

    # scope=all：连同 uploads 一起清
    r2 = client.delete("/api/outputs", params={"scope": "all"})
    assert r2.status_code == 200
    assert r2.json()["deleted"] == 1
    assert os.listdir(os.path.join(ws, "素材")) == []

    # 非法 scope
    r3 = client.delete("/api/outputs", params={"scope": "hack"})
    assert r3.status_code == 400


def test_delete_single_output_file(client_and_workspace):
    client, ws = client_and_workspace
    _make_outputs(ws)

    r = client.delete("/api/files", params={"path": "产出物/图表.png"})
    assert r.status_code == 200
    assert not os.path.exists(os.path.join(ws, "产出物", "图表.png"))

    # 文件页签：嵌套目录下的源码文件同样可删（旧「仅限 产出物/素材」限制已放开）
    nested = os.path.join(ws, "src", "core")
    os.makedirs(nested)
    with open(os.path.join(nested, "main.py"), "w", encoding="utf-8") as f:
        f.write("print('hi')\n")
    r2 = client.delete("/api/files", params={"path": "src/core/main.py"})
    assert r2.status_code == 200
    assert not os.path.exists(os.path.join(nested, "main.py"))

    # 越界仍被拒绝
    r3 = client.delete("/api/files", params={"path": "../../etc/passwd"})
    assert r3.status_code in (403, 404)

    # 不存在的文件
    r4 = client.delete("/api/files", params={"path": "产出物/nope.png"})
    assert r4.status_code == 404


def test_delete_dir_and_git_internal_rejected(client_and_workspace):
    """底线不放开的项：目录不可删、.git 内部文件不可删。"""
    client, ws = client_and_workspace
    os.makedirs(os.path.join(ws, "src", "pkg"))

    r = client.delete("/api/files", params={"path": "src/pkg"})
    assert r.status_code == 400
    assert os.path.isdir(os.path.join(ws, "src", "pkg"))

    os.makedirs(os.path.join(ws, ".git"))
    with open(os.path.join(ws, ".git", "config"), "w", encoding="utf-8") as f:
        f.write("[core]\n")
    r2 = client.delete("/api/files", params={"path": ".git/config"})
    assert r2.status_code == 403
    assert os.path.isfile(os.path.join(ws, ".git", "config"))


# ---------------------------------------------------------------- /api/files/rename

def test_rename_output_file(client_and_workspace):
    client, ws = client_and_workspace
    _make_outputs(ws)
    r = client.post("/api/files/rename", json={"path": "产出物/图表.png", "new_name": "新图表.png"})
    assert r.status_code == 200
    assert r.json()["path"] == "产出物/新图表.png"
    assert r.json()["name"] == "新图表.png"
    assert os.path.isfile(os.path.join(ws, "产出物", "新图表.png"))
    assert not os.path.exists(os.path.join(ws, "产出物", "图表.png"))


def test_rename_upload_file(client_and_workspace):
    client, ws = client_and_workspace
    client.post("/api/upload", files={"file": ("数据.csv", "a,b\n1,2\n", "text/csv")})
    r = client.post("/api/files/rename", json={"path": "素材/数据.csv", "new_name": "新数据.csv"})
    assert r.status_code == 200
    assert os.path.isfile(os.path.join(ws, "素材", "新数据.csv"))
    assert not os.path.exists(os.path.join(ws, "素材", "数据.csv"))


def test_rename_same_name_is_noop(client_and_workspace):
    client, ws = client_and_workspace
    _make_outputs(ws)
    r = client.post("/api/files/rename", json={"path": "产出物/图表.png", "new_name": "图表.png"})
    assert r.status_code == 200
    assert os.path.isfile(os.path.join(ws, "产出物", "图表.png"))


def test_rename_extension_change_rejected(client_and_workspace):
    client, ws = client_and_workspace
    _make_outputs(ws)
    r = client.post("/api/files/rename", json={"path": "产出物/图表.png", "new_name": "图表.txt"})
    assert r.status_code == 400
    # 原文件安然无恙
    assert os.path.isfile(os.path.join(ws, "产出物", "图表.png"))


def test_rename_conflict_409(client_and_workspace):
    client, ws = client_and_workspace
    client.post("/api/upload", files={"file": ("a.csv", "x\n", "text/csv")})
    client.post("/api/upload", files={"file": ("b.csv", "y\n", "text/csv")})
    r = client.post("/api/files/rename", json={"path": "素材/a.csv", "new_name": "b.csv"})
    assert r.status_code == 409


def test_rename_invalid_name(client_and_workspace):
    client, ws = client_and_workspace
    _make_outputs(ws)
    r = client.post("/api/files/rename", json={"path": "产出物/图表.png", "new_name": ""})
    assert r.status_code == 400
    r2 = client.post("/api/files/rename", json={"path": "产出物/图表.png", "new_name": "a/b.png"})
    assert r2.status_code == 400


def test_rename_workspace_source_file(client_and_workspace):
    """文件页签：嵌套目录下的源码文件可重命名（旧「仅限 产出物/素材」限制已放开）。"""
    client, ws = client_and_workspace
    os.makedirs(os.path.join(ws, "src"))
    with open(os.path.join(ws, "src", "main.py"), "w", encoding="utf-8") as f:
        f.write("print('hi')\n")

    r = client.post("/api/files/rename", json={"path": "src/main.py", "new_name": "app.py"})
    assert r.status_code == 200
    assert r.json()["path"] == "src/app.py"
    assert os.path.isfile(os.path.join(ws, "src", "app.py"))
    assert not os.path.exists(os.path.join(ws, "src", "main.py"))

    # 越界仍被拒绝
    r2 = client.post("/api/files/rename", json={"path": "../../etc/passwd", "new_name": "passwd"})
    assert r2.status_code in (403, 404)


def test_rename_git_internal_rejected(client_and_workspace):
    client, ws = client_and_workspace
    os.makedirs(os.path.join(ws, ".git"))
    with open(os.path.join(ws, ".git", "config"), "w", encoding="utf-8") as f:
        f.write("[core]\n")
    r = client.post("/api/files/rename", json={"path": ".git/config", "new_name": "config2"})
    assert r.status_code == 403
    assert os.path.isfile(os.path.join(ws, ".git", "config"))


def test_rename_missing_file_404(client_and_workspace):
    client, _ = client_and_workspace
    r = client.post("/api/files/rename", json={"path": "产出物/nope.png", "new_name": "x.png"})
    assert r.status_code == 404


# ---------------------------------------------------------------- 新建项目

def test_create_project_with_git(client_and_workspace):
    client, ws = client_and_workspace
    r = client.post("/api/projects/create", json={
        "parent": ws, "name": "新项目", "git": True,
    })
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    target = os.path.join(ws, "新项目")
    assert os.path.isdir(target)
    if shutil.which("git"):
        assert data["git_initialized"] is True
        assert os.path.isdir(os.path.join(target, ".git"))


def test_create_project_without_git(client_and_workspace):
    client, ws = client_and_workspace
    r = client.post("/api/projects/create", json={
        "parent": ws, "name": "裸目录", "git": False,
    })
    assert r.status_code == 200
    assert r.json()["git_initialized"] is False
    assert not os.path.exists(os.path.join(ws, "裸目录", ".git"))


def test_create_project_duplicate_and_invalid(client_and_workspace):
    client, ws = client_and_workspace
    os.makedirs(os.path.join(ws, "已存在"))
    # 目录已存在
    r = client.post("/api/projects/create", json={"parent": ws, "name": "已存在"})
    assert r.status_code == 409
    # 非法名称
    r2 = client.post("/api/projects/create", json={"parent": ws, "name": "带 空格"})
    assert r2.status_code == 400
    # 父目录不存在
    r3 = client.post("/api/projects/create", json={"parent": "/no/such/dir", "name": "x"})
    assert r3.status_code == 400
