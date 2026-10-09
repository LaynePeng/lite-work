# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""插件能力声明与权限差异（W4，对齐手册 pp.28-30 能力版本化 + p.56 权限可见）。

解决什么：装插件 / 升级技能会**静默扩大攻击面**——用户毫不知情地多了一个
能执行命令的插件。本模块把"这个插件能做什么"变成程序可读、可 diff 的数据。

设计：
- `permissions` 字段从插件源码的 frontmatter / manifest.json / `contributes` 里读；
- `diff_permissions` 计算新旧版本之间的权限变化（增长 / 收窄 / 不变）；
- **增长 → 需要用户确认**（安装/升级弹窗显示警示）；收窄 → 静默通过；
- 不改变现有安全模型（SecurityGuard 仍然做最终拦截），这里只是"事前告知"。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

#: 权限类别（对应工具的实际能力面）
PERMISSION_CATEGORIES = frozenset({
    "exec",          # 能执行命令（execute_command / shell）
    "fs_write",      # 能写文件（write_file / apply_* / delete_file）
    "fs_read",       # 能读文件（read_file / search_code / get_file_outline）
    "network",       # 能联网（webfetch / MCP 网络工具）
    "skills",        # 能加载技能（load_skill）
    "git",           # 能操作 git（git_commit / git_push）
    "spawn",         # 能派生子 Agent（spawn_agent）
    "office",        # 能生成办公文档（docx/xlsx/pptx/pdf）
})

#: 每个类别的中文说明（安装弹窗用）
CATEGORY_LABELS: Dict[str, str] = {
    "exec": "执行命令",
    "fs_write": "写文件",
    "fs_read": "读文件",
    "network": "联网",
    "skills": "加载技能",
    "git": "Git 操作",
    "spawn": "派生子 Agent",
    "office": "生成办公文档",
}

#: 从插件源码里提取 permissions 声明的正则（frontmatter / 类属性 / dict 字面量）
_PERM_RE = re.compile(
    r'["\']?permissions["\']?\s*[:=]\s*\{([^}]*)\}', re.DOTALL
)


def normalize_permissions(raw: Any) -> Dict[str, List[str]]:
    """归一化 permissions 字段（宽容解析，不抛错）。

    输入形态：
        {"exec": ["*"], "network": ["api.example.com"]}
        {"exec": True}  →  {"exec": ["*"]}
    输出：{category: [scope, ...]}，非法类别忽略。
    """
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, List[str]] = {}
    for key, val in raw.items():
        cat = str(key).strip().lower()
        if cat not in PERMISSION_CATEGORIES:
            continue
        if isinstance(val, bool):
            out[cat] = ["*"] if val else []
        elif isinstance(val, list):
            scopes = [str(v).strip() for v in val if isinstance(v, (str, int))]
            out[cat] = scopes if scopes else (["*"] if val else [])
        elif isinstance(val, str):
            out[cat] = [val.strip()] if val.strip() else []
        else:
            out[cat] = ["*"]
    return out


def extract_permissions_from_code(source: str) -> Dict[str, List[str]]:
    """从插件源码里提取 permissions 声明（作为 manifest 的回退来源）。

    匹配形如 `permissions = {"exec": [...]}` 或 frontmatter `permissions:` 的块。
    找不到 → 空字典（无声明 = 视为无额外权限）。
    """
    if not source:
        return {}
    match = _PERM_RE.search(source)
    if not match:
        return {}
    # 简单解析：把花括号内内容当作 JSON-like dict 的值部分
    inner = match.group(1)
    result: Dict[str, List[str]] = {}
    # 逐类别匹配
    for cat in PERMISSION_CATEGORIES:
        cat_re = re.compile(
            rf'["\']?{cat}["\']?\s*:\s*\[([^\]]*)\]', re.DOTALL
        )
        cat_match = cat_re.search(inner)
        if cat_match:
            items = [x.strip().strip('\'"') for x in cat_match.group(1).split(",") if x.strip()]
            result[cat] = items if items else ["*"]
        else:
            # 布尔形式：{"exec": True}
            bool_re = re.compile(rf'["\']?{cat}["\']?\s*:\s*(True|False)', re.IGNORECASE)
            bool_match = bool_re.search(inner)
            if bool_match and bool_match.group(1).lower() == "true":
                result[cat] = ["*"]
    return result


