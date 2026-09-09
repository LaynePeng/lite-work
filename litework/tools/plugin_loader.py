# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""Cordis 风格本地插件加载器：~/.lite-work/plugins/ 自动发现与安装。

遵循与内置工具一致的 Cordis 插件模式（ToolPlugin → Plugin），
用户只需把 .py 文件放入 ~/.lite-work/plugins/ 即可注册新工具，
无需修改应用代码。

支持两种插件格式：
- 单文件插件：~/.lite-work/plugins/my_tool.py（内含 ToolPlugin 子类）
- 目录插件：~/.lite-work/plugins/my_tool/plugin.py 或 __init__.py
  （目录内可附带 requirements.txt 自动安装依赖）
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.types import Plugin

logger = logging.getLogger("litework.tools.plugin_loader")

# 插件搜索目录（相对于用户家目录）
USER_PLUGIN_DIR = ".lite-work/plugins"

# 社区插件默认源（manifest.json 根）
DEFAULT_COMMUNITY_URL = "https://github.com/laynepeng/lite-work-plugins"

# GitHub 导入仅允许这些主机（防 SSRF）
GITHUB_ALLOWED_HOSTS = {"github.com", "api.github.com", "codeload.github.com", "raw.githubusercontent.com"}


# ---------------------------------------------------------------- semver（无外部依赖）

def semver_parse(v: str) -> Optional[tuple]:
    """解析 semver 为可比较元组 (major, minor, patch, prerelease)。

    支持 "1.2.3"、"1.2.3-beta.1"、"v1.2.3"；解析失败返回 None。
    """
    if not v:
        return None
    s = str(v).strip().lstrip("vV")
    if not s:
        return None
    main, _, pre = s.partition("-")
    parts = main.split(".")
    if len(parts) < 2:
        return None
    nums = []
    for p in parts[:3]:
        if not p.isdigit():
            return None
        nums.append(int(p))
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums + ([pre] if pre else []))


def semver_compare(a: str, b: str) -> int:
    """返回 -1 / 0 / 1（a < b / a == b / a > b）；无法解析时按字符串比较。"""
    pa, pb = semver_parse(a), semver_parse(b)
    if pa is None or pb is None:
        if a == b:
            return 0
        return -1 if (a or "") < (b or "") else 1
    if pa[:3] != pb[:3]:
        return -1 if pa[:3] < pb[:3] else 1
    # 版本号相同：有预发布 < 正式版；预发布按字母序
    ra = pa[3] if len(pa) > 3 else ""
    rb = pb[3] if len(pb) > 3 else ""
    if ra == rb:
        return 0
    if not ra:
        return 1  # a 是正式版 > b 预发布
    if not rb:
        return -1  # b 是正式版 > a 预发布
    return -1 if ra < rb else 1


# ---------------------------------------------------------------- 已安装元信息（installed.json）

INSTALLED_FILE = "installed.json"


def _installed_path(config_dir: str) -> str:
    return os.path.join(_plugin_root(config_dir), INSTALLED_FILE)


