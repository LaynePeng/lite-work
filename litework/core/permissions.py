# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""职责域权限模型：工具 → 职责域映射 与 domains → allowed/permissions 换算。

职责域模型取代「tools 白名单 + permissions 覆盖」双层配置：
- 每个职责域一个动作（allow / deny / ask），UI 按域开关配置；
- 工具名 → 域映射集中在本文件（新增工具时在这里补一行）；
- 未知工具（MCP / 用户插件动态注册）默认域 "misc"，默认动作 ask（需显式配置）。
"""
from __future__ import annotations

from typing import Dict, List, Optional

# 职责域定义（顺序决定 UI 展示顺序）
DOMAIN_ORDER = ["read", "plan", "edit", "execute", "git_write", "web", "office", "collab", "interactive", "misc"]

# 域的中文名与说明（前端展示用）
DOMAIN_LABELS: Dict[str, str] = {
    "read":        "读取与搜索（文件/AST/Git 查看/代码审查）",
    "plan":        "规划产出（plan_save 写计划文件，仅规划 Agent）",
    "edit":        "修改文件（写入/精确编辑/删除）",
    "execute":     "执行命令（终端/脚本）",
    "git_write":   "Git 写入（提交）",
    "web":         "联网（webfetch / 批量抓取）",
    "office":      "办公生产力（文档/表格/PPT/PDF/OCR/图表）",
    "collab":      "多 Agent 协作（派生/消息/共享任务）",
    "interactive": "交互（提问/待办/技能加载）",
    "misc":        "其他/未分类（MCP 与插件动态工具）",
}

# 工具 → 职责域 映射
TOOL_DOMAIN: Dict[str, str] = {
    # read
    "read_file": "read", "list_dir": "read", "file_tree": "read",
    "search_code": "read", "get_file_outline": "read", "read_focused_symbol": "read",
    "git_status": "read", "git_diff": "read", "git_log": "read", "git_branch": "read",
    "review_code": "read",
    # plan（plan_save：Plan Agent 唯一写通道，见 tools/plan_save.py）
    "plan_save": "plan",
    # edit
    "write_file": "edit", "delete_file": "edit",
    "apply_search_replace": "edit", "apply_unified_diff": "edit",
    # execute
    "execute_command": "execute",
    # git_write
    "git_commit": "git_write",
    # web
    "webfetch": "web", "webfetch_batch": "web",
    # office
    "docx_create": "office", "docx_append": "office", "docx_format": "office",
    "docx_replace": "office", "docx_read": "office",
    "xlsx_create": "office", "xlsx_format": "office", "xlsx_replace": "office", "xlsx_read": "office",
    "pptx_create": "office", "pptx_format": "office", "pptx_read": "office",
    "ppt_edit": "office",
    "pdf_create": "office", "pdf_read": "office",
    "data_analyze": "office", "chart_make": "office",
    "ocr_image": "office", "ocr_document": "office", "ocr_pptx": "office",
    # collab
    "spawn_agent": "collab", "list_agents": "collab", "close_agent": "collab",
    "wait_agents": "collab", "send_message": "collab", "followup_task": "collab",
    "create_shared_tasks": "collab", "list_shared_tasks": "collab",
    "claim_shared_task": "collab", "complete_shared_task": "collab",
    # interactive
    "ask_user": "interactive", "todo_write": "interactive", "load_skill": "interactive",
}

# 默认域动作（内置 agent 未显式声明 domains 时的兜底）——偏安全
DEFAULT_DOMAIN_ACTIONS: Dict[str, str] = {
    "read": "allow", "plan": "deny", "edit": "deny", "execute": "deny", "git_write": "deny",
    "web": "allow", "office": "deny", "collab": "allow", "interactive": "allow",
    "misc": "ask",
}


def domain_of(tool_name: str) -> str:
    """工具 → 职责域；未知工具归 misc。"""
    return TOOL_DOMAIN.get(tool_name, "misc")


# ---------------------------------------------------------------- 无人值守权限档
#
# 盯梢/定时任务在**没人点审批**的时刻运行（审批门 600s 超时后自动拒绝，
# 见 security/approval.py）。因此这些任务的权限不能只靠「标记 ask」——
# 那只会让它卡住然后被拒；必须真的把写/执行类域**从工具面移除**。
#
# 档位是白名单式的：值 = **允许保留**的职责域，其余全部 deny。
# 前端/路由层只接受这里定义的键（见 server/routers/schedules.py 的
# ALLOWED_PERMISSIONS），未知档位按最保守的 "read" 处理（fail-safe）。
UNATTENDED_PROFILES: Dict[str, frozenset] = {
    # 只读：读文件/搜索/Git 查看 + 交互（todo_write 等），不给联网
    "read": frozenset({"read", "interactive"}),
    # 只读 + 联网：抓网页（盯网页变化这类计划的基本需求）
    "read+web": frozenset({"read", "web", "interactive"}),
}


def deny_domains_for(profile: str) -> frozenset:
    """无人值守权限档 → 需要显式拒绝的职责域集合。

    未知档位返回最保守的结果（只留 read/interactive）——宁可少给能力，
    也不能因为档位名写错就让一个无人值守任务拿到写/执行权限。
    """
    keep = UNATTENDED_PROFILES.get(profile)
    if keep is None:
        keep = UNATTENDED_PROFILES["read"]
    return frozenset(d for d in DOMAIN_ORDER if d not in keep)


def resolved_domains(domains: Optional[Dict[str, str]]) -> Dict[str, str]:
    """合并显式 domains 与默认动作（未声明域用默认）。"""
    base = dict(DEFAULT_DOMAIN_ACTIONS)
    for dom, action in (domains or {}).items():
        if action in ("allow", "deny", "ask"):
            base[dom] = action
    return base


def domains_to_allowed_and_permissions(
    domains: Optional[Dict[str, str]],
    all_tool_names: List[str],
    extra_tools: Optional[List[str]] = None,
    unattended_profile: Optional[str] = None,
) -> tuple[List[str], Dict[str, str]]:
    """domains → (allowed, permissions) 换算，供 build_registry 使用。

    - allowed：显式域 allow/ask 的工具列表；extra_tools 追加（UI 高级微调）；
    - permissions：域 ask 的工具 → "ask"；域 deny 的工具 → "deny"（双保险拦截）；
    - unattended_profile：盯梢/定时等**无人值守**任务传档位名（read / read+web）。
      档位之外的域一律 deny，且 extra_tools 也会被过滤——否则会话配置里的
      extra_tools 能绕过无人值守收敛，等于没用。
    - 注意：allowed 恒为列表（可为空 = 全禁），**绝不为 None**——
      build_registry(allowed=None) 语义是全量放行，空列表退化会导致
      「应全禁的 Agent 反而获得全部工具」的安全漏洞。
    """
    resolved = resolved_domains(domains)
    if unattended_profile is not None:
        for dom in deny_domains_for(unattended_profile):
            resolved[dom] = "deny"
    allowed: List[str] = []
    permissions: Dict[str, str] = {}
    for t in all_tool_names:
        act = resolved.get(domain_of(t), "ask")
        if act == "deny":
            permissions[t] = "deny"
        elif act == "allow":
            allowed.append(t)
        elif act == "ask":
            allowed.append(t)
            permissions[t] = "ask"
    for t in extra_tools or []:
        # 无人值守时不给 extra_tools 开口子：白名单外的工具一律不放行
        if unattended_profile is not None and resolved.get(domain_of(t), "ask") == "deny":
            continue
        if t not in allowed:
            allowed.append(t)
    return allowed, permissions


def denied_tool_messages(agent_label: str, permissions: Dict[str, str]) -> Dict[str, str]:
    """权限为 deny 的工具 → 自解释拒绝消息（供 AgentLoop 越权调用时回填）。

    背景（v1.9.x 循环事故复盘）：模型越权调用被裁剪的工具时，注册表只回
    「[Error]: 未注册的工具」——不解释原因，模型易误读为工具名笔误而换参数
    重试，成为过度验证/循环的信任污染入口。此消息明确三点：是权限边界而非
    工具名错误、重试无效、出路是什么（向用户说明 / 写入计划交接）。
    """
    return {
        tool: _denial_message(agent_label, tool)
        for tool, action in permissions.items() if action == "deny"
    }


def _denial_message(agent_label: str, tool: str) -> str:
    domain = domain_of(tool)
    label = DOMAIN_LABELS.get(domain, domain)
    return (
        f"[权限拒绝]: {tool} 属于「{label}」职责域，当前 Agent（{agent_label}）对该域"
        "无权限（deny），此调用不会执行。这不是工具名错误，换参数重试同样会被拒绝——"
        "能力边界由用户配置。若任务确需该能力，请在回复中向用户说明，"
        "或把需要的改动写入计划 / 交接给具备对应权限的 Agent 执行。"
    )