# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""办公场景：文件上传 / 产出物下载与预览（AGI 通用入口）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from ...tools.office import OUTPUT_DIR_NAME, UPLOADS_DIR_NAME
from .context import ServerContext


def _guess_media_type(target: str) -> str:
    import mimetypes
    mt, _ = mimetypes.guess_type(target)
    return mt or "application/octet-stream"


def create_router(ctx: ServerContext) -> APIRouter:
    router = APIRouter()
    app = ctx.app

    @router.post("/api/upload")
    async def upload_file(request: Request):
        """接收 multipart 文件上传，保存到工作区 素材/ 目录。

        办公场景入口：用户把 CSV/Excel/文档等素材丢给 Agent 处理。
        返回 {path: 工作区相对路径, size, name}，Agent 可直接用 read_file /
        data_analyze 读取。
        """
        if request:
            ctx.check_auth(request)
        import os as _os

        workspace = ctx.require_workspace()
        try:
            form = await request.form()
        except Exception:
            raise HTTPException(status_code=400, detail="无效的表单请求")
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(status_code=400, detail="缺少文件字段 file")

        filename = _os.path.basename(getattr(upload, "filename", "") or "upload.bin")
        # 清理文件名中的路径成分与危险字符
        filename = "".join(c for c in filename if c not in '\\/:*?"<>|').strip() or "upload.bin"
        size_limit = 50 * 1024 * 1024  # 50MB
        data = await upload.read()
        if len(data) > size_limit:
            raise HTTPException(status_code=413, detail="文件超过 50MB 上传限制")
        if not data:
            raise HTTPException(status_code=400, detail="空文件")

        uploads_dir = _os.path.join(workspace, UPLOADS_DIR_NAME)
        _os.makedirs(uploads_dir, exist_ok=True)
        # 同名冲突：追加序号
        base, ext = _os.path.splitext(filename)
        target = _os.path.join(uploads_dir, filename)
        n = 1
        while _os.path.exists(target):
            target = _os.path.join(uploads_dir, f"{base}_{n}{ext}")
            n += 1
        with open(target, "wb") as f:
            f.write(data)

        rel_path = _os.path.relpath(target, workspace).replace("\\", "/")
        return {"path": rel_path, "name": _os.path.basename(target), "size": len(data)}

    @router.get("/api/files/download")
    async def download_file(path: str, request: Request = None):
        """下载工作区内的文件（用于办公产出物：docx/xlsx/pptx/pdf/图片）。"""
        if request:
            ctx.check_auth(request)
        import os as _os

        workspace = ctx.require_workspace()
        target = _os.path.abspath(_os.path.join(workspace, path))
        if not (target == workspace or target.startswith(workspace + _os.path.sep)):
            raise HTTPException(status_code=403, detail="路径越界")
        if not _os.path.isfile(target):
            raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
        filename = _os.path.basename(target)
        return FileResponse(
            target,
            filename=filename,
            content_disposition_type="attachment",
            headers={"Access-Control-Expose-Headers": "Content-Disposition"},
        )

    @router.get("/api/outputs")
    async def list_outputs(request: Request = None):
        """列出工作区 产出物/（Agent 交付物）与 素材/（用户上传素材）下的文件。

        供侧边栏「产出物」Tab 展示：文件名/相对路径/大小/修改时间/来源。
        """
        if request:
            ctx.check_auth(request)
        import os as _os
        from datetime import datetime

        workspace = ctx.require_workspace()
        items = []
        for source, rel_dir in (("outputs", OUTPUT_DIR_NAME), ("uploads", UPLOADS_DIR_NAME)):
            base = _os.path.join(workspace, rel_dir)
            if not _os.path.isdir(base):
                continue
            try:
                entries = sorted(_os.listdir(base))
            except OSError:
                continue
            for name in entries:
                full = _os.path.join(base, name)
                if not _os.path.isfile(full):
                    continue
                try:
                    stat = _os.stat(full)
                except OSError:
                    continue
                items.append({
                    "name": name,
                    "path": f"{rel_dir}/{name}".replace("\\", "/"),
                    "source": source,
                    "size": stat.st_size,
                    "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                })
        # 新产出的排前面
        items.sort(key=lambda x: x["mtime"], reverse=True)
        return {"items": items}

    @router.get("/api/outputs/zip")
    async def download_outputs_zip(include_uploads: bool = False, request: Request = None):
        """把 产出物/（可选含 素材/）打包为 zip 一键下载。"""
        if request:
            ctx.check_auth(request)
        import io as _io
        import os as _os
        import zipfile as _zipfile

        workspace = ctx.require_workspace()
        sources = [OUTPUT_DIR_NAME] + ([UPLOADS_DIR_NAME] if include_uploads else [])
        # 只收顶层常规文件（与 /api/outputs 列表口径一致）
        collected = []
        total = 0
        limit = 512 * 1024 * 1024  # 512MB 安全上限
        for rel_dir in sources:
            base = _os.path.join(workspace, rel_dir)
            if not _os.path.isdir(base):
                continue
            for name in sorted(_os.listdir(base)):
                full = _os.path.join(base, name)
                if not _os.path.isfile(full):
                    continue
                size = _os.path.getsize(full)
                if total + size > limit:
                    raise HTTPException(status_code=413, detail="产出物总大小超过 512MB 上限，请先清理部分文件")
                collected.append((rel_dir, name, full))
                total += size

        if not collected:
            raise HTTPException(status_code=404, detail="没有可下载的产出物")

        buf = _io.BytesIO()
        with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as zf:
            for rel_dir, name, full in collected:
                # 归档名带 outputs/ 前缀，避免与 uploads 混淆
                zf.write(full, f"{rel_dir.strip('.')}/{name}")
        buf.seek(0)
        from datetime import datetime as _dt

        stamp = _dt.now().strftime("%Y%m%d_%H%M")
        filename = f"lite-work-outputs-{stamp}.zip"
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Access-Control-Expose-Headers": "Content-Disposition",
            },
        )

    @router.delete("/api/outputs")
    async def clear_outputs(scope: str = "outputs", request: Request = None):
        """清空产出目录：scope = outputs（默认）/ uploads / all。

        只删除 产出物/素材 下的顶层文件，不影响代码与其他数据。
        """
        if request:
            ctx.check_auth(request)
        import os as _os

        if scope not in ("outputs", "uploads", "all"):
            raise HTTPException(status_code=400, detail="scope 仅支持 outputs / uploads / all")
        workspace = ctx.require_workspace()
        dirs = [OUTPUT_DIR_NAME, UPLOADS_DIR_NAME] if scope == "all" else [(OUTPUT_DIR_NAME if scope == "outputs" else UPLOADS_DIR_NAME)]
        deleted = 0
        for rel_dir in dirs:
            base = _os.path.join(workspace, rel_dir)
            if not _os.path.isdir(base):
                continue
            for name in _os.listdir(base):
                full = _os.path.join(base, name)
                if not _os.path.isfile(full):
                    continue
                try:
                    _os.remove(full)
                    deleted += 1
                except OSError:
                    continue
        return {"ok": True, "scope": scope, "deleted": deleted}

    @router.delete("/api/files")
    async def delete_file(path: str, request: Request = None):
        """删除单个产出物/素材文件（仅限 产出物/素材 内，防误删代码）。"""
        if request:
            ctx.check_auth(request)
        import os as _os

        workspace = ctx.require_workspace()
        rel = (path or "").strip().lstrip("/\\").replace("\\", "/")
        if not rel.split("/")[0] in (OUTPUT_DIR_NAME, UPLOADS_DIR_NAME):
            raise HTTPException(status_code=403, detail="仅支持删除 产出物/素材 内的文件")
        target = _os.path.abspath(_os.path.join(workspace, rel))
        if not (target.startswith(workspace + _os.path.sep) and rel.split("/")[0] in (OUTPUT_DIR_NAME, UPLOADS_DIR_NAME)):
            raise HTTPException(status_code=403, detail="路径越界")
        if not _os.path.isfile(target):
            raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
        try:
            _os.remove(target)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"删除失败: {exc}")
        return {"ok": True, "path": rel}

    @router.get("/api/files/raw")
    async def serve_file_raw(path: str, request: Request = None):
        """内联返回工作区文件（Content-Disposition: inline）。

        用于浏览器直接渲染预览：图片（png/jpg/svg）与 PDF。
        """
        if request:
            ctx.check_auth(request)
        import os as _os

        workspace = ctx.require_workspace()
        target = _os.path.abspath(_os.path.join(workspace, path))
        if not (target == workspace or target.startswith(workspace + _os.path.sep)):
            raise HTTPException(status_code=403, detail="路径越界")
        if not _os.path.isfile(target):
            raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
        return FileResponse(target, media_type=_guess_media_type(target),
                            headers={"Cache-Control": "no-store"})

    @router.get("/api/files/preview")
    async def preview_file(path: str, request: Request = None):
        """结构化预览办公产出物。

        - 图片（png/jpg/jpeg/svg/gif/webp）与 PDF → kind="media"，前端用 /api/files/raw 渲染
        - xlsx → kind="table"，解析前 N 行返回表头+行数据（多 sheet 返回首个，附 sheet 名列表）
        - docx → kind="text"，提取段落与表格文字
        - pptx → kind="slides"，提取每页标题与要点
        - 其他文本类 → kind="text"，直接读前 20000 字符
        """
        if request:
            ctx.check_auth(request)
        import os as _os

        workspace = ctx.require_workspace()
        target = _os.path.abspath(_os.path.join(workspace, path))
        if not (target == workspace or target.startswith(workspace + _os.path.sep)):
            raise HTTPException(status_code=403, detail="路径越界")
        if not _os.path.isfile(target):
            raise HTTPException(status_code=404, detail=f"文件不存在: {path}")

        ext = _os.path.splitext(target)[1].lower()
        name = _os.path.basename(target)

        if ext in (".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp", ".pdf"):
            from urllib.parse import quote
            return {"name": name, "kind": "media",
                    "media_type": _guess_media_type(target),
                    "raw_url": f"/api/files/raw?path={quote(path)}"}

        if ext in (".xlsx", ".xlsm"):
            try:
                import openpyxl
                wb = openpyxl.load_workbook(target, read_only=True, data_only=True)
            except Exception as exc:
                raise HTTPException(status_code=422, detail=f"解析 Excel 失败: {exc}")
            sheet_name = wb.sheetnames[0] if wb.sheetnames else ""
            ws = wb[sheet_name] if sheet_name else None
            rows: list[list] = []
            max_rows, max_cols = 100, 30
            if ws is not None:
                for row in ws.iter_rows(max_row=max_rows, max_col=max_cols, values_only=True):
                    cells = ["" if v is None else str(v) for v in row]
                    # read_only 模式会按 ws 尺寸填充空单元格，裁掉行尾空白
                    while cells and cells[-1] == "":
                        cells.pop()
                    rows.append(cells)
            return {"name": name, "kind": "table", "sheet": sheet_name,
                    "sheets": wb.sheetnames, "rows": rows,
                    "truncated": (ws is not None and ws.max_row > max_rows)}

        if ext == ".docx":
            try:
                from docx import Document
                doc = Document(target)
            except Exception as exc:
                raise HTTPException(status_code=422, detail=f"解析 Word 失败: {exc}")
            parts = []
            for p in doc.paragraphs:
                if p.text.strip():
                    style = (p.style.name or "").lower()
                    prefix = "#" * min(4, 1 + sum(1 for c in style if c.isdigit() and c != "0")) \
                        if "heading" in style else ""
                    parts.append(f"{prefix} {p.text.strip()}".strip())
            for ti, table in enumerate(doc.tables):
                parts.append(f"[表格 {ti + 1}]")
                for row in table.rows[:20]:
                    cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                    parts.append("| " + " | ".join(cells) + " |")
            text = "\n\n".join(parts)
            return {"name": name, "kind": "text", "text": text[:20000],
                    "truncated": len(text) > 20000}

        if ext == ".pptx":
            try:
                from pptx import Presentation
                prs = Presentation(target)
            except Exception as exc:
                raise HTTPException(status_code=422, detail=f"解析 PPT 失败: {exc}")
            slides = []
            for slide in prs.slides:
                title, bullets = "", []
                for shape in slide.shapes:
                    if not shape.has_text_frame:
                        continue
                    tf = shape.text_frame
                    is_title = shape == getattr(slide.shapes, "title", None)
                    for para in tf.paragraphs:
                        t = "".join(run.text for run in para.runs).strip()
                        if not t:
                            continue
                        if is_title and not title:
                            title = t
                        else:
                            bullets.append(t)
                slides.append({"title": title, "bullets": bullets[:12]})
            return {"name": name, "kind": "slides", "slides": slides}

        # 其他文件按文本读取
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                text = f.read(20000)
        except OSError as exc:
            raise HTTPException(status_code=422, detail=f"读取失败: {exc}")
        return {"name": name, "kind": "text", "text": text,
                "truncated": False}

    return router
