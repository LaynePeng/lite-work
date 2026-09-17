# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""办公场景：文件上传 / 产出物下载与预览（AGI 通用入口）。"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ...tools.office import OUTPUT_DIR_NAME, UPLOADS_DIR_NAME
from .context import ServerContext

# ——— 产出物收件箱：按类型分组 + 只留最新版本 ———
#
# Agent 会在 产出物/ 下按类型建子目录（图表/演示/报告/表格数据/论文/专利…），
# 产出带 `_vN` 版本号的文件，并把旧版挪进 `归档/`、过程文件放进 `中间产物/`。
# 收件箱只呈现「每类的最新交付物」：跳过归档/中间产物，按 (类型, 基名, 扩展名)
# 折叠版本，保留版本号最大的那一个——新版本产出后自动替换旧条目。

# 不视为交付物的目录（归档 / 过程产物 / 工具元数据）
_SKIP_DIRS = {"归档", "中间产物", ".git", "__pycache__", "node_modules", ".DS_Store"}
# 视为交付物的扩展名（LaTeX 中间产物 .aux/.log/.toc 等不算）
_DELIVERABLE_EXTS = {
    ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".pdf",
    ".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp",
    ".md", ".txt", ".csv", ".html", ".zip", ".mmd", ".puml",
}
# 目录自身索引不算交付物
_SKIP_FILES = {"INDEX.md", "index.md"}
# 版本号：`_v1` / `-v2` / `v1.2` / `（3）` / `(3)`
_VER_RE = re.compile(r"(?:[_\-\s]*[vV](\d+(?:\.\d+)?)|[（(](\d+)[)）])\s*$")


def _split_version(stem: str) -> tuple:
    """拆出「基名 + 版本号」：('专利简报_v1' → ('专利简报', 1.0))；无版本号 → (stem, 0)。"""
    m = _VER_RE.search(stem)
    if not m:
        return stem, 0.0
    ver = m.group(1) or m.group(2) or "0"
    try:
        return stem[:m.start()].strip(" _-"), float(ver)
    except ValueError:
        return stem, 0.0


def _collect_deliverables(base: str, rel_base: str, source: str) -> list:
    """收集 base 目录下的交付物（跳过归档/中间产物，折叠版本，只留最新）。

    返回条目列表：{name, path, source, category, size, mtime, ext, version}。
    category = 一级子目录名（根目录下的散文件归入「未分类」）。
    """
    import os as _os
    from datetime import datetime

    out: list = []
    if not _os.path.isdir(base):
        return out
    # 一层子目录 = 类型；根目录散文件 = 未分类。更深一层若非跳过目录，归入其一级类型。
    for root, dirs, files in _os.walk(base):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        rel_root = _os.path.relpath(root, base)
        parts = [] if rel_root == "." else rel_root.split(_os.sep)
        category = parts[0] if parts else "未分类"
        for name in files:
            if name.startswith(".") or name in _SKIP_FILES:
                continue
            ext = _os.path.splitext(name)[1].lower()
            if ext not in _DELIVERABLE_EXTS:
                continue
            full = _os.path.join(root, name)
            try:
                st = _os.stat(full)
            except OSError:
                continue
            stem = name[: -len(ext)] if ext else name
            base_name, ver = _split_version(stem)
            out.append({
                "name": name,
                "path": f"{rel_base}/{_os.path.relpath(full, base)}".replace("\\", "/"),
                "source": source,
                "category": category,
                "ext": ext.lstrip("."),
                "version": ver,
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "_key": (category, base_name.lower(), ext),
            })
    return out


def _latest_per_item(entries: list) -> dict:
    """按 (类型, 基名, 扩展名) 折叠版本，只保留版本号最大的一条。"""
    best: dict = {}
    for e in entries:
        k = e["_key"]
        cur = best.get(k)
        if cur is None or e["version"] > cur["version"] or (
            e["version"] == cur["version"] and e["mtime"] > cur["mtime"]
        ):
            best[k] = e
    return best


class RenameRequest(BaseModel):
    """重命名工作区内文件：path=工作区相对路径，new_name=新文件名（不含目录）。"""

    path: str
    new_name: str


def _guess_media_type(target: str) -> str:
    import mimetypes
    mt, _ = mimetypes.guess_type(target)
    return mt or "application/octet-stream"


