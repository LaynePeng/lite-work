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

# 未跟踪文件行数统计的大小上限（未跟踪文件不在 git diff 里，行数由本地计数补齐）
MAX_LINE_COUNT_BYTES = 1_000_000


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

    def _not_ready(self) -> bool:
        """未打开项目（或目录不存在）：所有公开操作应安全返回，而非 assert/报 500。"""
        return not self.workspace or not os.path.isdir(self.workspace)

    def is_git_repo(self) -> bool:
        if self._not_ready():
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

    def exists(self, name: str) -> bool:
        """worktree 目录是否仍在磁盘上。"""
        return os.path.isdir(self.path_of(name))

    def branch_exists(self, name: str) -> bool:
        """分支 worktree-<name> 是否仍存在（目录被删但分支还在 → 可恢复）。"""
        r = self._git(self._ws, "rev-parse", "-q", "--verify", self.branch_of(name))
        return r.returncode == 0

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
        if self._not_ready():
            return {"ok": False, "reason": "未打开项目，隔离工作树不可用"}
        if not self.is_git_repo():
            return {"ok": False, "reason": "当前项目不是 git 仓库，隔离工作树不可用"}

        wt = self.path_of(safe)
        branch = self.branch_of(safe)
        if os.path.exists(wt):
            # 已存在（上次未处理）：直接复用，不重复创建
            return {"ok": True, "path": wt, "branch": branch, "reused": True, "restored": False,
                    "reason": "复用已存在的 worktree"}

        os.makedirs(self._root(), exist_ok=True)
        had_branch = self.branch_exists(safe)
        r = self._git(self._ws, "worktree", "add", "-b", branch, wt)
        restored = False
        if r.returncode != 0:
            # 分支已存在（目录曾被删/清理）→ 挂回原分支 = **真正的恢复**
            r2 = self._git(self._ws, "worktree", "add", wt, branch)
            if r2.returncode != 0:
                logger.warning("[Worktree] 创建失败: %s", (r.stderr or r2.stderr)[:300])
                return {"ok": False, "reason": f"git worktree 创建失败: {(r.stderr or '')[:200]}"}
            restored = True

        self._link_dependencies(wt)
        logger.info("[Worktree] %s %s（分支 %s）",
                    "已恢复" if restored else "已创建", wt, branch)
        return {"ok": True, "path": wt, "branch": branch, "reused": False,
                "restored": restored, "had_branch": had_branch, "reason": ""}

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

    # ------------------------------------------------------------ 收割 / 状态

    def _main_branch(self) -> str:
        """主工作区当前分支名（detached 时返回 HEAD）。"""
        r = self._git(self._ws, "rev-parse", "--abbrev-ref", "HEAD")
        return (r.stdout or "").strip() or "HEAD"

    def _base_commit(self, name: str) -> str:
        """worktree 分支的基点：与主分支的 merge-base（用于「本分支改了什么」）。"""
        r = self._git(self._ws, "merge-base", self._main_branch(), self.branch_of(name))
        return (r.stdout or "").strip()

    def _count_lines(self, abs_path: str) -> Optional[int]:
        """数文本文件行数（未跟踪文件不在 git diff 里，用它补齐 +/- 统计）。

        二进制（前 8KB 含 NUL）或超过 MAX_LINE_COUNT_BYTES → None（不计数）。
        """
        try:
            if os.path.getsize(abs_path) > MAX_LINE_COUNT_BYTES:
                return None
            with open(abs_path, "rb") as f:
                data = f.read(MAX_LINE_COUNT_BYTES + 1)
        except OSError:
            return None
        if len(data) > MAX_LINE_COUNT_BYTES or b"\x00" in data[:8192]:
            return None
        if not data:
            return 0
        return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)

    def _snapshot(self, wt: str, base: str) -> Dict[str, Any]:
        """汇总 worktree 的**有效改动**（未提交 + 分支上已提交，相对 base）。

        变更来源 = 「跟踪文件相对 base 的 diff」 ∪ 「未跟踪文件（status ??）」。
        刻意**不执行 `git add -A`**：status/harvest 之外不该改动 worktree 的索引，
        否则会干扰 Agent 自己在 worktree 里跑的 git 命令（此前 status() 会偷偷
        把文件加进暂存区）。未跟踪文件不在 diff 里，故其行数用本地计数补齐。
        """
        dep_links = {rel for rel in self._deps_to_link()
                     if os.path.islink(os.path.join(wt, rel))}
        pending: List[Tuple[str, str]] = []
        untracked: List[str] = []
        st = self._git(wt, "status", "--porcelain")
        for ln in st.stdout.splitlines():
            if not ln.strip():
                continue
            code = ln[:2]
            path = ln[3:].strip().strip('"')
            if path.startswith(WORKTREE_DIR) or path in dep_links:
                continue
            pending.append((code.strip() or "M", path))
            if code == "??":
                untracked.append(path)
        files: List[Tuple[str, str]] = []
        adds = dels = 0
        if base:
            ns = self._git(wt, "diff", "--name-status", base)
            for ln in ns.stdout.splitlines():
                parts = ln.split("\t")
                if len(parts) >= 2:
                    files.append((parts[0].strip() or "M", parts[-1].strip()))
            ns2 = self._git(wt, "diff", "--numstat", base)
            for ln in ns2.stdout.splitlines():
                parts = ln.split("\t")
                if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                    adds += int(parts[0])
                    dels += int(parts[1])
        files = [(st_, p) for st_, p in files
                 if not p.startswith(WORKTREE_DIR) and p not in dep_links]
        # 未跟踪文件：状态记为 A，行数本地计数补齐（二进制/超大不计）
        for p in untracked:
            files.append(("A", p))
            n = self._count_lines(os.path.join(wt, p))
            if n is not None:
                adds += n
        return {"files": files, "pending": pending, "adds": adds, "dels": dels}

    def harvest(self, name: str) -> Dict[str, Any]:
        """把 worktree 的改动**提交到分支**（不再生成 patch），返回变更摘要。

        git 原生工作流：分支上留下真实提交，用户可用 `git log` / `git diff`
        审查，合并走 `git merge`（保留历史与来源）。Agent 自己提交过的改动也会
        被计入（`git add -A` 只补提交剩余部分；无剩余则不产生空提交）。
        """
        wt = self.path_of(name)
        if not os.path.isdir(wt):
            return {"files": [], "pending": [], "adds": 0, "dels": 0, "commits": 0, "committed": False}
        base = self._base_commit(name)
        snap = self._snapshot(wt, base)
        committed = False
        if snap["pending"]:
            # 收割（提交）是冷路径：这里才需要 add -A；依赖 symlink 不进提交
            self._git(wt, "add", "-A")
            self._unstage_dep_symlinks(wt)
            msg = f"lite-work: 隔离工作树改动（{name}）\n\n由 lite-work 自动提交，便于审查与合并。"
            r = self._git(wt, "commit", "-q", "-m", msg)
            committed = r.returncode == 0
            if committed:
                snap = self._snapshot(wt, self._base_commit(name))
        commits = 0
        cnt = self._git(wt, "rev-list", "--count", f"{base}..HEAD") if base else None
        if cnt is not None and cnt.stdout.strip().isdigit():
            commits = int(cnt.stdout.strip())
        return {**snap, "commits": commits, "committed": committed}

    def diff_text(self, name: str) -> str:
        """供评审卡「查看 diff」：相对基点的完整 unified diff（含未提交改动）。"""
        if self._not_ready():
            return ""
        wt = self.path_of(name)
        if not os.path.isdir(wt):
            return ""
        base = self._base_commit(name)
        if not base:
            return ""
        # 不 add（不动 index）；`git diff <base>` 即工作区相对基点（含已提交+未提交）
        return self._git(wt, "diff", base).stdout

    def status(self, name: str) -> Dict[str, Any]:
        """worktree 当前状态摘要（供前端轮询）。

        额外带出**合并目标与落后信息**：``main_branch``（合并会进到哪个分支）、
        ``base``（本分支基点）、``behind``（主分支已前进 → 合并可能需要解决冲突）。
        多会话同项目时，别的会话合并完主分支后，这里能让本会话看到「我基于旧提交」。
        """
        if self._not_ready():
            return {"exists": False, "files": [], "adds": 0, "dels": 0,
                    "branch": self.branch_of(name), "main_branch": "", "main_head": "",
                    "behind": False}
        wt = self.path_of(name)
        main_branch = self._main_branch()
        main_head = (self._git(self._ws, "rev-parse", "HEAD").stdout or "").strip()
        if not os.path.isdir(wt):
            return {"exists": False, "files": [], "adds": 0, "dels": 0,
                    "branch": self.branch_of(name),
                    "main_branch": main_branch, "main_head": main_head, "behind": False}
        base = self._base_commit(name)
        snap = self._snapshot(wt, base)
        cnt = self._git(wt, "rev-list", "--count", f"{base}..HEAD") if base else None
        commits = int(cnt.stdout.strip()) if (cnt is not None and cnt.stdout.strip().isdigit()) else 0
        head = (self._git(wt, "rev-parse", "HEAD").stdout or "").strip()
        return {
            "exists": True,
            "branch": self.branch_of(name),
            "path": wt,
            "head": head,
            "files": [{"status": s, "path": p} for s, p in snap["files"]],
            "pending": [{"status": s, "path": p} for s, p in snap["pending"]],
            "adds": snap["adds"],
            "dels": snap["dels"],
            "commits": commits,
            # 合并目标 = 主工作区当前分支；base 与 main_head 不同 → 主分支已前进
            "main_branch": main_branch,
            "main_head": main_head,
            "base": base,
            "behind": bool(base and main_head and base != main_head),
        }

    # ------------------------------------------------------------ 合并 / 丢弃

    def merge(self, name: str) -> Dict[str, Any]:
        """把 worktree 分支**合并**回主工作区（`git merge --no-ff`，保留来源）。

        - 先把 worktree 的改动提交到分支（harvest）——分支上留下真实提交；
        - 在主工作区执行 `git merge --no-ff -m ...`：产生一个可追溯的 merge commit；
        - 冲突 → 保持合并中状态（主工作区带冲突标记），返回冲突文件列表，
          worktree 保留；用户可解决后提交，或用 abort_merge() 放弃。

        成功由调用方决定是否清理 worktree（当前策略：成功即清理）。
        """
        if self._not_ready():
            return {"ok": False, "reason": "未打开项目"}
        wt = self.path_of(name)
        if not os.path.isdir(wt):
            return {"ok": False, "reason": "worktree 不存在"}
        h = self.harvest(name)  # 提交待提交改动到分支
        if not h["files"]:
            return {"ok": True, "merged": 0, "files": [], "reason": "无改动"}
        branch = self.branch_of(name)
        # 主工作区必须在某个分支上（detached 无法 merge）
        main_branch = self._main_branch()
        if main_branch == "HEAD":
            return {"ok": False, "reason": "主工作区处于 detached HEAD，无法合并（请先切到分支）"}
        msg = f"lite-work: 合并隔离工作树（{name}）"
        r = self._git(self._ws, "merge", "--no-ff", "--no-edit", "-m", msg, branch)
        if r.returncode == 0:
            return {
                "ok": True,
                "merged": len(h["files"]),
                "files": [p for _, p in h["files"]],
                "adds": h["adds"], "dels": h["dels"],
                "commits": h["commits"],
                "out": (r.stdout or "").strip(),
            }
        # 冲突（或其它失败）：收集未合并路径
        conflicts: List[str] = []
        u = self._git(self._ws, "diff", "--name-only", "--diff-filter=U")
        conflicts = [x for x in u.stdout.splitlines() if x.strip()]
        if not conflicts:
            # 非冲突失败（如主工作区有未提交改动挡住合并）→ 原样回传 git 信息
            return {
                "ok": False,
                "reason": f"合并失败：{(r.stderr or r.stdout or '').strip()[:300]}",
                "conflicts": [],
            }
        return {
            "ok": False,
            "reason": f"合并冲突：{len(conflicts)} 个文件需要手动解决",
            "conflicts": conflicts,
        }

    def has_merge_in_progress(self) -> bool:
        """主工作区是否处于「合并中」状态（存在冲突待解决）。"""
        if self._not_ready():
            return False
        r = self._git(self._ws, "rev-parse", "-q", "--verify", "MERGE_HEAD")
        return r.returncode == 0

    def is_branch_merged(self, name: str) -> bool:
        """分支是否已完全并入主工作区当前分支（用于合并后清理判定）。"""
        if self._not_ready():
            return False
        r = self._git(self._ws, "merge-base", "--is-ancestor", self.branch_of(name), "HEAD")
        return r.returncode == 0

    def merge_conflicts(self) -> List[str]:
        """主工作区当前未合并（冲突）的文件列表（非合并中则为空）。"""
        if self._not_ready() or not self.has_merge_in_progress():
            return []
        r = self._git(self._ws, "diff", "--name-only", "--diff-filter=U")
        return [x for x in r.stdout.splitlines() if x.strip()]

    def abort_merge(self) -> Dict[str, Any]:
        """放弃进行中的合并（`git merge --abort`），主工作区回到合并前状态。"""
        if not self.has_merge_in_progress():
            return {"ok": True, "reason": "没有进行中的合并"}
        r = self._git(self._ws, "merge", "--abort")
        return {"ok": r.returncode == 0, "reason": (r.stderr or "").strip()[:200]}

    def discard(self, name: str) -> Dict[str, Any]:
        """丢弃 worktree：删目录 + 删分支，主工作区毫发无伤。"""
        if self._not_ready():
            return {"ok": False, "reason": "未打开项目"}
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

    # ------------------------------------------------------------ 枚举 / 暂存

    def list_active(self) -> List[Dict[str, Any]]:
        """列出磁盘上存在的 worktree（供侧边栏/恢复）。"""
        if self._not_ready():
            return []
        root = self._root()
        if not os.path.isdir(root):
            return []
        out: List[Dict[str, Any]] = []
        for name in sorted(os.listdir(root)):
            wt = os.path.join(root, name)
            if not os.path.isdir(wt):
                continue
            s = self.status(name)
            out.append({"name": name, **s})
        return out
