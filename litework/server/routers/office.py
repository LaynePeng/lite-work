# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""办公场景：文件上传 / 素材列表 / 文件下载与预览（AGI 通用入口）。

v2 项目结构（2026-10）：交付物直接放项目根目录（侧栏「文件」页签浏览），
原「产出物收件箱」三件套（/api/outputs、/api/outputs/zip、DELETE
/api/outputs）已随侧栏「产出物」tab 移除；本文件保留上传、素材列表
（Composer 的 # 引用数据源）与通用文件操作/预览。
"""
from __future__ import annotations

import os as _os
from datetime import datetime as _dt

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ...tools.office import UPLOADS_DIR_NAME
from .context import ServerContext


def _list_uploads(workspace: str) -> list:
    """列出 素材/ 下的文件（供 Composer 的 # 引用面板）。

    跳过隐藏文件与 Office 锁文件（`~$xxx`）；递归但记录相对路径。
    """
    base = _os.path.join(workspace, UPLOADS_DIR_NAME)
    out: list = []
    if not _os.path.isdir(base):
        return out
    for root, dirs, files in _os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.startswith(".") or name.startswith("~$"):
                continue
            full = _os.path.join(root, name)
            try:
                st = _os.stat(full)
            except OSError:
                continue
            out.append({
                "name": name,
                "path": _os.path.relpath(full, workspace).replace("\\", "/"),
                "source": "uploads",
                "size": st.st_size,
                "mtime": _dt.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
    out.sort(key=lambda x: str(x["mtime"]), reverse=True)
    return out


class RenameRequest(BaseModel):
    """重命名工作区内文件：path=工作区相对路径，new_name=新文件名（不含目录）。"""

    path: str
    new_name: str


class DeleteFilesRequest(BaseModel):
    """批量删除工作区内文件：paths=工作区相对路径列表（单次最多 500 个）。"""

    paths: list[str] = []


class CreateEntryRequest(BaseModel):
    """在工作区内新建文件/文件夹。

    parent=工作区相对目录（"" 或 "." 表示工作区根），name=单层名称，
    kind=file（空文件）| dir（空目录）。文件页签在目录上右键时使用。
    """

    parent: str = ""
    name: str
    kind: str = "file"


def _guess_media_type(target: str) -> str:
    import mimetypes
    mt, _ = mimetypes.guess_type(target)
    return mt or "application/octet-stream"


def _is_git_internal(rel: str) -> bool:
    """路径是否位于 .git/ 内部（禁止经 UI 改删仓库元数据）。"""
    return ".git" in (rel or "").split("/")


def _resolve_delete_target(workspace: str, path: str, allow_dir: bool = False) -> tuple:
    """单删/批量删除共用的路径守卫：返回 (规范化相对路径, 绝对路径, 是否目录)。

    守卫规则（与文件页签删除语义一致）：
    - 路径为空 → 400；
    - `.git/` 内部 → 403（防破坏仓库元数据）；
    - `..` 等越界 → 403（含直接用 `.`/`..` 指向工作区根自身的情形）；
    - 目录：allow_dir=False → 400（批量删除不支持目录）；allow_dir=True → 放行，
      由调用方要求显式 recursive 并做二次确认；
    - 目标不存在 → 404。
    违规时抛 HTTPException；调用方决定抛出（单删）还是记 failed（批量）。
    """
    import os as _os

    rel = (path or "").strip().lstrip("/\\").replace("\\", "/")
    if not rel:
        raise HTTPException(status_code=400, detail="缺少 path")
    if _is_git_internal(rel):
        raise HTTPException(status_code=403, detail="不允许操作 .git 内部文件")
    target = _os.path.abspath(_os.path.join(workspace, rel))
    if not target.startswith(workspace + _os.path.sep):
        raise HTTPException(status_code=403, detail="路径越界")
    if _os.path.isdir(target):
        if not allow_dir:
            raise HTTPException(status_code=400, detail="不支持删除目录")
        return rel, target, True
    if not _os.path.isfile(target):
        raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
    return rel, target, False


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
    async def download_file(path: str, request: Request):
        """下载工作区内的文件（docx/xlsx/pptx/pdf/图片等）。"""
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

    @router.get("/api/uploads")
    async def list_uploads(request: Request):
        """列出 素材/ 下的文件（Composer 的 # 引用面板数据源）。

        返回 {items: [{name, path, source, size, mtime}], total}。
        """
        ctx.check_auth(request)
        workspace = ctx.require_workspace()
        items = _list_uploads(workspace)
        return {"items": items, "total": len(items)}

    @router.delete("/api/files")
    async def delete_file(request: Request, path: str, recursive: bool = False):
        """删除工作区内的文件或目录（素材与源码文件均可）。

        侧边栏「文件」页签使用此端点；底线不放开的项：
        - 只允许工作区内的路径（`..` 越界一律 403）；
        - `.git/` 内部文件禁止删除（避免破坏仓库）；
        - 文件：直接删除；
        - 目录：必须显式 `recursive=true`（UI 侧二次确认「连同内容一并删除」），
          否则 400；工作区根目录本身不可删（越界守卫已拦）。
        """
        ctx.check_auth(request)
        import os as _os

        workspace = ctx.require_workspace()
        rel, target, is_dir = _resolve_delete_target(workspace, path, allow_dir=True)
        if is_dir:
            if not recursive:
                raise HTTPException(
                    status_code=400,
                    detail="删除目录需显式 recursive=true（目录内容一并删除且不可恢复）",
                )
            import shutil as _shutil

            try:
                _shutil.rmtree(target)
            except OSError as exc:
                raise HTTPException(status_code=500, detail=f"删除目录失败: {exc}")
            return {"ok": True, "path": rel, "kind": "dir"}
        try:
            _os.remove(target)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"删除失败: {exc}")
        return {"ok": True, "path": rel, "kind": "file"}

    @router.post("/api/files/delete-batch")
    async def delete_files_batch(payload: DeleteFilesRequest, request: Request):
        """批量删除工作区内的文件（单删守卫复用，单项失败不中断整批）。

        与单删 DELETE /api/files 的区别：任一路径守卫失败或删除异常时，
        不中断整批，而是把该项记入 failed 继续处理其余路径。
        响应：{ok, deleted: 成功数, failed: [{path: 原样相对路径, error: 中文原因}]}。
        """
        ctx.check_auth(request)
        import os as _os

        workspace = ctx.require_workspace()
        if not payload.paths:
            raise HTTPException(status_code=400, detail="paths 不能为空")
        if len(payload.paths) > 500:
            raise HTTPException(status_code=400, detail="单次最多删除 500 个文件")
        deleted = 0
        failed: list = []
        for path in payload.paths:
            try:
                _, target, _ = _resolve_delete_target(workspace, path)
                _os.remove(target)
                deleted += 1
            except HTTPException as exc:
                failed.append({"path": path, "error": str(exc.detail)})
            except OSError as exc:
                failed.append({"path": path, "error": f"删除失败: {exc}"})
        return {"ok": True, "deleted": deleted, "failed": failed}

    @router.post("/api/files/create")
    async def create_entry(payload: CreateEntryRequest, request: Request):
        """在工作区内新建空文件 / 空目录（文件页签：在目录上右键「新建…」）。

        守卫（与删除/重命名同一套底线）：
        - parent 必须是工作区内**已存在**的目录（`""`/`"."` = 工作区根）；
          `.git/` 内部 → 403；`..` 越界 → 403；
        - name 只允许单层名称：非空、不含路径分隔符与 `*?"<>|` 等字符、
          不是 `.` / `..`；
        - 目标已存在 → 409（不覆盖）；
        - kind 仅 file | dir。
        """
        ctx.check_auth(request)
        import os as _os

        workspace = ctx.require_workspace()
        parent_rel = (payload.parent or "").strip().lstrip("/\\").replace("\\", "/")
        if parent_rel in (".", "/"):
            parent_rel = ""
        if parent_rel and _is_git_internal(parent_rel):
            raise HTTPException(status_code=403, detail="不允许在 .git 内部新建")
        parent_abs = (
            _os.path.abspath(_os.path.join(workspace, parent_rel)) if parent_rel else workspace
        )
        if parent_abs != workspace and not parent_abs.startswith(workspace + _os.path.sep):
            raise HTTPException(status_code=403, detail="路径越界")
        if not _os.path.isdir(parent_abs):
            raise HTTPException(status_code=404, detail=f"目录不存在: {parent_rel or '.'}")

        name = (payload.name or "").strip()
        if not name or name in (".", ".."):
            raise HTTPException(status_code=400, detail="名称不能为空")
        if name != _os.path.basename(name) or any(c in name for c in '\\/:*?"<>|'):
            raise HTTPException(status_code=400, detail="名称不能包含路径分隔符或特殊字符")

        kind = (payload.kind or "file").strip().lower()
        if kind not in ("file", "dir"):
            raise HTTPException(status_code=400, detail="kind 仅支持 file / dir")

        rel = f"{parent_rel}/{name}" if parent_rel else name
        if _is_git_internal(rel):
            raise HTTPException(status_code=403, detail="不允许操作 .git 内部项")
        target = _os.path.join(parent_abs, name)
        if _os.path.exists(target):
            raise HTTPException(status_code=409, detail=f"同名项已存在: {name}")
        try:
            if kind == "dir":
                _os.mkdir(target)
            else:
                # 'x'：独占创建，避免与并发写入互相覆盖
                with open(target, "x", encoding="utf-8"):
                    pass
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"创建失败: {exc}")
        return {"ok": True, "path": rel, "name": name, "kind": kind}

    @router.post("/api/files/rename")
    async def rename_file(payload: RenameRequest, request: Request):
        """重命名工作区内的单个文件或目录（素材与源码文件均可，同目录改名）。

        侧边栏「文件」页签使用此端点；底线不放开的项：
        - 只允许工作区内、且保持在同一目录下改名（不允许挪目录）；
        - 不允许修改扩展名（仅对文件；目录名无扩展名语义，防把 .xlsx 改成别的东西后解析失败）；
        - 新文件名为空 / 含路径分隔符与危险字符 → 400；
        - `.git/` 内部文件禁止重命名；
        - 目标同名已存在 → 409。
        """
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
        if not _os.path.exists(target):
            raise HTTPException(status_code=404, detail=f"文件或目录不存在: {rel}")

        new_name = (payload.new_name or "").strip()
        if not new_name or new_name in (".", ".."):
            raise HTTPException(status_code=400, detail="新文件名不能为空")
        # 禁止任何路径成分与危险字符（只允许改文件名，不允许挪目录）
        if new_name != _os.path.basename(new_name) or any(c in new_name for c in '\\/:*?"<>|'):
            raise HTTPException(status_code=400, detail="文件名不能包含路径分隔符或特殊字符")
        # 扩展名约束只对文件生效：目录改名无扩展名语义（如 `v1.2` 不是后缀）
        if _os.path.isfile(target) and _os.path.splitext(rel)[1].lower() != _os.path.splitext(new_name)[1].lower():
            raise HTTPException(status_code=400, detail="不允许修改文件扩展名")
        # M4：新名字也不能是 .git（_is_git_internal 只查旧路径；把目录改名成
        # .git 会污染后续 git 语义——工作区根目录变成 git 仓库元数据目录）
        parent_rel = _os.path.dirname(rel)
        new_rel_check = f"{parent_rel}/{new_name}" if parent_rel else new_name
        if _is_git_internal(new_rel_check):
            raise HTTPException(status_code=403, detail="不允许改名为 .git 内部项")

        # 与原文件/目录同目录（支持嵌套目录下的源码文件）
        parent = _os.path.dirname(rel)
        new_rel = f"{parent}/{new_name}" if parent else new_name
        new_path = _os.path.join(workspace, parent, new_name) if parent else _os.path.join(workspace, new_name)
        if new_name == _os.path.basename(rel):
            # 名字没变：幂等返回
            return {"ok": True, "path": rel, "name": new_name}
        if _os.path.exists(new_path):
            raise HTTPException(status_code=409, detail=f"同名项已存在: {new_name}")
        try:
            # TOCTOU 防护：os.rename 在 POSIX 上会静默覆盖已存在目标——
            # exists 检查与 rename 之间的窗口里并发创建的同名文件会被覆盖。
            # 改用 link + unlink：link 在目标已存在时抛 FileExistsError（原子失败）
            if _os.path.isdir(target):
                # 目录改名没有 link 等价物；退回 rename（目录覆盖窗口极窄，可接受）
                _os.rename(target, new_path)
            else:
                _os.link(target, new_path)
                _os.unlink(target)
        except FileExistsError:
            raise HTTPException(status_code=409, detail=f"同名项已存在: {new_name}")
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"重命名失败: {exc}")
        return {"ok": True, "path": new_rel, "name": new_name}

    @router.get("/api/files/raw")
    async def serve_file_raw(path: str, request: Request):
        """内联返回工作区文件（Content-Disposition: inline）。

        用于浏览器直接渲染预览：图片（png/jpg/svg）与 PDF。
        """
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
    async def preview_file(path: str, request: Request):
        """结构化预览办公文件（docx/xlsx/pptx/pdf/图片）。

        - 图片（png/jpg/jpeg/svg/gif/webp）与 PDF → kind="media"，前端用 /api/files/raw 渲染
        - xlsx → kind="table"，解析前 N 行返回表头+行数据（多 sheet 返回首个，附 sheet 名列表）
        - docx → kind="text"，提取段落与表格文字
        - pptx → kind="slides"，提取每页标题与要点
        - 其他文本类 → kind="text"，直接读前 20000 字符
        """
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