def read_installed(config_dir: str) -> Dict[str, Dict[str, Any]]:
    """读取已安装插件元信息（version/source/installed_at）。"""
    try:
        with open(_installed_path(config_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_installed(config_dir: str, name: str, version: str,
                     source: Optional[str] = None) -> None:
    """记录/更新某个已安装插件的元信息。"""
    data = read_installed(config_dir)
    entry = data.get(name, {})
    if version:
        entry["version"] = version
    if source:
        entry["source"] = source
    entry["installed_at"] = _now_iso()
    data[name] = entry
    try:
        os.makedirs(_plugin_root(config_dir), exist_ok=True)
        with open(_installed_path(config_dir), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        logger.warning("[PluginLoader] 写入 installed.json 失败")


def unrecord_installed(config_dir: str, name: str) -> None:
    data = read_installed(config_dir)
    if name in data:
        del data[name]
        try:
            with open(_installed_path(config_dir), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now().isoformat(timespec="seconds")


def _safe_name(name: str) -> str:
    """安全清理：只保留字母、数字、下划线、连字符、点。"""
    clean = "".join(c for c in name if c.isalnum() or c in "._-")
    return clean.strip("._-") or "unknown"


def _discover_plugin_modules(plugin_dir: str) -> List[Dict[str, Any]]:
    """发现插件目录下所有可加载的插件模块描述。

    返回 [{name, path, is_dir, spec_path}] 列表。
    """
    candidates: List[Dict[str, Any]] = []
    if not os.path.isdir(plugin_dir):
        return candidates
    try:
        entries = sorted(os.listdir(plugin_dir))
    except OSError as exc:
        logger.warning("[PluginLoader] 无法读取插件目录 %s: %s", plugin_dir, exc)
        return candidates

    for entry in entries:
        full = os.path.join(plugin_dir, entry)
        if entry.startswith(".") or entry.startswith("_"):
            continue
        if os.path.isfile(full) and entry.endswith(".py"):
            candidates.append({
                "name": entry[:-3],
                "path": full,
                "is_dir": False,
                "spec_path": full,
            })
        elif os.path.isdir(full):
            # 目录插件：plugin.py 或 __init__.py
            plugin_file = os.path.join(full, "plugin.py")
            init_file = os.path.join(full, "__init__.py")
            spec = None
            if os.path.isfile(plugin_file):
                spec = plugin_file
            elif os.path.isfile(init_file):
                spec = init_file
            if spec:
                candidates.append({
                    "name": entry,
                    "path": full,
                    "is_dir": True,
                    "spec_path": spec,
                })
    return candidates


def _install_plugin_deps(plugin_dir: str) -> None:
    """安装目录插件依赖（幂等）。

    两套机制，按运行形态自动选择：
    - wheels/*.whl（推荐，全形态可用）：解压到插件 libs/ 子目录并加入
      sys.path。打包态（PyInstaller frozen）的 site-packages 已固化，
      pip 装到任何位置 frozen 进程都无法 import——自带 wheels 是唯一
      可离线分发的方式（whl 即 zip，包结构在根目录，直接解压即用）。
      以 stamp 文件记录已解压的 wheel 清单，未变化不重复解压。
    - requirements.txt（仅开发态）：sys.executable -m pip 安装到 venv。
      打包态 sys.executable 是 backend.exe（无 pip），跳过并告警。
    """
    # 1. wheels 解压（幂等：stamp 记录 wheel 文件名清单，变化才重新解压）
    wheels_dir = os.path.join(plugin_dir, "wheels")
    if os.path.isdir(wheels_dir):
        try:
            _extract_wheels(wheels_dir, os.path.join(plugin_dir, "libs"))
        except Exception as exc:
            logger.warning("[PluginLoader] 插件 wheels 解压失败（%s）: %s", plugin_dir, exc)

    # 2. requirements.txt：仅开发态（打包态无 pip 且装了也 import 不到）
    req_file = os.path.join(plugin_dir, "requirements.txt")
    if not os.path.isfile(req_file):
        return
    if getattr(sys, "frozen", False):
        logger.warning(
            "[PluginLoader] 打包版不支持 requirements.txt 安装（%s）："
            "请让插件自带 wheels/*.whl（pip download <pkg> -d wheels/）", plugin_dir,
        )
        return
    try:
        import subprocess
        logger.info("[PluginLoader] 安装插件依赖: %s", req_file)
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", req_file, "--quiet"],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=600,
        )
    except Exception as exc:
        logger.warning("[PluginLoader] 插件依赖安装失败（%s）: %s", req_file, exc)


def _extract_wheels(wheels_dir: str, libs_dir: str) -> None:
    """把 wheels_dir 下全部 .whl 解压到 libs_dir（whl 即 zip，包在根目录）。

    stamp 机制：libs_dir/.wheels-stamp 记录已解压的 wheel 文件名集合，
    与当前目录一致时跳过（避免每次加载重复 IO）；wheel 更新后自动重解压。
    """
    wheel_files = sorted(
        f for f in os.listdir(wheels_dir) if f.lower().endswith(".whl")
    )
    if not wheel_files:
        return
    import zipfile as _zipfile

    stamp_path = os.path.join(libs_dir, ".wheels-stamp")
    try:
        with open(stamp_path, "r", encoding="utf-8") as f:
            if f.read().splitlines() == wheel_files:
                return  # 已解压且无变化
    except OSError:
        pass

    os.makedirs(libs_dir, exist_ok=True)
    for wf in wheel_files:
        # zip-slip 防护：条目必须解压在 libs_dir 内
        with _zipfile.ZipFile(os.path.join(wheels_dir, wf)) as zf:
            libs_abs = os.path.abspath(libs_dir)
            for info in zf.infolist():
                if info.is_dir():
                    continue
                dest = os.path.abspath(os.path.join(libs_dir, info.filename))
                if not dest.startswith(libs_abs + os.sep):
                    raise ValueError(f"wheel 条目路径越界: {info.filename}")
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(info) as fsrc, open(dest, "wb") as fdst:
                    import shutil as _shutil
                    _shutil.copyfileobj(fsrc, fdst)
    with open(stamp_path, "w", encoding="utf-8") as f:
        f.write("\n".join(wheel_files))
    logger.info("[PluginLoader] 已解压 %d 个 wheel → %s", len(wheel_files), libs_dir)


def _plugin_libs_dir(spec_path: str) -> Optional[str]:
    """插件模块的 libs 目录（存在才返回）。目录插件：模块所在目录/libs。"""
    plugin_dir = os.path.dirname(os.path.abspath(spec_path))
    libs = os.path.join(plugin_dir, "libs")
    return libs if os.path.isdir(libs) else None


def _load_module(spec_path: str, module_name: str) -> Optional[object]:
    """动态导入 Python 模块，返回模块对象或 None。

    按 importlib 推荐模式注册进 sys.modules（exec 前注册，失败回滚）——
    inspect.getfile / 异常 traceback 等都依赖模块可查；协作模式插件的
    recipe.md 定位（inspect.getfile(type(self))）同样依赖这一点。
    """
    try:
        spec = importlib.util.spec_from_file_location(module_name, spec_path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        plugin_dir = os.path.dirname(os.path.abspath(spec_path))
        # sys.path 顺序：libs（wheels 解压出的依赖，优先）→ 插件目录（相对导入）
        libs = _plugin_libs_dir(spec_path)
        if libs and libs not in sys.path:
            sys.path.insert(0, libs)
        if plugin_dir not in sys.path:
            sys.path.insert(0, plugin_dir)
        sys.modules[module_name] = mod
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            sys.modules.pop(module_name, None)  # 执行失败不留半成品
            raise
        return mod
    except Exception as exc:
        logger.warning("[PluginLoader] 加载插件模块 %s 失败: %s", module_name, exc)
        return None


def _find_plugin_classes(module: object) -> List[type]:
    """从模块中找出 Plugin/ToolPlugin 子类（排除基类自身）。"""
    from ..orchestration.collab_policy import CollabModePlugin
    from ..tools.plugin import ToolPlugin

    classes: List[type] = []
    for name in dir(module):
        obj = getattr(module, name)
        if not isinstance(obj, type):
            continue
        # 只要 Plugin 子类（ToolPlugin / CollabModePlugin 也是 Plugin 子类）；
        # 基类自身排除——用户模块 import 基类时不实例化
        if (issubclass(obj, Plugin) and obj is not Plugin
                and obj is not ToolPlugin and obj is not CollabModePlugin):
            classes.append(obj)
    return classes


def load_plugins(config_dir: str) -> List[Plugin]:
    """从插件目录加载所有 Cordis 插件。

    搜索顺序：
    1. config_dir/plugins/（~/.lite-work/plugins/）
    2. 工作区 .lite-work/plugins/（可选，由调用方传递其他路径）

    返回 Plugin 实例列表（错误隔离：单个插件失败不影响其他）。
    """
    plugins: List[Plugin] = []
    seen_names: set = set()

    search_dirs = [os.path.join(config_dir, "plugins")]
    for plugin_dir in search_dirs:
        modules = _discover_plugin_modules(plugin_dir)
        for desc in modules:
            name = desc["name"]
            if name in seen_names:
                continue
            seen_names.add(name)

            # 目录插件：先安装依赖
            if desc["is_dir"]:
                _install_plugin_deps(desc["path"])

            mod = _load_module(desc["spec_path"], f"litework_plugin_{name}")
            if mod is None:
                continue

            classes = _find_plugin_classes(mod)
            if not classes:
                logger.debug("[PluginLoader] 插件 %s 中未找到 Plugin 子类，跳过", name)
                continue

            for cls in classes:
                try:
                    instance = cls()
                    plugins.append(instance)
                    logger.info("[PluginLoader] 已加载插件: %s (%s)", name, cls.__name__)
                except Exception as exc:
                    logger.warning("[PluginLoader] 实例化插件 %s.%s 失败: %s",
                                   name, cls.__name__, exc)

    return plugins


# ---------------------------------------------------------------- 管理接口（Web/API 薄封装）

# 插件图标约定：插件目录下的 icon.svg / icon.png / icon.jpg / icon.webp / icon.gif
ICON_FILENAMES = ("icon.svg", "icon.png", "icon.jpg", "icon.webp", "icon.gif")


def find_plugin_icon(config_dir: str, name: str) -> Optional[str]:
    """查找已安装目录插件的图标文件路径（svg 优先），无图标返回 None。

    name 经 _safe_plugin_name 校验（防路径穿越）；单文件插件（<name>.py）无图标。
    """
    safe = _safe_plugin_name(name)
    if not safe:
        return None
    plugin_dir = os.path.join(_plugin_root(config_dir), safe)
    if not os.path.isdir(plugin_dir):
        return None
    for fname in ICON_FILENAMES:
        candidate = os.path.join(plugin_dir, fname)
        if os.path.isfile(candidate):
            return candidate
    return None


def _plugin_root(config_dir: str) -> str:
    return os.path.join(config_dir, "plugins")


def _safe_plugin_name(name: str) -> Optional[str]:
    """插件名安全校验：只允许字母/数字/_/-/.，防路径穿越。"""
    name = (name or "").strip().strip("/\\")
    if not name or len(name) > 64:
        return None
    if name in (".", "..") or ":" in name:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return None
    return name


def list_plugins(config_dir: str) -> List[Dict[str, Any]]:
    """列出已安装插件及其暴露的工具名（不实例化，纯读元数据）。"""
    root = _plugin_root(config_dir)
    installed = read_installed(config_dir)
    out: List[Dict[str, Any]] = []
    for desc in _discover_plugin_modules(root):
        name = desc["name"]
        # 目录插件先解依赖（wheels 解压幂等；与 load_plugins 同路径，
        # 保证元信息读取时插件模块能正常 import 其依赖）
        if desc["is_dir"]:
            _install_plugin_deps(desc["path"])
        tools: List[str] = []
        removed: List[str] = []
        description = ""
        version = ""
        mod = _load_module(desc["spec_path"], f"litework_plugin_meta_{name}")
        if mod is not None:
            classes = _find_plugin_classes(mod)
            for cls in classes:
                try:
                    instance = cls()
                    if hasattr(instance, "get_tools"):
                        for t in instance.get_tools():
                            tools.append(getattr(t, "name", "?"))
                    if hasattr(instance, "removed_tools"):
                        removed.extend(instance.removed_tools)
                    desc_text = getattr(instance, "description", "")
                    if isinstance(desc_text, str) and desc_text:
                        description = desc_text
                    ver = getattr(instance, "version", "")
                    if isinstance(ver, str) and ver:
                        version = ver
                except Exception:
                    pass
        # 合并 installed.json 中的版本信息
        rec = installed.get(name, {})
        src = rec.get("source", "")
        if not version and rec.get("version"):
            version = rec["version"]
        out.append({
            "name": name,
            "path": desc["path"],
            "is_dir": desc["is_dir"],
            "tools": sorted(set(tools)),
            "removed_tools": sorted(set(removed)),
            "description": description,
            "version": version,
            "source": src,
        })
    return out


def delete_plugin(config_dir: str, name: str) -> Dict[str, Any]:
    """删除一个插件（单文件或目录）。"""
    safe = _safe_plugin_name(name)
    if safe is None:
        raise ValueError(f"非法插件名: {name!r}")
    root = _plugin_root(config_dir)
    candidates = [
        os.path.join(root, f"{safe}.py"),
        os.path.join(root, safe),
    ]
    removed = None
    for cand in candidates:
        if os.path.isfile(cand):
            os.remove(cand)
            removed = cand
            break
        if os.path.isdir(cand):
            import shutil
            shutil.rmtree(cand)
            removed = cand
            break
    if removed is None:
        raise ValueError(f"插件 {name!r} 不存在")
    unrecord_installed(config_dir, safe)
    return {"ok": True, "name": safe, "path": removed}


def _copy_plugin_entry(cand: Path, target: str, target_name: str) -> Dict[str, Any]:
    """复制单个插件条目到目标路径，返回导入结果。"""
    import shutil as _shutil
    if cand.is_file():
        dest = target if target.endswith(".py") else target + ".py"
        _shutil.copy2(cand, dest)
        return {"ok": True, "name": target_name, "path": dest}
    _shutil.copytree(cand, target, ignore=_shutil.ignore_patterns("__pycache__", ".git"))
    # 安装即解依赖：wheels 解压到 libs/（stamp 幂等），插件装完即自包含；
    # requirements.txt 在打包态跳过（load 时会再兜底调用一次，双保险）
    _install_plugin_deps(target)
    return {"ok": True, "name": target_name, "path": target}


def _copy_plugin_entries(
    root: str, entries: List[Path], name: Optional[str], overwrite: bool = False,
) -> List[Dict[str, Any]]:
    """把一批插件条目复制到插件根目录，支持覆盖。"""
    os.makedirs(root, exist_ok=True)
    imported = []
    for cand in entries:
        # 仅单条目来源时允许 name 覆盖；否则用条目自身名
        target_name = _safe_plugin_name((name if len(entries) == 1 else None) or cand.name)
        if target_name is None:
            raise ValueError(f"非法插件名（来自 {cand.name}）")
        target = os.path.join(root, target_name)
        if os.path.exists(target):
            if not overwrite:
                raise ValueError(f"插件 {target_name!r} 已存在，请先删除或使用覆盖模式")
            delete_plugin(str(Path(root).parent), target_name)
        imported.append(_copy_plugin_entry(cand, target, target_name))
    return imported


def import_zip_bytes(config_dir: str, data: bytes, name: Optional[str] = None,
                     overwrite: bool = False) -> List[Dict[str, Any]]:
    """从 zip 字节流导入插件到 ~/.lite-work/plugins/。"""
    import io as _io
    import shutil as _shutil
    import tempfile as _tempfile
    import zipfile as _zipfile

    root = _plugin_root(config_dir)
    os.makedirs(root, exist_ok=True)
    tmp = _tempfile.mkdtemp(prefix="litework-plugin-")
    try:
        with _zipfile.ZipFile(_io.BytesIO(data)) as zf:
            if len(zf.namelist()) > 2000:
                raise ValueError("zip 条目过多（>2000），疑似恶意文件")
            for info in zf.infolist():
                if info.is_dir():
                    continue
                dest = (Path(tmp) / info.filename).resolve()
                if not str(dest).startswith(str(Path(tmp).resolve())):
                    raise ValueError(f"zip 条目路径越界: {info.filename}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as fsrc, open(dest, "wb") as fdst:
                    _shutil.copyfileobj(fsrc, fdst)
        candidates = _find_plugin_entries(Path(tmp))
        if not candidates:
            raise ValueError("zip 内未找到插件")
        # zip 根目录直接是插件目录（plugin.py 在压缩包顶层，常见于
        # "在文件夹内压缩"）：有 name 参数时归位为目录插件（保留伴生文件）
        if name and candidates and all(e.is_file() for e in candidates) and _is_plugin_module_root(Path(tmp)):
            candidates = [_wrap_as_plugin_dir(Path(tmp), name)]
        return _copy_plugin_entries(root, candidates, name, overwrite)
    finally:
        _shutil.rmtree(tmp, ignore_errors=True)


def _find_plugin_entries(base: Path, depth: int = 0) -> List[Path]:
    """找出 base 下所有插件条目（.py 文件 或 含 plugin.py/__init__.py 的目录），深度限制 3 层。"""
    found: List[Path] = []
    if depth > 3:
        return found
    try:
        for child in sorted(base.iterdir()):
            if child.name.startswith(".") or child.name.startswith("_"):
                continue
            if child.is_file() and child.name.endswith(".py"):
                found.append(child)
            elif child.is_dir():
                if (child / "plugin.py").is_file() or (child / "__init__.py").is_file():
                    found.append(child)
                else:
                    found.extend(_find_plugin_entries(child, depth + 1))
    except OSError:
        pass
    return found


def _is_plugin_module_root(base: Path) -> bool:
    """base 目录直接包含 plugin.py / __init__.py（即它本身是一个插件目录）。"""
    return (base / "plugin.py").is_file() or (base / "__init__.py").is_file()


def _wrap_as_plugin_dir(base: Path, plugin_name: str) -> Path:
    """把 base 下的全部内容收进 base/plugin_name/ 子目录（目录插件归位）。"""
    inner = base / plugin_name
    inner.mkdir(exist_ok=True)
    for e in list(base.iterdir()):
        if e.name != plugin_name:
            e.rename(inner / e.name)
    return inner


def import_source(config_dir: str, source: str, name: Optional[str] = None,
                  overwrite: bool = False, version: Optional[str] = None) -> List[Dict[str, Any]]:
    """从本地目录 / zip 文件 / GitHub URL 导入插件，并在 installed.json 记录元信息。"""
    source = (source or "").strip()
    if source.startswith(("http://", "https://")):
        results = _import_from_github(config_dir, source, name, overwrite)
    else:
        p = Path(source).expanduser()
        if not p.exists():
            raise ValueError(f"路径不存在: {source}")
        if p.is_dir():
            # 目录本身是插件目录（直接含 plugin.py）→ 整目录作为一个插件安装
            if _is_plugin_module_root(p):
                entries: List[Path] = [p]
            else:
                entries = _find_plugin_entries(p)
            if not entries:
                raise ValueError(f"{source} 下未找到插件")
            results = _copy_plugin_entries(_plugin_root(config_dir), entries, name, overwrite)
        elif p.suffix.lower() == ".zip":
            with open(p, "rb") as f:
                results = import_zip_bytes(config_dir, f.read(), name, overwrite)
        else:
            raise ValueError(f"不支持的导入来源: {source}（支持目录 / .zip / GitHub URL）")
    for r in results:
        record_installed(config_dir, r["name"], version or "", source)
    return results


def _import_from_github(config_dir: str, url: str, name: Optional[str],
                        overwrite: bool = False) -> List[Dict[str, Any]]:
    """从 GitHub 仓库导入插件（zipball 下载），支持子目录（tree/{branch}/{dir}）。"""
    import io as _io
    import httpx
    import zipfile as _zipfile
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in GITHUB_ALLOWED_HOSTS:
        raise ValueError("仅支持 github.com 仓库 URL")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise ValueError("GitHub URL 需形如 https://github.com/{owner}/{repo}")
    owner, repo = parts[0], parts[1]
    repo = repo.removesuffix(".git")
    # 子目录：github.com/{owner}/{repo}/tree/{branch}/{dir...}
    subpath = ""
    if len(parts) >= 5 and parts[2] == "tree":
        subpath = "/".join(parts[4:])

    api = f"https://api.github.com/repos/{owner}/{repo}"
    headers = {"User-Agent": "lite-work-agent", "Accept": "application/vnd.github+json"}
    branch = ""
    with httpx.Client(timeout=60, follow_redirects=True, headers=headers) as client:
        if subpath:
            meta = client.get(api).json()
            branch = meta.get("default_branch") or "main"
        zip_url = f"{api}/zipball/{branch}" if branch else f"{api}/zipball"
        resp = client.get(zip_url)
        if resp.status_code == 404:
            raise ValueError(f"仓库不存在或为私有: {owner}/{repo}（暂不支持私仓）")
        resp.raise_for_status()
    with _zipfile.ZipFile(_io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
        top_dir = names[0].split("/")[0] if names and "/" in names[0] else ""
        tmp = Path(tempfile.mkdtemp(prefix="litework-plugin-gh-"))
        try:
            import shutil as _shutil
            for info in zf.infolist():
                if info.is_dir():
                    continue
                rel = info.filename
                if rel.startswith(top_dir + "/"):
                    rel = rel[len(top_dir) + 1:]
                if subpath:
                    # 只提取子目录内容
                    if not rel.startswith(subpath + "/"):
                        continue
                    rel = rel[len(subpath) + 1:]
                dest = (tmp / rel).resolve()
                if not str(dest).startswith(str(tmp.resolve())):
                    raise ValueError(f"zip 条目路径越界: {info.filename}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as fsrc, open(dest, "wb") as fdst:
                    _shutil.copyfileobj(fsrc, fdst)
            entries = _find_plugin_entries(tmp)
            # 子目录本身就是一个插件目录（内容直接是 plugin.py/__init__.py）：
            # 把整个子目录内容归入以子目录名命名的插件目录，保留 requirements.txt
            # 等伴生文件——否则会被当成散文件、插件名错成 "plugin"
            if subpath and entries and all(e.is_file() for e in entries) and _is_plugin_module_root(tmp):
                entries = [_wrap_as_plugin_dir(tmp, subpath.rstrip("/").split("/")[-1])]
            return _copy_plugin_entries(_plugin_root(config_dir), entries, name, overwrite)
        finally:
            _shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 社区仓库 manifest

def fetch_community_manifest(url: str = DEFAULT_COMMUNITY_URL) -> Dict[str, Any]:
    """下载并解析社区仓库的 manifest.json。

    url 可为 GitHub 仓库根（默认）或带 /tree/{branch}/{dir} 的子目录。
    返回 {version, min_app_version, plugins: [], skills: []}。
    """
    import httpx

    from urllib.parse import urlparse

    url = (url or DEFAULT_COMMUNITY_URL).strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("社区源 URL 必须是 http/https")
    if parsed.hostname not in GITHUB_ALLOWED_HOSTS:
        raise ValueError("仅支持 github.com 仓库 URL")

    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise ValueError("GitHub URL 需形如 https://github.com/{owner}/{repo}")
    owner, repo = parts[0], parts[1]
    repo = repo.removesuffix(".git")

    # manifest 路径：默认根目录；子目录时定位到该目录
    manifest_path = "manifest.json"
    if len(parts) >= 5 and parts[2] == "tree":
        branch = parts[3]
        subdir = "/".join(parts[4:])
        manifest_path = f"{subdir}/manifest.json"
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{manifest_path}"
    else:
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{manifest_path}"

    with httpx.Client(timeout=60, follow_redirects=True,
                      headers={"User-Agent": "lite-work-agent"}) as client:
        resp = client.get(raw_url)
        if resp.status_code != 200:
            raise ValueError(f"社区源未找到 manifest.json（{resp.status_code}）")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ValueError(f"manifest.json 解析失败: {exc}")
    if not isinstance(data, dict):
        raise ValueError("manifest.json 格式错误（应为 JSON 对象）")
    return {
        "version": str(data.get("version") or ""),
        "min_app_version": str(data.get("min_app_version") or ""),
        "plugins": data.get("plugins") if isinstance(data.get("plugins"), list) else [],
        "skills": data.get("skills") if isinstance(data.get("skills"), list) else [],
    }


def install_from_source(config_dir: str, source: str, name: Optional[str] = None,
                        overwrite: bool = False, version: Optional[str] = None) -> List[Dict[str, Any]]:
    """统一安装入口（别名）：URL / zip / 本地目录，记录版本与来源。"""
    return import_source(config_dir, source, name=name, overwrite=overwrite, version=version)