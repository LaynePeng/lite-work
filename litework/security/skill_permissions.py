"""技能权限规则（对齐 OpenCode permission.skill）。

config.json 中的 `skill_permissions` 是 glob 模式 → 动作 的映射：

    "skill_permissions": {
        "internal-*": "deny",
        "experimental-*": "ask",
        "*": "allow"
    }

- 匹配顺序：config 中的插入顺序，首个命中的模式生效；无命中默认 allow
- deny：技能对 Agent 完全隐藏——技能索引不列出、triggers 不匹配、
  load_skill 工具与 /skill 命令一律拒绝
- ask：使用前弹审批卡确认（load_skill 走 beforeTool 管道；
  /skill 显式命令与 triggers 自动匹配在任务启动时审批）
- allow：默认行为，无任何拦截
"""
from __future__ import annotations

import fnmatch
from typing import Any, Dict

VALID_ACTIONS = ("allow", "deny", "ask")


def normalize_rules(raw: Any) -> Dict[str, str]:
    """清洗配置中的规则：非法 action/pattern 丢弃，统一小写。"""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, str] = {}
    for pat, action in raw.items():
        p = str(pat or "").strip().lower()
        a = str(action or "").strip().lower()
        if p and a in VALID_ACTIONS:
            out[p] = a
    return out


def resolve(rules: Dict[str, str], name: str) -> str:
    """解析技能名对应的动作：首个命中的模式生效，默认 allow。"""
    n = (name or "").strip().lower()
    if not n:
        return "allow"
    for pat, action in rules.items():
        if fnmatch.fnmatchcase(n, pat):
            return action
    return "allow"