def read_plugin_permissions(plugin_dir: str) -> Dict[str, List[str]]:
    """读插件的 permissions 声明（manifest.json > plugin.py 源码提取 > 空）。"""
    from pathlib import Path
    import json as _json

    p = Path(plugin_dir)
    # 1. manifest.json（社区插件标准位置）
    manifest = p / "manifest.json"
    if manifest.is_file():
        try:
            data = _json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("permissions"):
                return normalize_permissions(data["permissions"])
        except (OSError, _json.JSONDecodeError):
            pass

    # 2. plugin.py 源码里的声明
    plugin_py = p / "plugin.py"
    if plugin_py.is_file():
        try:
            source = plugin_py.read_text(encoding="utf-8", errors="replace")
            return extract_permissions_from_code(source)
        except OSError:
            pass

    return {}


def diff_permissions(
    old: Dict[str, List[str]],
    new: Dict[str, List[str]],
) -> Dict[str, Any]:
    """计算权限变化。

    返回：
        {
            "widened": bool,           # 是否有增长（需要用户确认）
            "narrowed": bool,          # 是否有收窄（静默通过）
            "added": {cat: scopes},    # 新增的类别
            "expanded": {cat: scopes}, # 已有类别新增的 scope
            "removed": {cat: ...},     # 移除的类别
            "shrunk": {cat: ...},      # 已有类别收窄的 scope
            "summary": str,            # 人话摘要（安装弹窗用）
        }
    """
    added: Dict[str, List[str]] = {}
    expanded: Dict[str, List[str]] = {}
    removed: Dict[str, List[str]] = {}
    shrunk: Dict[str, List[str]] = {}

    for cat, scopes in new.items():
        if cat not in old:
            added[cat] = scopes
        else:
            old_set = set(old[cat])
            new_set = set(scopes)
            gained = new_set - old_set
            lost = old_set - new_set
            if gained:
                expanded[cat] = sorted(gained)
            if lost:
                shrunk[cat] = sorted(lost)

    for cat in old:
        if cat not in new:
            removed[cat] = old[cat]

    widened = bool(added or expanded)
    narrowed = bool(removed or shrunk)

    # 人话摘要
    parts: List[str] = []
    for cat in sorted(list(added) + list(expanded)):
        label = CATEGORY_LABELS.get(cat, cat)
        scopes = added.get(cat) or expanded.get(cat) or []
        scope_str = "全部" if "*" in scopes else "、".join(scopes[:3])
        if cat in added:
            parts.append(f"新增「{label}」权限（{scope_str}）")
        else:
            parts.append(f"扩展「{label}」权限范围至 {scope_str}")
    summary = "；".join(parts) if parts else ("权限收窄" if narrowed else "权限无变化")

    return {
        "widened": widened,
        "narrowed": narrowed,
        "added": added,
        "expanded": expanded,
        "removed": removed,
        "shrunk": shrunk,
        "summary": summary,
    }


def permissions_for_display(perms: Dict[str, List[str]]) -> List[str]:
    """把权限转成人类可读的行列表（安装弹窗用）。"""
    if not perms:
        return ["（无特殊权限声明）"]
    out: List[str] = []
    for cat in sorted(perms):
        label = CATEGORY_LABELS.get(cat, cat)
        scopes = perms[cat]
        if "*" in scopes:
            out.append(f"⚠ {label}：不受限")
        elif scopes:
            out.append(f"{label}：{'、'.join(scopes[:5])}{'…' if len(scopes) > 5 else ''}")
        else:
            out.append(f"{label}：（空）")
    return out
