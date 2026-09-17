# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""项目与用户技能发现、导入管理与按需加载工具。

标准技能结构：目录内含 SKILL.md（YAML frontmatter + Markdown 正文）。
frontmatter 兼容 OpenCode 规范：name/description 必填（校验宽松：不符仅告警）；
lite-work 扩展 `triggers`（逗号分隔关键词，命中自动注入）。未知字段忽略。

可写根目录仅限 `.agents/skills`（工作区与用户家目录）——`.claude/.opencode`
等目录属于第三方工具，一律只读，防误删。
"""
from __future__ import annotations

import io
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.types import ToolDefinition
from .install_progress import checkpoint

logger = logging.getLogger("litework.tools.skills")

# OpenCode 同款名称规范（不符仅告警不拒绝，避免阻断导入第三方技能）
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
NAME_MAX_LEN = 64
DESC_MAX_LEN = 1024
# 导入大小限制（社区仓库含大型技能如 ppt-master：12000+ 文件 / 60MB+，
# 无 git 用户的 zipball 回退按整仓 zip 判定，上限需覆盖整仓而非单技能）
ZIP_MAX_BYTES = 150 * 1024 * 1024
ZIP_MAX_ENTRIES = 20000
# GitHub 导入仅允许这些主机（防 SSRF）
GITHUB_ALLOWED_HOSTS = {"github.com", "api.github.com", "codeload.github.com", "raw.githubusercontent.com"}

# 工作区/用户级根目录（相对后缀）
WORKSPACE_ROOT_SUFFIXES = (
    (".agents/skills", True),    # 可写
    (".claude/skills", False),   # 第三方，只读
    (".opencode/skills", False),
    ("skills", False),
)
USER_ROOT_SUFFIXES = (
    (".agents/skills", True),
    (".claude/skills", False),
    (".config/opencode/skills", False),
)


def _safe_name(name: str) -> Optional[str]:
    """技能目录名安全校验：防路径穿越与非法字符，返回规范化名或 None。"""
    name = (name or "").strip().strip("/")
    if not name or len(name) > 100:
        return None
    if name in (".", "..") or "\\" in name or ":" in name:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return None
    return name


def parse_frontmatter(text: str) -> Dict[str, Any]:
    """轻量 YAML frontmatter 解析（不引入 pyyaml）。

    支持扁平 `key: value`、一层嵌套 map（`metadata:` 下缩进键值）
    与多行块标量（`|` 字面块 / `>` 折叠块，作为字符串值收集）。
    未知字段由调用方决定取舍。
    """
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    meta: Dict[str, Any] = {}
    current_key: Optional[str] = None
    i, n = 1, len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if stripped == "---":
            break
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if line.startswith((" ", "\t")) and current_key:
            # 嵌套 map（一层）
            if ":" in stripped:
                k, _, v = stripped.partition(":")
                nested = meta.setdefault(current_key, {})
                if isinstance(nested, dict):
                    nested[k.strip()] = v.strip().strip("'\"")
            i += 1
            continue
        if ":" in stripped:
            key, _, value = stripped.partition(":")
            key, value = key.strip(), value.strip()
            if value in ("|", ">"):
                # 块标量：收集后续缩进行作为字符串值
                block: List[str] = []
                j = i + 1
                while j < n and lines[j].startswith((" ", "\t")):
                    block.append(lines[j].strip())
                    j += 1
                if value == ">":
                    # 折叠块：非空行用空格连接（近似 YAML 折叠语义）
                    meta[key] = " ".join(block)
                else:
                    # 字面块：保留换行
                    meta[key] = "\n".join(block)
                current_key = None
                i = j
                continue
            if value == "":
                meta[key] = {}
                current_key = key
            else:
                meta[key] = value.strip("'\"")
                current_key = None
        i += 1
    return meta


def _builtin_skills_dir() -> Optional[Path]:
    """定位随产品分发的内置技能目录（skills/）。

    项目是动态创建/切换的，内置技能不能只依赖 workspace 下的 skills/：
    - 打包态：PyInstaller --add-data 落点（不同版本 _MEIPASS 可能是
      _internal/ 或可执行文件目录，全部候选穷举）
    - 开发态：仓库根 skills/（litework 包的上一级）
    """
    try:
        import sys as _sys
        candidates: List[Path] = []
        meipass = getattr(_sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "skills")
            candidates.append(Path(meipass) / "_internal" / "skills")
        if getattr(_sys, "frozen", False):
            exe_dir = Path(_sys.executable).resolve().parent
            candidates.append(exe_dir / "skills")
            candidates.append(exe_dir / "_internal" / "skills")
        # 开发态：litework/tools/skills.py → litework 包 → 仓库根/skills
        candidates.append(Path(__file__).resolve().parent.parent.parent / "skills")
        for cand in candidates:
            if cand.is_dir() and any(
                (d / "SKILL.md").is_file() for d in cand.iterdir() if d.is_dir()
            ):
                return cand
    except Exception:
        pass
    return None


# 随包分发的技能解释器目录名（python-build-standalone，见 scripts/package-backend.mjs）
SKILL_PYTHON_DIRNAME = "skill-python"


def _builtin_skill_python() -> Optional[Path]:
    """定位随包分发的技能解释器可执行文件；未随包分发时返回 None。

    打包态落点由 PyInstaller --add-data 决定（_MEIPASS/_internal），开发态看
    仓库根是否放了 release/skill-python/python（构建脚本的缓存位置）。
    """
    candidates: List[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / SKILL_PYTHON_DIRNAME)
        candidates.append(Path(meipass) / "_internal" / SKILL_PYTHON_DIRNAME)
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / SKILL_PYTHON_DIRNAME)
        candidates.append(exe_dir / "_internal" / SKILL_PYTHON_DIRNAME)
        # PyInstaller 6 的 onedir：可执行文件与 _internal 同级
        candidates.append(exe_dir / "_internal" / ".." / SKILL_PYTHON_DIRNAME)
    root = Path(__file__).resolve().parent.parent.parent
    candidates.append(root / "release" / SKILL_PYTHON_DIRNAME / "python")
    candidates.append(root / SKILL_PYTHON_DIRNAME)
    for base in candidates:
        for rel in ("bin/python3", "python3", "python.exe", "bin/python"):
            exe = base / rel
            if exe.is_file():
                return exe
    return None


def use_builtin_skill_python() -> Optional[Path]:
    """把随包技能解释器接入子进程环境；返回解释器路径（没有则 None）。

    做两件事（幂等）：
    - `LITEWORK_SKILL_PYTHON` 指向该解释器，SKILL.md 里可用它显式调用；
    - **打包态**把解释器所在 bin 前置进 PATH —— 技能脚本里的 `python3 ...` 直接
      命中它，从而与用户系统 Python 的版本/已装包无关，离线可用。

    开发态默认不动 PATH（仍走系统 python3，保持 npm run dev 的既有行为）；
    需要验证打包行为时设 `LITEWORK_SKILL_PYTHON_FORCE=1`。
    未随包分发（旧包）时返回 None，行为与旧版一致。
    """
    exe = _builtin_skill_python()
    if exe is None:
        return None
    os.environ.setdefault("LITEWORK_SKILL_PYTHON", str(exe))
    force = os.environ.get("LITEWORK_SKILL_PYTHON_FORCE") == "1"
    if not (getattr(sys, "frozen", False) or force):
        return exe
    bindir = str(exe.parent)
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if bindir not in parts:
        os.environ["PATH"] = bindir + os.pathsep + os.environ.get("PATH", "")
    return exe


# 内置技能安装到用户级目录的标记文件（内容为安装时的产品版本）
BUILTIN_SKILL_MARKER = ".litework-builtin"


def sync_builtin_skills_to_user() -> int:
    """把内置技能同步安装到 ~/.agents/skills/（用户级标准位置）。

    为什么必须装到用户目录：内置位置（开发态=仓库 skills/、打包态=
    _internal/skills/）对 Agent 不稳定——换机器/升级应用后路径漂移，
    SKILL.md 里的脚本相对路径无从解析。用户级目录 ~/.agents/skills/
    持久稳定且 SkillsTools 原生扫描。

    同步策略（幂等，启动时执行）：
    - 目标不存在 → 安装；
    - 目标带 .litework-builtin 标记（我们装的）→ 版本变化时覆盖升级；
    - 用户自有的同名技能（无标记）→ 不动。
    返回本次安装/升级的技能数。
    """
    import shutil as _shutil

    from litework import __version__

    builtin = _builtin_skills_dir()
    if builtin is None:
        return 0
    user_root = Path.home() / ".agents" / "skills"
    installed = 0
    try:
        for skill_dir in builtin.iterdir():
            if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").is_file():
                continue
            target = user_root / skill_dir.name
            marker = target / BUILTIN_SKILL_MARKER
            need_install = False
            if not target.is_dir():
                need_install = True
            elif marker.is_file():
                # 我们装的：版本变化才升级
                try:
                    need_install = marker.read_text(encoding="utf-8").strip() != __version__
                except OSError:
                    need_install = True
            # 用户自有同名技能（无标记）→ 不覆盖
            if not need_install:
                continue
            user_root.mkdir(parents=True, exist_ok=True)
            _shutil.copytree(
                skill_dir, target,
                dirs_exist_ok=True,
                # node_modules（引擎，可达数百 MB）不拷贝：留在安装包内置
                # 位置直接用（LITEWORK_SKILLS_SOURCE 指路），避免双倍占盘
                # 与启动阻塞
                ignore=_shutil.ignore_patterns(
                    "__pycache__", BUILTIN_SKILL_MARKER, "node_modules"),
            )
            marker.write_text(__version__, encoding="utf-8")
            installed += 1
    except Exception:
        if installed:
            invalidate_skills_cache()
        return installed
    if installed:
        invalidate_skills_cache()
    return installed


# 技能快照缓存：`list_skills()` 要遍历所有技能根、逐个读 SKILL.md 解析 frontmatter，
# 而它处在热路径上（每轮 System Prompt 技能索引、每任务工具白名单、命令面板、API）。
# 用「根目录下各 SKILL.md 的 (名字, mtime_ns, size) 签名」做键：
# 新增/删除/改名/编辑技能都会改变签名 → 自动失效，无需手工埋点；内容不变则零解析。
SKILLS_CACHE_MAX = 32
_SKILLS_CACHE: Dict[str, Tuple[Tuple[Any, ...], List[Dict[str, Any]]]] = {}


def _skills_signature(roots: List[Dict[str, Any]]) -> Tuple[Any, ...]:
    sig: List[Any] = []
    for root in roots:
        base = root["path"]
        sig.append((str(base), root["scope"]))
        if not base.is_dir():
            continue
        try:
            entries = sorted(base.iterdir())
        except OSError:
            continue
        for skill_dir in entries:
            skill_file = skill_dir / "SKILL.md"
            try:
                st = skill_file.stat()
            except OSError:
                continue
            if st.st_size == 0:
                continue
            sig.append((skill_dir.name, st.st_mtime_ns, st.st_size))
    return tuple(sig)


def invalidate_skills_cache() -> None:
    """清空技能快照缓存（安装/删除/同步内置技能后调用；签名一般已能自动失效）。"""
    _SKILLS_CACHE.clear()


class SkillsTools:
    def __init__(self, workspace: Optional[str]) -> None:
        self.workspace = Path(workspace).resolve() if workspace else None
        self.roots: List[Dict[str, Any]] = []
        # 内置技能根：提前计算，用于跳过工作区中与内置重合的只读路径
        builtin = _builtin_skills_dir()
        if self.workspace:
            for suffix, writable in WORKSPACE_ROOT_SUFFIXES:
                path = self.workspace / suffix
                # 跳过与内置技能根目录重合的 workspace 条目（如 dev 态 lite-work
                # 仓库自身的 skills/），避免只读副本影子用户已更新的版本
                if builtin and path.resolve() == builtin.resolve():
                    continue
                self.roots.append({
                    "path": path, "scope": "workspace", "writable": writable,
                })
        home = Path.home()
        for suffix, writable in USER_ROOT_SUFFIXES:
            self.roots.append({
                "path": home / suffix, "scope": "user", "writable": writable,
            })
        # 产品内置技能根（随包分发，任何 workspace 都可见；只读）。
        # 优先级最低：workspace / user 同名技能覆盖内置版。
        if builtin:
            self.roots.append({
                "path": builtin, "scope": "builtin", "writable": False,
            })

    # ------------------------------------------------------------ 发现

    def _skills(self) -> Dict[str, Path]:
        found: Dict[str, Path] = {}
        for root in self.roots:
            base = root["path"]
            if not base.is_dir():
                continue
            for skill_dir in sorted(base.iterdir()):
                skill_file = skill_dir / "SKILL.md"
                if skill_dir.is_dir() and skill_file.is_file():
                    found.setdefault(skill_dir.name, skill_file)
        return found

    def list_skills(self) -> List[Dict[str, Any]]:
        """结构化技能列表（带签名缓存，见模块顶部 _SKILLS_CACHE）。

        返回的每个 dict 都是副本：调用方（app.skills_list 会补 permission 字段）
        可以随意改，不会污染缓存。
        """
        key = str(self.workspace or "")
        sig = _skills_signature(self.roots)
        hit = _SKILLS_CACHE.get(key)
        if hit is not None and hit[0] == sig:
            return [dict(d) for d in hit[1]]
        out = self._list_skills_uncached()
        _SKILLS_CACHE[key] = (sig, out)
        if len(_SKILLS_CACHE) > SKILLS_CACHE_MAX:
            _SKILLS_CACHE.pop(next(iter(_SKILLS_CACHE)))
        return [dict(d) for d in out]

    def _list_skills_uncached(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        seen: set = set()
        for root in self.roots:
            base = root["path"]
            if not base.is_dir():
                continue
            for skill_dir in sorted(base.iterdir()):
                skill_file = skill_dir / "SKILL.md"
                if not (skill_dir.is_dir() and skill_file.is_file()):
                    continue
                raw_text = skill_file.read_text(encoding="utf-8", errors="replace")
                meta = parse_frontmatter(raw_text)
                description = str(meta.get("description") or "")
                if not description:
                    # 兼容无 frontmatter 的朴素技能：裸 `description:` 行
                    for line in raw_text.splitlines():
                        if line.strip().lower().startswith("description:"):
                            description = line.split(":", 1)[1].strip()
                            break
                name = str(meta.get("name") or skill_dir.name)
                if name in seen:
                    continue
                seen.add(name)
                out.append({
                    "name": name,
                    "description": description[:DESC_MAX_LEN],
                    "dirName": skill_dir.name,
                    "path": str(skill_file),
                    "scope": root["scope"],
                    "writable": bool(root["writable"]),
                    "triggers": str(meta.get("triggers") or ""),
                    # frontmatter 可选 version：社区技能更新对比用（缺失为空串）
                    "version": str(meta.get("version") or ""),
                })
        return out

    def read_skill(self, name: str) -> Optional[str]:
        path = self._skills().get(name)
        if path is None:
            return None
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"[Error]: 无法读取技能 {name!r}: {exc}"

    def match_skills(self, prompt: str, mode: str = "substring") -> List[Dict[str, Any]]:
        """triggers 自动匹配。
        mode="substring"：大小写不敏感子串命中即返回（旧行为）。
        mode="advanced"：词边界匹配（无修饰词）或正则匹配（/pattern/ 包裹）。
        """
        if mode == "advanced":
            return self._match_skills_advanced(prompt)
        # substring 模式（默认）
        lowered = (prompt or "").lower()
        matched: List[Dict[str, Any]] = []
        for skill in self.list_skills():
            triggers = skill.get("triggers") or ""
            for t in triggers.split(","):
                t = t.strip().lower()
                if t and t in lowered:
                    matched.append(skill)
                    break
        return matched

    def _match_skills_advanced(self, prompt: str) -> List[Dict[str, Any]]:
        """高级匹配：/pattern/ 为正则，其余为词边界（\\bword\\b）。"""
        matched: List[Dict[str, Any]] = []
        for skill in self.list_skills():
            triggers = skill.get("triggers") or ""
            for t in triggers.split(","):
                t = t.strip()
                if not t:
                    continue
                try:
                    if t.startswith("/") and t.endswith("/") and len(t) > 1:
                        # /regex/ 包裹 → 直接正则匹配
                        pattern = t[1:-1]
                        if re.search(pattern, prompt, re.I):
                            matched.append(skill)
                            break
                    else:
                        # 普通词 → 词边界匹配
                        pattern = re.escape(t)
                        if re.search(rf"\b{pattern}\b", prompt, re.I):
                            matched.append(skill)
                            break
                except re.error:
                    # 非法正则回退到词边界匹配
                    pattern = re.escape(t)
                    if re.search(rf"\b{pattern}\b", prompt, re.I):
                        matched.append(skill)
                        break
        return matched

    def index(self) -> str:
        rows = []
        for skill in self.list_skills():
            description = skill["description"] or "使用该技能目录中的 SKILL.md"
            rows.append(f"- {skill['name']}: {description}")
        return "\n".join(rows) or "（当前没有发现可用技能）"

    # ------------------------------------------------------------ 管理（仅可写根）

    def _writable_root(self, scope: str) -> Optional[Path]:
        for root in self.roots:
            if root["scope"] == scope and root["writable"]:
                return root["path"]
        return None

    def _resolve_target(self, name: str, scope: str) -> Path:
        root = self._writable_root(scope)
        if root is None:
            raise ValueError(f"scope {scope!r} 不可写（未打开项目时仅支持 user）")
        safe = _safe_name(name)
        if safe is None:
            raise ValueError(f"非法技能名: {name!r}")
        target = (root / safe).resolve()
        if not str(target).startswith(str(root.resolve())):
            raise ValueError("技能路径越界")
        return target

    def _validate_meta(self, meta: Dict[str, str], dir_name: str) -> None:
        name = meta.get("name", "")
        if name and (len(name) > NAME_MAX_LEN or not SKILL_NAME_RE.fullmatch(name)):
            logger.warning("[Skills] 技能 %s 的 frontmatter name 不符合 OpenCode 规范: %r", dir_name, name)
        desc = meta.get("description", "")
        if desc and len(desc) > DESC_MAX_LEN:
            logger.warning("[Skills] 技能 %s 的 description 超过 %d 字符", dir_name, DESC_MAX_LEN)

    def create_skill(self, name: str, description: str, scope: str = "workspace") -> Dict[str, Any]:
        meta = {"name": name, "description": description}
        self._validate_meta(meta, name)
        target = self._resolve_target(name, scope)
        if target.exists():
            raise ValueError(f"技能 {name!r} 已存在于 {target}")
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text(
            "---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            "triggers: \n"
            "---\n\n"
            f"# {name}\n\n"
            "（在此编写技能正文：流程、规则、示例。Agent 通过 load_skill 或 /skill 加载本文件。）\n",
            encoding="utf-8",
        )
        return {"ok": True, "name": name, "path": str(target / "SKILL.md"), "scope": scope}

    def update_skill(self, name: str, description: str, scope: str) -> Dict[str, Any]:
        """修改已有技能的 description（更新 SKILL.md frontmatter）。"""
        root = self._writable_root(scope)
        if root is None:
            raise ValueError(f"scope {scope!r} 不可写")
        safe = _safe_name(name)
        if safe is None:
            raise ValueError(f"非法技能名: {name!r}")
        target = (root / safe).resolve()
        if not str(target).startswith(str(root.resolve())):
            raise ValueError(f"非法技能名: {name!r}")
        skill_file = target / "SKILL.md"
        if not skill_file.is_file():
            raise ValueError(f"技能 {name!r} 不存在于 {scope} 技能目录")
        raw = skill_file.read_text(encoding="utf-8", errors="replace")
        # 定位 frontmatter 内的 description 字段（可能是单行或块标量多行）
        import re as _re
        lines = raw.splitlines(keepends=True)
        fm_end = 0
        in_fm = raw.startswith("---")
        if in_fm:
            for idx, ln in enumerate(lines):
                if idx > 0 and ln.strip() == "---":
                    fm_end = idx
                    break
        # 在 frontmatter 中找 description 起始行
        desc_idx = -1
        for idx, ln in enumerate(lines[: fm_end or len(lines)]):
            if _re.match(r"^description\s*:", ln):
                desc_idx = idx
                break
        if desc_idx >= 0:
            # 若为块标量（| 或 >），连同后续缩进行一起替换
            block_lines = 1
            if _re.search(r":\s*[|>]\s*$", lines[desc_idx]):
                for ln in lines[desc_idx + 1: fm_end or len(lines)]:
                    if ln.startswith((" ", "\t")):
                        block_lines += 1
                    else:
                        break
            new_raw = "".join(lines[:desc_idx]) + f"description: {description}\n" + "".join(lines[desc_idx + block_lines:])
        else:
            # 没有 description 行，在 --- 后插入
            new_raw = raw.replace("---\n", f"---\ndescription: {description}\n", 1)
        skill_file.write_text(new_raw, encoding="utf-8")
        return {"ok": True, "name": name, "path": str(skill_file), "scope": scope}

    def delete_skill(self, name: str, scope: str) -> Dict[str, Any]:
        root = self._writable_root(scope)
        if root is None:
            raise ValueError(f"scope {scope!r} 不可写")
        safe = _safe_name(name)
        if safe is None:
            raise ValueError(f"非法技能名: {name!r}")
        target = (root / safe).resolve()
        if not str(target).startswith(str(root.resolve())):
            raise ValueError(f"非法技能名: {name!r}")
        if not (target / "SKILL.md").is_file():
            raise ValueError(f"技能 {name!r} 不存在于 {scope} 技能目录")
        shutil.rmtree(target)
        return {"ok": True, "name": name}

    def import_skill(self, source: str, scope: str = "workspace", name: Optional[str] = None,
                      overwrite: bool = False) -> List[Dict[str, Any]]:
        """导入技能：source 为本地目录 / zip 文件路径 / GitHub URL。

        overwrite=True 时同名技能覆盖更新（保留本地 .env）。
        返回导入的技能列表（一个 zip/仓库可能包含多个技能）。
        """
        source = (source or "").strip()
        if source.startswith(("http://", "https://")):
            return self._import_from_github(source, scope, name, overwrite)
        p = Path(source).expanduser()
        if not p.exists():
            raise ValueError(f"路径不存在: {source}")
        if p.is_dir():
            return self._import_from_dir(p, scope, name, overwrite)
        if p.suffix.lower() == ".zip":
            return self._import_from_zip_file(p, scope, name, overwrite)
        raise ValueError(f"不支持的导入来源: {source}（支持目录 / .zip / GitHub URL）")

    # ------------------------------------------------------------ 目录导入

    def _find_skill_dirs(self, base: Path, depth: int = 0) -> List[Path]:
        """找出 base 下所有含 SKILL.md 的目录（深度限制 3 层）。"""
        found: List[Path] = []
        if depth > 3:
            return found
        try:
            if (base / "SKILL.md").is_file():
                found.append(base)
                return found  # 命中技能目录后不再下钻
            for child in sorted(base.iterdir()):
                if child.is_dir() and not child.name.startswith("."):
                    found.extend(self._find_skill_dirs(child, depth + 1))
        except OSError:
            pass
        return found

    def _install_skill_deps(self, target: Path) -> Dict[str, Any]:
        """安装技能依赖：Python 包（requirements.txt）、Node 包（package.json）、
        环境文件（.env.example → .env）。

        返回依赖安装报告，供前端展示。安装失败不会中断导入（仅报告）。
        """
        report: Dict[str, Any] = {"pip": None, "npm": None, "env": None}

        # 1. Python 依赖
        # 打包态 sys.executable 是 PyInstaller backend.exe（无 pip）；
        # 技能脚本本就用系统 Python 子进程执行，依赖也装到系统 Python
        req_file = target / "requirements.txt"
        if req_file.is_file():
            try:
                py = "python3"
                if getattr(sys, "frozen", False):
                    py = shutil.which("python3") or shutil.which("python") or ""
                    if not py:
                        report["pip"] = {
                            "ok": False,
                            "error": "未找到系统 Python（打包版技能依赖需系统 Python + pip）",
                        }
                        return report
                else:
                    py = sys.executable or "python3"
                r = subprocess.run(
                    [py, "-m", "pip", "install", "-r", str(req_file), "--quiet"],
                    capture_output=True, text=True, timeout=600,
                )
                report["pip"] = {
                    "ok": r.returncode == 0,
                    "stdout": r.stdout[:2000] if r.stdout else "",
                    "stderr": r.stderr[:2000] if r.stderr else "",
                }
                if r.returncode != 0:
                    logger.warning("[Skills] pip install 失败（%s）: %s", target.name, r.stderr[:500])
            except Exception as exc:
                report["pip"] = {"ok": False, "error": str(exc)[:500]}
                logger.warning("[Skills] pip install 异常（%s）: %s", target.name, exc)

        # 2. Node 依赖
        pkg_file = target / "package.json"
        if pkg_file.is_file():
            try:
                npm = shutil.which("npm")
                if not npm:
                    report["npm"] = {"ok": False, "error": "npm 未安装"}
                else:
                    r = subprocess.run(
                        [npm, "install", "--no-audit", "--no-fund", "--production"],
                        cwd=str(target), capture_output=True, text=True, timeout=120,
                    )
                    report["npm"] = {
                        "ok": r.returncode == 0,
                        "stdout": r.stdout[:2000] if r.stdout else "",
                        "stderr": r.stderr[:2000] if r.stderr else "",
                    }
                    if r.returncode != 0:
                        logger.warning("[Skills] npm install 失败（%s）: %s", target.name, r.stderr[:500])
            except Exception as exc:
                report["npm"] = {"ok": False, "error": str(exc)[:500]}
                logger.warning("[Skills] npm install 异常（%s）: %s", target.name, exc)

        # 3. 环境文件
        env_example = target / ".env.example"
        env_file = target / ".env"
        if env_example.is_file() and not env_file.is_file():
            try:
                shutil.copy2(env_example, env_file)
                report["env"] = {"ok": True, "action": "已从 .env.example 创建 .env，请检查配置"}
            except Exception as exc:
                report["env"] = {"ok": False, "error": str(exc)[:500]}
        elif env_example.is_file() and env_file.is_file():
            report["env"] = {"ok": True, "action": ".env 已存在，跳过"}
        elif env_example.is_file():
            report["env"] = {"ok": True, "action": "无 .env.example，跳过"}

        return report

    def _copy_skill_dir(self, src: Path, scope: str, name: Optional[str],
                        overwrite: bool = False) -> Dict[str, Any]:
        from .install_progress import STAGE_INSTALL, report

        report(step=STAGE_INSTALL, message="安装技能…")
        meta = parse_frontmatter((src / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
        self._validate_meta(meta, src.name)
        skill_name = _safe_name(name or str(meta.get("name") or src.name))
        if skill_name is None:
            raise ValueError(f"非法技能名（来自 {src.name}）")
        target = self._resolve_target(skill_name, scope)
        env_backup: Optional[bytes] = None
        if target.exists():
            if not overwrite:
                raise ValueError(f"技能 {skill_name!r} 已存在，请先删除或改名（或使用覆盖更新）")
            # 覆盖更新：保留本地 .env（用户密钥等配置），其余以新版为准
            env_file = target / ".env"
            if env_file.is_file():
                env_backup = env_file.read_bytes()
            shutil.rmtree(target)
        shutil.copytree(src, target, ignore=shutil.ignore_patterns("__pycache__", ".git"))
        if env_backup is not None:
            (target / ".env").write_bytes(env_backup)
        # 复制后安装依赖
        deps = self._install_skill_deps(target)
        result: Dict[str, Any] = {"ok": True, "name": skill_name, "path": str(target / "SKILL.md"), "scope": scope}
        if any(v is not None for v in deps.values()):
            result["deps"] = deps
        return result

    def _import_from_dir(self, src: Path, scope: str, name: Optional[str],
                          overwrite: bool = False) -> List[Dict[str, Any]]:
        candidates = self._find_skill_dirs(src)
        if not candidates:
            raise ValueError(f"{src} 下未找到含 SKILL.md 的技能目录")
        return [self._copy_skill_dir(c, scope, name if len(candidates) == 1 else None, overwrite)
                for c in candidates]

    # ------------------------------------------------------------ zip 导入

    def _import_zip_buffer(self, zf: zipfile.ZipFile, scope: str, name: Optional[str],
                           overwrite: bool = False) -> List[Dict[str, Any]]:
        names = zf.namelist()
        if len(names) > ZIP_MAX_ENTRIES:
            raise ValueError(f"zip 条目过多（>{ZIP_MAX_ENTRIES}），疑似恶意文件")
        # zip-slip 防护：所有条目必须解压在临时目录内
        tmp = Path(tempfile.mkdtemp(prefix="litework-skill-"))
        try:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                dest = (tmp / info.filename).resolve()
                if not str(dest).startswith(str(tmp.resolve())):
                    raise ValueError(f"zip 条目路径越界: {info.filename}")
                total = sum(i.file_size for i in zf.infolist())
                if total > ZIP_MAX_BYTES:
                    raise ValueError(f"zip 解压后超过大小限制（>{ZIP_MAX_BYTES // (1024 * 1024)}MB）")
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as fsrc, open(dest, "wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst)
            candidates = self._find_skill_dirs(tmp)
            if not candidates:
                raise ValueError("zip 内未找到含 SKILL.md 的技能目录")
            return [self._copy_skill_dir(c, scope, name if len(candidates) == 1 else None, overwrite)
                    for c in candidates]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _import_from_zip_file(self, p: Path, scope: str, name: Optional[str],
                              overwrite: bool = False) -> List[Dict[str, Any]]:
        if p.stat().st_size > ZIP_MAX_BYTES:
            raise ValueError(f"zip 超过大小限制（>{ZIP_MAX_BYTES // (1024 * 1024)}MB）")
        with zipfile.ZipFile(p) as zf:
            return self._import_zip_buffer(zf, scope, name, overwrite)

    def import_zip_bytes(self, data: bytes, scope: str = "workspace", name: Optional[str] = None,
                         overwrite: bool = False) -> List[Dict[str, Any]]:
        """前端 base64 上传的 zip 直接导入。"""
        if len(data) > ZIP_MAX_BYTES:
            raise ValueError(f"zip 超过大小限制（>{ZIP_MAX_BYTES // (1024 * 1024)}MB）")
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return self._import_zip_buffer(zf, scope, name, overwrite)

    # ------------------------------------------------------------ GitHub 导入

    def _import_from_github(self, url: str, scope: str, name: Optional[str],
                            overwrite: bool = False) -> List[Dict[str, Any]]:
        """从 GitHub 导入技能。

        优先使用 git clone（稳健、支持大仓库、保留子模块），
        git 不可用时回退为 zipball 流式下载。
        """
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in GITHUB_ALLOWED_HOSTS:
            raise ValueError("仅支持 github.com 仓库 URL")
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) < 2:
            raise ValueError("GitHub URL 需形如 https://github.com/{owner}/{repo}")
        owner, repo = parts[0], parts[1]
        repo = repo.removesuffix(".git")
        # 可选子路径：github.com/{owner}/{repo}/tree/{branch}/{dir...}
        subpath = ""
        if len(parts) >= 5 and parts[2] == "tree":
            subpath = "/".join(parts[4:])
        # 可选分支/标签：https://github.com/{owner}/{repo}#{branch|tag}
        branch = ""
        if parsed.fragment:
            branch = parsed.fragment.strip().strip("/")

        # 子路径安装（社区机制 tree/{branch}/{dir}）优先「可续传」按文件下载：
        # raw + Range + 持久缓存，慢网可断点续传、跨重试不重下（GitHub 的 zipball
        # 不支持 Range，git clone 也不能续传）。失败再回退 git clone / zip。
        if subpath:
            ref = branch or parts[3] or "HEAD"
            from .gh_fetch import InstallCancelled, TooManyFilesError, default_cache_root, fetch_subpath

            try:
                cached = fetch_subpath(
                    default_cache_root(), owner, repo, ref, subpath,
                    on_progress=lambda d, t: logger.info("[Skills] 可续传下载 %s: %d/%d", subpath, d, t),
                )
                candidates = self._find_skill_dirs(Path(cached))
                if candidates:
                    return [self._copy_skill_dir(c, scope, name if len(candidates) == 1 else None, overwrite)
                            for c in candidates]
                logger.warning("[Skills] 可续传下载未找到技能目录，回退 git/zip: %s", subpath)
            except TooManyFilesError as exc:
                # 大技能（如 ppt-master 12000+ 文件）：按文件续传请求数过多，
                # 回退 git clone 单 pack 整包下载（不可续传，靠重试兜底）
                logger.warning("[Skills] %s；改用 git clone 整包下载", exc)
            except InstallCancelled:
                raise  # 用户取消：不得回退到其他下载路径（缓存已在 fetch_subpath 清理）
            except Exception as exc:
                logger.warning("[Skills] 可续传下载失败，回退 git/zip（%s）: %s", subpath, exc)

        # 回退：git clone（稳健、支持大仓库）；无 git 时用 zipball 流式下载
        git = shutil.which("git")
        if git:
            return self._import_from_github_git(git, url, owner, repo, branch, subpath, scope, name, overwrite)

        # 回退：zipball 流式下载
        import httpx

        headers = {"User-Agent": "lite-work-agent", "Accept": "application/vnd.github+json"}
        api = f"https://api.github.com/repos/{owner}/{repo}"
        timeout = httpx.Timeout(connect=8.0, read=120.0, write=10.0, pool=8.0)
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
                if branch:
                    ref_resp = client.get(f"{api}/git/ref/heads/{branch}")
                    if ref_resp.status_code != 200:
                        tag_resp = client.get(f"{api}/git/ref/tags/{branch}")
                        if tag_resp.status_code != 200:
                            raise ValueError(f"分支或标签不存在: {branch}")
                elif subpath:
                    meta = client.get(api).json()
                    branch = meta.get("default_branch") or "main"
                zip_url = f"{api}/zipball/{branch}" if branch else f"{api}/zipball"
                with client.stream("GET", zip_url) as resp:
                    if resp.status_code == 404:
                        raise ValueError(f"仓库不存在或为私有: {owner}/{repo}（暂不支持私仓）")
                    if resp.status_code >= 400:
                        raise ValueError(f"下载技能失败（HTTP {resp.status_code}）")
                    tmp_zip = Path(tempfile.mkdtemp(prefix="litework-gh-")) / "repo.zip"
                    with open(tmp_zip, "wb") as f:
                        for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                            f.write(chunk)
                            checkpoint()  # 取消/暂停检查点（取消时 tempdir 由 finally 清理）
        except httpx.TimeoutException as exc:
            raise ValueError("下载技能超时（网络较慢或不可达），请检查网络后重试") from exc
        except httpx.TransportError as exc:
            raise ValueError(f"无法连接技能源（网络错误）：{exc}") from exc
        try:
            with zipfile.ZipFile(tmp_zip) as zf:
                results = self._import_zip_buffer(zf, scope, name, overwrite)
        finally:
            shutil.rmtree(tmp_zip.parent, ignore_errors=True)
        if subpath:
            results = [r for r in results if r["path"].replace("\\", "/").find(f"/{subpath}/") >= 0]
            if not results:
                raise ValueError(f"仓库 {subpath} 子路径下未找到技能")
        return results

    def _import_from_github_git(
        self, git: str, url: str, owner: str, repo: str,
        branch: str, subpath: str, scope: str, name: Optional[str],
        overwrite: bool = False,
    ) -> List[Dict[str, Any]]:
        """使用 git clone 从 GitHub 导入技能（支持大仓库、子模块）。"""
        # 构造 clone URL（不含片段）
        from urllib.parse import urlparse

        parsed = urlparse(url)
        clone_url = f"https://github.com/{owner}/{repo}.git"
        clone_args = [git, "clone", "--depth", "1"]
        if branch:
            clone_args.extend(["--branch", branch])
        clone_args.append(clone_url)

        tmp = Path(tempfile.mkdtemp(prefix="litework-git-"))
        target_dir = tmp / "repo"
        try:
            # git clone 不支持传输中续传（中断即丢临时 pack），但可自动重试
            # 兜住瞬时抖动；重试前清掉半成品目录（git clone 要求目标不存在）。
            import time as _time

            last_err = ""
            attempt = 0
            while True:
                checkpoint()  # 取消/暂停检查点（clone 单次不可中断，但重试间隙可退出）
                attempt += 1
                if target_dir.exists():
                    shutil.rmtree(target_dir, ignore_errors=True)
                try:
                    r = subprocess.run(
                        clone_args + [str(target_dir)],
                        capture_output=True, text=True, timeout=300,
                    )
                    if r.returncode == 0:
                        break
                    last_err = (r.stderr or "")[:500]
                except subprocess.TimeoutExpired:
                    last_err = "git clone 超时"
                if attempt >= 3:
                    raise ValueError(f"git clone 失败（已重试 3 次）: {last_err}")
                _time.sleep(min(2 ** attempt, 10))

            # 处理子路径
            skill_root = target_dir
            if subpath:
                skill_root = target_dir / subpath
                if not skill_root.is_dir():
                    raise ValueError(f"子路径 {subpath} 不存在")

            candidates = self._find_skill_dirs(skill_root)
            if not candidates:
                raise ValueError(f"导入来源中未找到含 SKILL.md 的技能目录")
            return [self._copy_skill_dir(c, scope, name if len(candidates) == 1 else None, overwrite)
                    for c in candidates]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ------------------------------------------------------------ Agent 工具接口（保持兼容）

    def get_tools(self) -> List[ToolDefinition]:
        return [ToolDefinition(
            name="load_skill",
            description="按名称加载项目或用户技能的 SKILL.md，使用前先从 System Prompt 的技能索引选择技能",
            parameters={
                "type": "object",
                "properties": {"skillName": {"type": "string", "description": "技能目录名"}},
                "required": ["skillName"],
            },
        )]

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        if name != "load_skill":
            raise ValueError(f"Unknown Skills Tool: {name}")
        skill_name = str(args.get("skillName") or "").strip()
        content = self.read_skill(skill_name)
        if content is None:
            # 失败时附带可用技能清单：Agent 无需自己去文件系统寻找
            available = [s["name"] for s in self.list_skills()]
            avail_hint = ("可用技能：" + ", ".join(available)) if available else "当前没有发现任何技能"
            return f"[Error]: 未找到技能 {skill_name!r}（{avail_hint}）"
        # 附带技能目录的绝对路径：SKILL.md 中的相对路径（如脚本、示例文件）
        # 以该目录为基准——Agent 工作区通常不是技能目录，缺路径只能瞎找
        skill_path = self._skills().get(skill_name)
        skill_dir = str(skill_path.parent) if skill_path else "（未知）"
        return (
            f"技能 {skill_name}\n"
            f"技能目录：{skill_dir}（文档中的相对路径均以该目录为基准）\n\n"
            f"{content}"
        )
