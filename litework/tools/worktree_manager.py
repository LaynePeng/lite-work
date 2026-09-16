# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""隔离工作树（git worktree）生命周期管理。

为什么需要它：让 Agent 在**独立分支 + 独立目录**里干活——主工作区不被污染，
改了什么用户可以审查后再决定「合并」或「丢弃」。参考 Claude Code 的 worktree
模型，但把「评审门」做成显式动作（Claude Code 是退出即处理）。

目录与分支约定：
- worktree 目录：`<workspace>/.lite-work/worktrees/<name>/`（项目内可见，非系统临时目录）
- 分支：`worktree-<name>`
- 基准：当前 HEAD

依赖处理：worktree 只含 git-tracked 文件，`node_modules` / `.venv` / `.env` 等
被 gitignore 的依赖不会被带过去。这里在创建后自动 symlink 常见依赖目录，让 Agent
在 worktree 里也能 `npm test` / `pytest`（跨文件系统 symlink 失败时静默跳过）。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("litework.worktree")

# worktree 根目录（相对 workspace）与分支前缀
WORKTREE_DIR = os.path.join(".lite-work", "worktrees")
BRANCH_PREFIX = "worktree-"

# 创建 worktree 后自动 symlink 进 worktree 的常见依赖路径（相对 workspace）
DEFAULT_LINK_DEPS = (
    "node_modules",
    "web/node_modules",
    ".venv",
    "venv",
    ".env",
    ".env.local",
)

# 依赖 symlink 的额外来源：项目根 `.worktreeinclude`（.gitignore 语法，可选）
WORKTREE_INCLUDE_FILE = ".worktreeinclude"


def _safe_name(name: str) -> Optional[str]:
    """worktree 名安全校验：防路径穿越与 git 非法字符。"""
    name = (name or "").strip().strip("/")
    if not name or len(name) > 80:
        return None
    if name in (".", "..") or "\\" in name or ":" in name:
        return None
    if not all(c.isalnum() or c in "._-" for c in name):
        return None
    return name