def _is_git_internal(rel: str) -> bool:
    """路径是否位于 .git/ 内部（禁止经 UI 改删仓库元数据）。"""
    return ".git" in (rel or "").split("/")


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
        """产出物收件箱数据：按类型分组 + 只留每项的最新版本。

        - 类型 = 产出物/ 下的一级子目录（图表/演示/报告/…）；根目录散文件归「未分类」；
        - 跳过「归档/」「中间产物/」等目录与非交付物扩展名（.aux/.log/…）；
        - 同一 (类型, 基名, 扩展名) 只保留版本号最大的那条（_v2 覆盖 _v1）——
          新版本产出后收件箱自动呈现最新版，旧版不显示（仍在 归档/ 里）。
        - 素材/ 同样处理，归入 source=uploads 的分组。
        返回 {groups: [{name, source, items: [...]}], total}（空分组不返回）。
        """
        if request:
            ctx.check_auth(request)
        import os as _os

        workspace = ctx.require_workspace()
        entries = []
        entries += _collect_deliverables(
            _os.path.join(workspace, OUTPUT_DIR_NAME), OUTPUT_DIR_NAME, "outputs")
        entries += _collect_deliverables(
            _os.path.join(workspace, UPLOADS_DIR_NAME), UPLOADS_DIR_NAME, "uploads")

        # 折叠版本：每项只留最新
        latest = _latest_per_item(entries)
        items = list(latest.values())
        for it in items:
            it.pop("_key", None)
        # 分组：先产出物类型，再素材；组内按时间倒序（最新在前）
        groups: dict = {}
        for it in items:
            key = ("outputs", it["category"]) if it["source"] == "outputs" else ("uploads", it["category"])
            groups.setdefault(key, []).append(it)
        out = []
        for (source, category), gi in groups.items():
            gi.sort(key=lambda x: str(x["mtime"]), reverse=True)
            out.append({"name": category, "source": source, "items": gi})
        # 组间排序：最新产出在前（产出物/素材同等对待，谁新谁前）
        out.sort(key=lambda g: str(g["items"][0]["mtime"]), reverse=True)
        total = sum(len(g["items"]) for g in out)
        return {"groups": out, "total": total}

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
        """删除工作区内的单个文件（产出物/素材 与源码文件均可）。

        侧边栏「文件」页签与「产出物」面板共用此端点；底线不放开的项：
        - 只允许工作区内的路径（`..` 越界一律 403）；
        - `.git/` 内部文件禁止删除（避免破坏仓库）；
        - 只允许删除文件，不允许删除目录（目录需递归确认，UI 未提供）。
        """
        if request:
            ctx.check_auth(request)
        import os as _os

        workspace = ctx.require_workspace()
        rel = (path or "").strip().lstrip("/\\").replace("\\", "/")
        if not rel:
            raise HTTPException(status_code=400, detail="缺少 path")
        if _is_git_internal(rel):
            raise HTTPException(status_code=403, detail="不允许操作 .git 内部文件")
        target = _os.path.abspath(_os.path.join(workspace, rel))
        if not target.startswith(workspace + _os.path.sep):
            raise HTTPException(status_code=403, detail="路径越界")
        if _os.path.isdir(target):
            raise HTTPException(status_code=400, detail="不支持删除目录")
        if not _os.path.isfile(target):
            raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
        try:
            _os.remove(target)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"删除失败: {exc}")
        return {"ok": True, "path": rel}

    @router.post("/api/files/rename")
    async def rename_file(payload: RenameRequest, request: Request = None):
        """重命名工作区内的单个文件（产出物/素材 与源码文件均可，同目录改名）。

        侧边栏「文件」页签与「产出物」面板共用此端点；底线不放开的项：
        - 只允许工作区内、且保持在同一目录下改名（不允许挪目录）；
        - 不允许修改扩展名（防把 .xlsx 改成别的东西后解析失败）；
        - 新文件名为空 / 含路径分隔符与危险字符 → 400；
        - `.git/` 内部文件禁止重命名；
        - 目标同名已存在 → 409。
        """
        if request:
            ctx.check_auth(request)
        import os as _os

        workspace = ctx.require_workspace()
        rel = (payload.path or "").strip().lstrip("/\\").replace("\\", "/")
        if not rel:
            raise HTTPException(status_code=400, detail="缺少 path")
        if _is_git_internal(rel):
            raise HTTPException(status_code=403, detail="不允许操作 .git 内部文件")
        target = _os.path.abspath(_os.path.join(workspace, rel))
        if not target.startswith(workspace + _os.path.sep):
            raise HTTPException(status_code=403, detail="路径越界")
        if not _os.path.isfile(target):
            raise HTTPException(status_code=404, detail=f"文件不存在: {rel}")

        new_name = (payload.new_name or "").strip()
        if not new_name or new_name in (".", ".."):
            raise HTTPException(status_code=400, detail="新文件名不能为空")
        # 禁止任何路径成分与危险字符（只允许改文件名，不允许挪目录）
        if new_name != _os.path.basename(new_name) or any(c in new_name for c in '\\/:*?"<>|'):
            raise HTTPException(status_code=400, detail="文件名不能包含路径分隔符或特殊字符")
        if _os.path.splitext(rel)[1].lower() != _os.path.splitext(new_name)[1].lower():
            raise HTTPException(status_code=400, detail="不允许修改文件扩展名")

        # 与原文件同目录（支持嵌套目录下的源码文件）
        parent = _os.path.dirname(rel)
        new_rel = f"{parent}/{new_name}" if parent else new_name
        new_path = _os.path.join(workspace, parent, new_name) if parent else _os.path.join(workspace, new_name)
        if new_name == _os.path.basename(rel):
            # 名字没变：幂等返回
            return {"ok": True, "path": rel, "name": new_name}
        if _os.path.exists(new_path):
            raise HTTPException(status_code=409, detail=f"同名文件已存在: {new_name}")
        try:
            _os.rename(target, new_path)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"重命名失败: {exc}")
        return {"ok": True, "path": new_rel, "name": new_name}

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
