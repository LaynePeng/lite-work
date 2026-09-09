# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""工作区 / 项目 / 文件系统：/api/workspace*、/api/tools、/api/projects/*、/api/fs/*。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .context import ServerContext


class WorkspaceUpdateRequest(BaseModel):
    path: str


class ProjectCreateRequest(BaseModel):
    """新建项目：parent 下创建 name 目录，git=True 时初始化 git 仓库。"""
    parent: str
    name: str
    git: bool = True


def create_router(ctx: ServerContext) -> APIRouter:
    router = APIRouter()
    app, tasks = ctx.app, ctx.tasks

    # ------------------------------------------------------------ 工具与工作区

    @router.get("/api/tools")
    async def list_tools(request: Request, agent_id: str = ""):
        ctx.check_auth(request)
        ctx.require_workspace()
        # 按 Agent 裁剪工具集：前端「工具」Tab 展示当前 Agent 可用工具，随 Agent 切换刷新
        registry = (
            app.create_agent_registry(agent_id)
            if agent_id
            else app.build_registry()
        )
        return [
            {"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in registry.get_tools()
        ]

    @router.get("/api/workspace/tree")
    async def workspace_tree(depth: int = 3, request: Request = None):
        if request:
            ctx.check_auth(request)
        from ...tools.filesystem import FileSystemTools

        workspace = ctx.require_workspace()
        fs = FileSystemTools(workspace)
        return {"workspace": workspace, "tree": fs._file_tree({"maxDepth": depth})}

    @router.get("/api/workspace/tree-json")
    async def workspace_tree_json(path: str = "", request: Request = None):
        """结构化目录树（侧边栏文件页签）：按路径懒加载 + git 状态字母。"""
        if request:
            ctx.check_auth(request)
        from ..tree import list_tree

        workspace = ctx.require_workspace()
        rel = path.strip().lstrip("/\\") or ""
        try:
            data = await asyncio.to_thread(list_tree, workspace, rel)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {
            "workspace": workspace,
            "path": rel,
            "git": {"branch": data["branch"], "has_repo": data["has_repo"]},
            "entries": data["entries"],
        }

    # ------------------------------------------------------------ 工作区

    @router.post("/api/workspace")
    async def set_workspace(payload: WorkspaceUpdateRequest, request: Request):
        ctx.check_auth(request)
        import os as _os
        path = _os.path.abspath(_os.path.expanduser(payload.path))
        if not _os.path.isdir(path):
            raise HTTPException(status_code=400, detail=f"目录不存在: {path}")
        if tasks.active_count() > 0:
            raise HTTPException(status_code=409, detail="当前有任务运行，请停止或等待任务结束后再切换项目")
        bg = app.background_agent_count()
        if bg > 0:
            raise HTTPException(status_code=409, detail=f"当前有 {bg} 个后台 Agent 运行中（工作区是其执行基准），请等待结束后再切换项目")
        app.workspace = path
        # 切换工作区即记录到最近项目（含目录选择器/子 Agent 场景）
        try:
            kind = "code" if _os.path.isdir(_os.path.join(path, ".git")) else "project"
            app.remember_project(path, kind=kind)
        except Exception:
            pass
        return {"ok": True, "workspace": app.workspace}

    @router.post("/api/projects/create")
    async def create_project(payload: ProjectCreateRequest, request: Request):
        """新建项目：在 parent 下创建 name 目录；git=True（默认）时初始化 git 仓库。

        用于「新建项目 / 新建代码」入口：新项目默认是 git 仓库，侧边栏文件树
        即可直接展示分支与状态（git tree）。
        """
        ctx.check_auth(request)
        import os as _os
        import re as _re
        import subprocess as _sp

        name = (payload.name or "").strip()
        if not name or not _re.fullmatch(r"[\w.\-\u4e00-\u9fff]+", name):
            raise HTTPException(status_code=400, detail="项目名仅支持字母/数字/中文/._- 且不含空格")
        if name.startswith("."):
            raise HTTPException(status_code=400, detail="项目名不能以 . 开头")

        parent = _os.path.abspath(_os.path.expanduser(payload.parent))
        if not _os.path.isdir(parent):
            raise HTTPException(status_code=400, detail=f"父目录不存在: {parent}")

        target = _os.path.join(parent, name)
        if _os.path.exists(target):
            raise HTTPException(status_code=409, detail=f"目录已存在: {target}")

        _os.makedirs(target, exist_ok=False)

        git_initialized = False
        if payload.git:
            try:
                proc = _sp.run(
                    ["git", "init", "-b", "main"],
                    cwd=target, capture_output=True, text=True, timeout=30,
                )
                # 旧版 git 不支持 -b，退回普通 init
                if proc.returncode != 0:
                    proc = _sp.run(
                        ["git", "init"],
                        cwd=target, capture_output=True, text=True, timeout=30,
                    )
                git_initialized = proc.returncode == 0 and _os.path.isdir(_os.path.join(target, ".git"))
            except (OSError, _sp.TimeoutExpired):
                git_initialized = False

        # 新建即记住（代码仓库优先按 git 判定 kind）
        try:
            app.remember_project(target, kind="code" if git_initialized else "project")
        except Exception:
            pass
        return {"ok": True, "path": target, "name": name, "git_initialized": git_initialized}

    @router.get("/api/projects/recent")
    async def list_recent_projects(request: Request = None):
        """最近打开的项目（侧边栏「项目」页签）。"""
        if request:
            ctx.check_auth(request)
        return {"items": app.list_recent_projects()}

    @router.post("/api/projects/recent")
    async def open_recent_project(payload: WorkspaceUpdateRequest, request: Request):
        """打开（切换到）一个最近项目：切换工作区并置顶记录。"""
        ctx.check_auth(request)
        import os as _os

        path = _os.path.abspath(_os.path.expanduser(payload.path))
        if not _os.path.isdir(path):
            raise HTTPException(status_code=400, detail=f"目录不存在: {path}")
        if tasks.active_count() > 0:
            raise HTTPException(status_code=409, detail="当前有任务运行，请停止或等待任务结束后再切换项目")
        bg = app.background_agent_count()
        if bg > 0:
            raise HTTPException(status_code=409, detail=f"当前有 {bg} 个后台 Agent 运行中（工作区是其执行基准），请等待结束后再切换项目")
        # kind 由前端按入口传入（打开代码=code / 打开项目=project），缺省按 git 判定
        kind = "code" if _os.path.isdir(_os.path.join(path, ".git")) else "project"
        app.remember_project(path, kind=kind)
        app.workspace = path
        return {"ok": True, "workspace": path, "kind": kind}

    @router.delete("/api/projects/recent")
    async def remove_recent_project(path: str, request: Request):
        """从最近列表移除一个项目（不删除磁盘文件）。"""
        ctx.check_auth(request)
        app.forget_project(path)
        return {"ok": True}

    @router.post("/api/projects/pin")
    async def toggle_project_pin(payload: WorkspaceUpdateRequest, request: Request):
        """置顶/取消置顶一个最近项目。返回翻转后的 pinned 状态。"""
        ctx.check_auth(request)
        try:
            pinned = app.toggle_project_pin(payload.path)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"ok": True, "path": payload.path, "pinned": pinned}

    @router.get("/api/fs/list")
    async def fs_list(path: str = "", show_hidden: bool = False, request: Request = None):
        """浏览任意目录（用于「打开项目」目录树选择）。

        - show_hidden=False（默认）过滤 . 开头的隐藏文件/目录
        - Windows：path 为空或盘符根时列出所有可用盘符（目录选择需先选盘）
        """
        if request:
            ctx.check_auth(request)
        import os as _os
        import string as _string
        import sys as _sys

        base = _os.path.expanduser(path) if path else (app.workspace or _os.path.expanduser("~"))
        base = _os.path.abspath(base)
        if not _os.path.isdir(base):
            raise HTTPException(status_code=400, detail=f"目录不存在: {base}")

        is_win = _sys.platform == "win32"

        def list_drives():
            drives = []
            for letter in _string.ascii_uppercase:
                d = f"{letter}:\\"
                if _os.path.isdir(d):
                    drives.append(d)
            return drives

        # Windows 盘符根（如 C:\）：parent 置空并在目录里列出全部盘符
        at_drive_root = is_win and _os.path.splitdrive(base)[1] in ("\\", "/", "")
        parent = None
        if at_drive_root:
            parent = None
        else:
            parent = _os.path.dirname(base) or None
            # POSIX 根目录 / 的 parent 为 None
            if base == _os.path.sep:
                parent = None

        dirs, files = [], []
        try:
            entries = _os.listdir(base)
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"无法读取: {exc}")

        for name in entries:
            full = _os.path.join(base, name)
            if not show_hidden and (name.startswith(".") or name == "Thumbs.db" or name == "desktop.ini"):
                continue
            try:
                if _os.path.isdir(full):
                    dirs.append(name)
                else:
                    files.append(name)
            except OSError:
                continue

        # Windows 盘符根：把其他盘符也列为可进入的"目录"
        if at_drive_root:
            cur_drive = _os.path.splitdrive(base)[0].upper()
            for d in list_drives():
                if d[0] != cur_drive[0]:
                    dirs.insert(0, d)

        def key(n: str):
            return n.lower()

        dirs.sort(key=key)
        files.sort(key=key)
        cap = 500
        return {
            "path": base,
            "parent": parent,
            "home": _os.path.expanduser("~"),
            "is_workspace": base == app.workspace,
            "dirs": dirs[:cap],
            "files": files[:cap],
            "truncated": len(dirs) + len(files) > cap,
        }

    @router.get("/api/fs/read")
    async def fs_read(path: str, request: Request = None):
        """读取工作区内的文件，返回内容、语言、行数和 git diff。"""
        if request:
            ctx.check_auth(request)
        import os as _os

        workspace = ctx.require_workspace()
        target = _os.path.abspath(_os.path.join(workspace, path))
        if not (target == workspace or target.startswith(workspace + _os.path.sep)):
            raise HTTPException(status_code=403, detail="路径越界")
        if not _os.path.isfile(target):
            raise HTTPException(status_code=404, detail=f"文件不存在: {path}")

        with open(target, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        lines = content.split("\n")
        ext = _os.path.splitext(path)[1].lower()

        # 简单语言检测
        lang_map = {
            ".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "tsx",
            ".jsx": "jsx", ".go": "go", ".rs": "rust", ".java": "java",
            ".c": "c", ".cpp": "cpp", ".h": "c", ".hpp": "cpp",
            ".css": "css", ".scss": "scss", ".less": "less",
            ".html": "html", ".htm": "html", ".xml": "xml", ".json": "json",
            ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
            ".md": "markdown", ".txt": "text", ".sh": "bash", ".bash": "bash",
            ".zsh": "bash", ".sql": "sql", ".rb": "ruby",
            ".swift": "swift", ".kt": "kotlin", ".svelte": "svelte",
            ".vue": "vue", ".astro": "astro",
        }
        language = lang_map.get(ext, "")

        # 获取 git diff（工作区 vs HEAD）
        diff_text = ""
        try:
            proc = __import__("subprocess").run(
                ["git", "-C", workspace, "diff", "HEAD", "--", path],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=10,
            )
            if proc.returncode == 0:
                diff_text = proc.stdout
        except Exception:
            pass

        return {
            "path": path,
            "content": content,
            "language": language,
            "lines": len(lines),
            "size": len(content),
            "diff": diff_text,
        }

    @router.get("/api/workspace/diff")
    async def workspace_file_diff(path: str, request: Request = None):
        """获取单个文件的 git diff（工作区 vs HEAD）。"""
        if request:
            ctx.check_auth(request)
        workspace = ctx.require_workspace()
        try:
            proc = __import__("subprocess").run(
                ["git", "-C", workspace, "diff", "HEAD", "--", path],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=10,
            )
            diff_text = proc.stdout if proc.returncode == 0 else ""
        except Exception:
            diff_text = ""
        additions = len([l for l in diff_text.split("\n") if l.startswith("+") and not l.startswith("+++")])
        deletions = len([l for l in diff_text.split("\n") if l.startswith("-") and not l.startswith("---")])
        return {"path": path, "diff": diff_text, "additions": additions, "deletions": deletions}

    return router