class WorktreeManager:
    """单个 workspace 的 worktree 管理器（无状态，每次操作现查现算）。"""

    def __init__(self, workspace: Optional[str]) -> None:
        self.workspace = os.path.abspath(workspace) if workspace else None

    # ------------------------------------------------------------ 基础能力

    def is_git_repo(self) -> bool:
        if not self.workspace or not os.path.isdir(self.workspace):
            return False
        return self._git(self._ws, "rev-parse", "--is-inside-work-tree").returncode == 0

    def is_dirty(self) -> bool:
        """主工作区是否有未提交改动（含未跟踪文件）。"""
        if not self.is_git_repo():
            return False
        r = self._git(self._ws, "status", "--porcelain")
        return bool(r.stdout.strip())

    def _git(self, cwd: str, *args: str, input_text: Optional[str] = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", cwd, *args],
            input=input_text,
            capture_output=True, text=True, timeout=120,
        )

    @property
    def _ws(self) -> str:
        """主工作区绝对路径（调用前应确保 workspace 已设置）。"""
        return self.workspace or "."

    def _root(self) -> str:
        assert self.workspace is not None
        return os.path.join(self.workspace, WORKTREE_DIR)

    def path_of(self, name: str) -> str:
        return os.path.join(self._root(), name)

    def branch_of(self, name: str) -> str:
        return f"{BRANCH_PREFIX}{name}"

    # ------------------------------------------------------------ 创建

    def create(self, name: str) -> Dict[str, Any]:
        """创建 worktree（分支 worktree-<name>，基于当前 HEAD）。

        返回 {"ok": bool, "path": str, "branch": str, "reason": str}。
        非 git 仓库 / 名称非法 / git 失败 → ok=False（调用方据此降级）。
        """
        safe = _safe_name(name)
        if safe is None:
            return {"ok": False, "reason": f"worktree 名非法: {name!r}"}
        if not self.is_git_repo():
            return {"ok": False, "reason": "当前项目不是 git 仓库，隔离工作树不可用"}

        wt = self.path_of(safe)
        branch = self.branch_of(safe)
        if os.path.exists(wt):
            # 已存在（上次未处理）：直接复用，不重复创建
            return {"ok": True, "path": wt, "branch": branch, "reason": "复用已存在的 worktree"}

        os.makedirs(self._root(), exist_ok=True)
        r = self._git(self._ws, "worktree", "add", "-b", branch, wt)
        if r.returncode != 0:
            # 分支已存在（上次残留）→ 尝试直接检出到该分支
            r2 = self._git(self._ws, "worktree", "add", wt, branch)
            if r2.returncode != 0:
                logger.warning("[Worktree] 创建失败: %s", (r.stderr or r2.stderr)[:300])
                return {"ok": False, "reason": f"git worktree 创建失败: {(r.stderr or '')[:200]}"}

        self._link_dependencies(wt)
        logger.info("[Worktree] 已创建 %s（分支 %s）", wt, branch)
        return {"ok": True, "path": wt, "branch": branch, "reason": ""}

    def _link_dependencies(self, wt: str) -> None:
        """把主工作区被 gitignore 的常见依赖 symlink 进 worktree。

        symlink 而非拷贝：零开销，且与主工作区共享同一份 node_modules/.venv
        （依赖版本天然一致）。跨文件系统 / 已存在 → 跳过。
        """
        for rel in self._deps_to_link():
            src = os.path.join(self.workspace or "", rel)
            dst = os.path.join(wt, rel)
            if not os.path.exists(src) or os.path.exists(dst) or os.path.islink(dst):
                continue
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                os.symlink(src, dst)
            except OSError as exc:
                logger.debug("[Worktree] symlink 跳过 %s: %s", rel, exc)

    def _deps_to_link(self) -> List[str]:
        """默认依赖列表 + 项目根 `.worktreeinclude` 里列出的条目（若存在）。"""
        deps = list(DEFAULT_LINK_DEPS)
        inc = os.path.join(self.workspace or "", WORKTREE_INCLUDE_FILE)
        try:
            if os.path.isfile(inc):
                with open(inc, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            deps.append(line)
        except OSError:
            pass
        # 去重保序
        seen = set()
        out: List[str] = []
        for d in deps:
            if d not in seen:
                seen.add(d)
                out.append(d)
        return out

    # ------------------------------------------------------------ 收割 / 状态

    def _unstage_dep_symlinks(self, wt: str) -> None:
        """把自动 symlink 进来的依赖从暂存区移除。

        它们不是 Agent 的改动，若混进 patch 会污染主工作区（甚至把 .venv 之类
        指向自身的 symlink 覆盖回去）。仅针对**我们创建的 symlink**，避免误伤
        仓库里真实跟踪的同名文件。
        """
        deps = [rel for rel in self._deps_to_link()
                if os.path.islink(os.path.join(wt, rel))]
        if deps:
            self._git(wt, "reset", "-q", "--", *deps)

    def harvest(self, name: str) -> Dict[str, Any]:
        """收集 worktree 改动：{"files": [(status, path)], "patch": str, "adds": int, "dels": int}。"""
        wt = self.path_of(name)
        if not os.path.isdir(wt):
            return {"files": [], "patch": "", "adds": 0, "dels": 0}
        self._git(wt, "add", "-A")
        self._unstage_dep_symlinks(wt)
        # 依赖 symlink 虽已移出暂存区，仍会以未跟踪身份出现在 status 里——
        # 它们不是 Agent 的改动，文件列表一并过滤
        dep_links = {rel for rel in self._deps_to_link()
                     if os.path.islink(os.path.join(wt, rel))}
        st = self._git(wt, "status", "--porcelain")
        files: List[Tuple[str, str]] = []
        for ln in st.stdout.splitlines():
            if not ln.strip():
                continue
            status = ln[:2].strip() or "M"
            path = ln[3:].strip().strip('"')
            files.append((status, path))
        # 忽略 worktree 自身目录（防御性）与自动链接的依赖
        files = [(s, p) for s, p in files
                 if not p.startswith(WORKTREE_DIR) and p not in dep_links]
        numstat = self._git(wt, "diff", "--cached", "--numstat")
        adds = dels = 0
        for ln in numstat.stdout.splitlines():
            parts = ln.split("\t")
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                adds += int(parts[0])
                dels += int(parts[1])
        diff = self._git(wt, "diff", "--cached", "--binary")
        return {"files": files, "patch": diff.stdout, "adds": adds, "dels": dels}

    def diff_text(self, name: str) -> str:
        """供评审卡「查看 diff」：人类可读的 unified diff（非二进制）。"""
        wt = self.path_of(name)
        if not os.path.isdir(wt):
            return ""
        self._git(wt, "add", "-A")
        self._unstage_dep_symlinks(wt)
        return self._git(wt, "diff", "--cached").stdout

    def status(self, name: str) -> Dict[str, Any]:
        """worktree 当前状态摘要（供前端轮询）。"""
        wt = self.path_of(name)
        exists = os.path.isdir(wt)
        if not exists:
            return {"exists": False, "files": [], "adds": 0, "dels": 0, "branch": self.branch_of(name)}
        h = self.harvest(name)
        return {
            "exists": True,
            "branch": self.branch_of(name),
            "path": wt,
            "files": [{"status": s, "path": p} for s, p in h["files"]],
            "adds": h["adds"],
            "dels": h["dels"],
        }

    # ------------------------------------------------------------ 合并 / 丢弃

    def merge(self, name: str) -> Dict[str, Any]:
        """把 worktree 改动应用回主工作区（--3way 优先）。

        成功 → 保留 worktree（供用户确认后丢弃）由调用方决定清理。
        冲突 → ok=False + conflicts 列表，worktree 保留供人工/Agent 处理。
        """
        h = self.harvest(name)
        patch = h["patch"]
        if not patch.strip():
            return {"ok": True, "files": [], "merged": 0, "reason": "无改动"}
        # 先落一份补丁备份，冲突时用户仍可手动 git apply
        patch_path = self._save_patch(name, patch)
        for extra in (["--3way"], []):
            r = self._git(self._ws, "apply", *extra, "-", input_text=patch)
            if r.returncode == 0:
                return {
                    "ok": True,
                    "merged": len(h["files"]),
                    "files": [p for _, p in h["files"]],
                    "adds": h["adds"], "dels": h["dels"],
                    "patch_path": patch_path,
                }
        # 冲突：git apply --3way 会留下冲突标记/暂存，返回冲突文件供提示
        conflicts = [p for _, p in h["files"]]
        return {
            "ok": False,
            "reason": "合并冲突：主工作区有冲突改动，请手动解决（补丁已保存）",
            "conflicts": conflicts,
            "patch_path": patch_path,
        }

    def discard(self, name: str) -> Dict[str, Any]:
        """丢弃 worktree：删目录 + 删分支，主工作区毫发无伤。"""
        wt = self.path_of(name)
        branch = self.branch_of(name)
        if os.path.isdir(wt):
            self._git(self._ws, "worktree", "remove", "--force", wt)
        # remove --force 失败时（目录被占用）兜底删目录 + prune
        if os.path.isdir(wt):
            shutil.rmtree(wt, ignore_errors=True)
            self._git(self._ws, "worktree", "prune")
        self._git(self._ws, "branch", "-D", branch)
        logger.info("[Worktree] 已丢弃 %s（分支 %s）", wt, branch)
        return {"ok": True, "branch": branch}

    def cleanup(self, name: str) -> None:
        """内部收尾（同 discard，不抛异常）。"""
        try:
            self.discard(name)
        except Exception:  # noqa: BLE001
            logger.debug("[Worktree] 清理失败: %s", name, exc_info=True)

    def _save_patch(self, name: str, patch: str) -> str:
        patch_dir = os.path.join(self.workspace or ".", WORKTREE_DIR, "_patches")
        os.makedirs(patch_dir, exist_ok=True)
        path = os.path.join(patch_dir, f"{name}.patch")
        with open(path, "w", encoding="utf-8") as f:
            f.write(patch)
        return path

    # ------------------------------------------------------------ 枚举 / 暂存

    def list_active(self) -> List[Dict[str, Any]]:
        """列出磁盘上存在的 worktree（供侧边栏/恢复）。"""
        root = self._root()
        if not os.path.isdir(root):
            return []
        out: List[Dict[str, Any]] = []
        for name in sorted(os.listdir(root)):
            wt = os.path.join(root, name)
            if not os.path.isdir(wt) or name == "_patches":
                continue
            s = self.status(name)
            out.append({"name": name, **s})
        return out

    def stash_workspace(self, message: str) -> bool:
        """中途进入 worktree 前暂存主工作区改动（干净则跳过）。"""
        if not self.is_dirty():
            return False
        r = self._git(self._ws, "stash", "push", "-u", "-m", message)
        return r.returncode == 0

    def pop_stash(self) -> bool:
        """退出 worktree 后恢复暂存的主工作区改动。"""
        r = self._git(self._ws, "stash", "pop")
        return r.returncode == 0
