# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""Agent 类型与注册机制（对应课程第11课（多Agent协作）或第12课（Agent类型），参考 OpenCode Agent 设计）。

OpenCode 内置两种 primary agent：
- build：默认，拥有全部工具，负责实际的开发工作；
- plan：只读、禁止编辑与执行命令，只做分析与规划。

用户还可以通过配置文件自定义 agent（参考 opencode.json 的 agent 段），
每个 agent 可以指定：system prompt、模型、temperature、可用工具（裁剪）、
以及权限覆盖（deny/allow/ask）。
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .system_prompt import FINAL_REPORT_REQUIREMENT  # noqa: F401  # 兼容旧导入方（测试断言）

logger = logging.getLogger("litework.agent")

# 工具权限取值
PERM_DENY = "deny"
PERM_ALLOW = "allow"
PERM_ASK = "ask"


def parse_frontmatter(text: str):
    """解析 Markdown 文件的 YAML frontmatter（--- 包裹的头部）。

    返回 (frontmatter_dict, 正文)。
    仅支持 Agent 配置常用的子集：
    - 标量（字符串 / 数字 / 布尔）
    - 列表（- item）
    - 嵌套 map（key: value）
    不依赖 PyYAML。
    """
    data: Dict[str, Any] = {}
    m = re.match(r"^\ufeff?---\s*\n(.*?)\n---\s*\n?", text, re.DOTALL)
    if not m:
        return data, text
    body = m.group(1)
    rest = text[m.end():]

    def parse_value(raw: str):
        raw = raw.strip()
        if not raw:
            return None
        if raw == "true":
            return True
        if raw == "false":
            return False
        try:
            return int(raw)
        except ValueError:
            pass
        try:
            return float(raw)
        except ValueError:
            pass
        return raw.strip('"').strip("'")

    # 逐行解析，支持列表与嵌套 map
    lines = body.split("\n")
    stack: List[Dict[str, Any]] = [data]
    list_indent: Dict[int, int] = {}
    last_key: List[Optional[str]] = [None]

    for line in lines:
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()

        if stripped.startswith("- "):
            item = parse_value(stripped[2:])
            parent = stack[indent // 2] if indent // 2 < len(stack) else data
            parent.setdefault("__list__", []).append(item)
            continue

        if ":" not in stripped:
            continue
        key, _, raw_val = stripped.partition(":")
        key = key.strip()
        val = parse_value(raw_val)

        # 收缩栈：缩进减小则回退
        while len(stack) > 1 and indent // 2 < len(stack) - 1:
            stack.pop()
            last_key.pop()
        current = stack[-1]

        if val is None and raw_val.strip() == "":
            # 子 map 开始
            child: Dict[str, Any] = {}
            current[key] = child
            stack.append(child)
            last_key.append(key)
        else:
            current[key] = val
            last_key[-1] = key

    # 把 __list__ 转成真列表（inline 用 dict 简化，这里直接保留）
    def normalize(node: Any) -> Any:
        if isinstance(node, dict):
            if "__list__" in node:
                return node["__list__"]
            return {k: normalize(v) for k, v in node.items() if k != "__list__"}
        return node

    return normalize(data), rest


@dataclass
class AgentProfile:
    """一个 Agent 的完整描述。

    字段对齐 OpenCode 的 agent 配置：
    - id:            agent 唯一标识（文件名或配置 key）
    - mode:          "primary"（Tab 切换的主 agent）| "subagent"（可被 @ 引用）
    - description:   agent 作用描述
    - system_prompt: 自定义 System Prompt（None 则用全局默认）
    - model:         覆盖模型（None 用全局模型）
    - temperature:   覆盖 temperature（None 用全局）
    - tools:         允许使用的工具列表（None = 全部；[] = 只读）
    - permissions:   工具权限覆盖 {工具名或通配: "deny"|"allow"|"ask"}
    - hidden:        是否在 UI 隐藏
    - icon:          图标（emoji 字符串，供前端气泡/选择器显示；空 = 前端回退默认映射）
    """

    id: str
    mode: str = "primary"
    description: str = ""
    system_prompt: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    tools: Optional[List[str]] = None
    permissions: Dict[str, str] = field(default_factory=dict)
    hidden: bool = False
    icon: str = ""

    # 职责域权限模型（P4）：{职责域: "allow"|"deny"|"ask"}，替代逐工具勾选。
    # 域 → 具体工具的映射见 core/permissions.py 的 TOOL_DOMAIN；None 表示跟随默认域动作。
    domains: Optional[Dict[str, str]] = None
    # 高级微调：额外放行的未映射工具（MCP / 插件动态工具）
    extra_tools: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "mode": self.mode,
            "description": self.description,
            "model": self.model,
            "temperature": self.temperature,
            "tools": self.tools,
            "permissions": self.permissions,
            "hidden": self.hidden,
            "icon": self.icon,
            "domains": self.domains,
            "extra_tools": self.extra_tools,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentProfile":
        return cls(
            id=data.get("id", ""),
            mode=data.get("mode", "primary"),
            description=data.get("description", ""),
            system_prompt=data.get("system_prompt"),
            model=data.get("model"),
            temperature=data.get("temperature"),
            tools=data.get("tools"),
            permissions=data.get("permissions") or {},
            hidden=bool(data.get("hidden", False)),
            icon=str(data.get("icon") or ""),
            domains=data.get("domains"),
            extra_tools=list(data.get("extra_tools") or []),
        )


# ---------------------------------------------------------------- 内置默认 agent

BUILD_PROMPT = """你是一名资深软件工程师（Build Agent），在用户本地的开发环境中完成实际开发工作。
你拥有完整工具集（文件读写、精确编辑、命令执行、Git、代码审查、联网、多 Agent 编排）——能力边界以「可用工具」清单为准。

你的工作流（按序执行）：
1. 理解需求：先读相关文件/搜索代码，不凭记忆臆测；需求模糊时用 ask_user 澄清；
2. 制定方案：复杂任务先规划（涉及多文件/多步骤时用 todo_write 列 TODO，
   并在首个回复给出改动要点与影响面）；
3. 实现：修改前先 read_file 获取精确上下文；用 apply_search_replace /
   apply_unified_diff 做精确编辑，不做无谓的全文件重写；一次改动尽量小；
4. 验证：改完必须验证——跑相关测试/构建/命令，确认结果再汇报；
5. 汇报：按「完成内容 / 改动文件 / 验证情况 / 未完成事项」四段式交付，
   未验证的内容必须明说。

安全与边界：
- 涉及删除文件、强制推送、sudo 提权等破坏性操作前，先与用户确认；
- 需要用户提供信息（密钥、目标、偏好）时用 ask_user 选项式提问；
- 后台长任务（打包/安装）运行期间不改动工作树；
- 失败时分析错误换策略，不要用完全相同参数连续重试。
"""


PLAN_PROMPT = """你是一名资深技术规划师（Plan Agent），在用户本地的开发环境中做方案设计。
你的核心职责是「分析 → 规划 → 交付可执行计划」，不直接实施。

你的能力边界：
- 你只读（读文件/搜索/代码结构/git 查看/联网查证），并可用 plan_save 把计划
  保存到本会话的计划文件（.lite-work/plans/）。
- 你【没有】任何通用写工具（write_file / apply_search_replace /
  apply_unified_diff / delete_file / execute_command 不在你的工具清单中）。
  不要尝试调用它们——调用会直接失败。需要写文件/执行命令的需求，
  写入计划，由用户切到 Build Agent 执行。
- 用户说「执行/干/直接改/落地」等词时，那不是给你的指令：你应把计划
  更新得更精确，并提示用户切换到 Build Agent。

你的工作流：
1. 探查：先读关键文件、看目录结构、搜索相关代码，弄清现状再分析；
2. 规划：输出清晰、可执行、分步骤的实现计划——每步含涉及文件、改动点、
   验证方式；用 todo_write 列出 TODO 清单（每步 pending）；
3. 落盘：需要交接时用 plan_save 把完整计划写入计划文件
   （Markdown，含可执行步骤）；不需要落盘时直接在回复给出；
4. 确认：遇到方向性选择用 ask_user 弹选项，不擅自假设；
5. 汇报：交付「分析结论 + 计划」，并说明哪些信息不足/需要用户确认。

参考边界：规划可以涉及任何范围的改动建议，但执行永远留给 Build Agent。
"""


def default_build_agent() -> AgentProfile:
    return AgentProfile(
        id="build",
        mode="primary",
        description="默认开发 Agent：代码开发、文件编辑、Git 操作、Shell 执行。",
        system_prompt=BUILD_PROMPT,
        # 职责域全放行（plan 域 deny：计划文件写入是 Plan Agent 专属通道）
        domains={
            "read": "allow", "plan": "deny", "edit": "allow", "execute": "allow",
            "git_write": "allow", "web": "allow", "office": "allow",
            "collab": "allow", "interactive": "allow",
        },
    )


def default_plan_agent() -> AgentProfile:
    return AgentProfile(
        id="plan",
        mode="primary",
        description="规划 Agent：只读分析与方案设计，计划落盘（plan_save），不实施。",
        system_prompt=PLAN_PROMPT,
        # 只读 + 交互 + 派生子 Agent；唯一写通道 plan_save（plan 域 allow）
        domains={
            "read": "allow", "plan": "allow", "edit": "deny", "execute": "deny",
            "git_write": "deny", "web": "allow", "office": "deny",
            "collab": "allow", "interactive": "allow",
            "misc": "deny",
        },
    )


OFFICE_PROMPT = """你是一名办公生产力专家（Office Agent），帮助用户完成日常工作交付：
文档、表格、演示文稿、数据分析、图表与资料整理。你擅长把需求直接做成文件。

你的能力：
- 产出文件：docx_create（Word）/ xlsx_create（Excel）/ pptx_create（PPT）/
  pdf_create（PDF）/ chart_make（图表）/ data_analyze（数据分析），
  产出保存到工作区 .outputs/，完成后务必告知用户路径；
- 读取与再加工：docx_read / xlsx_read / pptx_read / pdf_read 读取既有
  办公文件；OCR 系列识别图片/扫描件内文字；可读写工作区普通文件；
- 长文档用 docx_append 增量追加，不为改一段重生成整篇。

你的工作流：
1. 需求确认：先明确交付物形态（Word/PPT/Excel/PDF）、受众与要点；
   关键数字/名称/日期必须来自用户或已读资料，绝不编造；
2. 大纲先行：长文档先给大纲让用户确认，再全文生成；
3. 图表保留：内容中的 PlantUML/Mermaid 代码块原样保留（工具自动渲染成图），
   不剔除不改写；UML 类图表一律用 PlantUML 而非 Mermaid 写；
4. 数据分析：先看数据结构与列名再分析，结论给出数字依据；
   大数据集先抽样预览，避免一次性输出全部行；
5. 自检：交付前检查——文件能否打开、数据是否准确、章节是否完整。

边界：
- 你【不】做代码开发：没有 git 提交、代码编辑、AST 工具；涉及写代码的
  任务提示用户切到 Build Agent；
- 执行命令仅在需要渲染图表等场景，且需用户确认（ask）；
- 数据不足用 ask_user 提问，不臆造业务数据。"""


def default_office_agent() -> AgentProfile:
    return AgentProfile(
        id="office",
        mode="primary",
        description="办公助手：Word/Excel/PPT/PDF、数据分析、图表、OCR。",
        system_prompt=OFFICE_PROMPT,
        # 办公全开 + 文件读写 + 联网 + 协作；execute 仅 ask（图表渲染）；
        # 无 git_write；plan 域 deny（不写计划文件）
        domains={
            "read": "allow", "plan": "deny", "edit": "allow", "execute": "ask",
            "git_write": "deny", "web": "allow", "office": "allow",
            "collab": "allow", "interactive": "allow",
        },
    )


RESEARCH_PROMPT = """你是一名调研分析师（Research Agent），帮助用户查证外部信息、整理资料并输出
结构化的调研报告或汇报材料。

你的工作流：
1. 澄清范围：需求模糊用 ask_user 提问（范围、深度、输出形态）；
2. 多源查证：webfetch / webfetch_batch 抓取权威来源，多来源交叉验证；
   不凭记忆臆测；每条关键结论标注来源 URL；抓取失败如实说明，不编造；
3. 结构化产出：先摘要（要点式）再详细分析；长报告边调研边用 docx_create +
   docx_append 落盘（每完成一个主题写一章）；数据对比用 xlsx_create /
   chart_make；汇报用 pptx_create；存档用 pdf_create；
4. 区分事实与观点：客观陈述标来源，推断与建议单独标明；
5. 自检：交付前检查——结论是否有来源、是否有未验证假设、是否有遗漏面。

边界：
- 你不做代码开发与命令执行：只读文件 + 联网 + 生成办公文档；
  涉及写代码/跑脚本的需求提示用户切到 Build Agent；
- 产出物写到工作区（.outputs/），不修改业务代码文件。"""


def default_research_agent() -> AgentProfile:
    return AgentProfile(
        id="research",
        mode="primary",
        description="调研助手：网络查证、多源交叉、调研报告与汇报材料。",
        system_prompt=RESEARCH_PROMPT,
        # 只读 + 联网 + 办公产出 + 协作；无 edit/execute/git_write；
        # plan 域 deny（不写计划文件）
        domains={
            "read": "allow", "plan": "deny", "edit": "deny", "execute": "deny",
            "git_write": "deny", "web": "allow", "office": "allow",
            "collab": "allow", "interactive": "allow",
            "misc": "deny",
        },
    )


# ---------------------------------------------------------------- Agent 注册表

# 内置 agent id（覆盖文件只冻结 tools/permissions，人格字段跟随发版）
BUILTIN_AGENT_IDS = ("build", "plan", "office", "research")


class AgentRegistry:
    """管理内置 + 用户自定义的 Agent。

    自定义 agent 来源（参考 OpenCode）：
    1. config.json 的 "agents" 段（JSON 格式）；
    2. .lite-work/agents/*.json 目录下的 agent 描述文件。
    """

    def __init__(self) -> None:
        self._agents: Dict[str, AgentProfile] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        self._agents["build"] = default_build_agent()
        self._agents["plan"] = default_plan_agent()
        self._agents["office"] = default_office_agent()
        self._agents["research"] = default_research_agent()

    # ------------------------------------------------------------ 查询

    def get(self, agent_id: str) -> AgentProfile:
        if agent_id not in self._agents:
            raise KeyError(f"未知 Agent: {agent_id}（可用: {', '.join(self.list_primary())}）")
        return self._agents[agent_id]

    def list_primary(self) -> List[str]:
        return [a.id for a in self._agents.values()
                if a.mode in ("primary", "all") and not a.hidden]

    def list_subagents(self) -> List[str]:
        return [a.id for a in self._agents.values()
                if a.mode in ("subagent", "all") and not a.hidden]

    def all(self) -> Dict[str, AgentProfile]:
        return dict(self._agents)

    def to_config(self) -> Dict[str, Any]:
        return {aid: p.to_dict() for aid, p in self._agents.items()}

    # ------------------------------------------------------------ 加载自定义

    def load_dir(self, agents_dir: str) -> None:
        """扫描 agents 目录下的 *.json / *.md 文件并注册自定义 agent。

        - .json：AgentProfile 的字段
        - .md：YAML frontmatter（AgentProfile 字段）+ 正文（system_prompt），
          文件名即 agent id（对齐 OpenCode 的 markdown agent）。
        """
        if not os.path.isdir(agents_dir):
            return
        for fname in sorted(os.listdir(agents_dir)):
            path = os.path.join(agents_dir, fname)
            base, ext = os.path.splitext(fname)
            try:
                if ext == ".json":
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    profile = AgentProfile.from_dict(data)
                    if not profile.id:
                        profile.id = base
                elif ext == ".md":
                    with open(path, "r", encoding="utf-8") as f:
                        raw = f.read()
                    data, body = parse_frontmatter(raw)
                    profile = AgentProfile.from_dict(data)
                    profile.id = profile.id or base
                    if body.strip() and not profile.system_prompt:
                        profile.system_prompt = body.strip()
                else:
                    continue
                self.register(profile)
                logger.info("[Agent] 已加载自定义 agent: %s (%s)", profile.id, path)
            except Exception:
                logger.exception("[Agent] 加载 agent 失败: %s", path)

    def load_config(self, agents_cfg: Optional[Dict[str, Any]]) -> None:
        """从 config.json 的 "agents" 段加载自定义 agent。"""
        if not agents_cfg:
            return
        for aid, data in agents_cfg.items():
            if not isinstance(data, dict):
                continue
            try:
                profile = AgentProfile.from_dict({**data, "id": data.get("id", aid)})
                self.register(profile)
                logger.info("[Agent] 已从配置注册 agent: %s", profile.id)
            except Exception:
                logger.exception("[Agent] 注册 agent 失败: %s", aid)

    def register(self, profile: AgentProfile) -> None:
        """注册（新增或覆盖）一个 agent。

        内置 agent 覆盖文件通常只含 tools/permissions（minimal 持久化）：
        其余字段（system_prompt/描述/模型）为空时继承当前内置默认，
        使人格修复与增强随主程序发版自动跟进，不被旧覆盖文件冻结。

        旧配置迁移：无 domains 时从 tools/permissions 反推（deny 优先），
        保证存量 agents/*.json 升级后职责域模型可用、安全语义不变。
        """
        existing = self._agents.get(profile.id)
        if existing is not None and profile.id in BUILTIN_AGENT_IDS:
            if profile.system_prompt is None:
                profile.system_prompt = existing.system_prompt
            if not profile.description:
                profile.description = existing.description
            if profile.model is None:
                profile.model = existing.model
            if profile.temperature is None:
                profile.temperature = existing.temperature
            if not profile.icon:
                profile.icon = existing.icon
        self._infer_domains_from_legacy(profile)
        self._agents[profile.id] = profile

    @staticmethod
    def _infer_domains_from_legacy(profile: AgentProfile) -> None:
        """旧 tools+permissions 配置 → 职责域反推（仅当未声明 domains 时）。

        - 白名单里有某域工具 → 该域 allow；
        - permissions 显式 deny 的工具 → 其域 deny（最高优先）；
        - permissions 显式 ask 的工具 → 其域 ask（除非已被 deny）；
        - 完全没工具（tools=None）→ 保持 None（跟随默认：偏安全）。
        """
        if profile.domains:
            return
        if profile.tools is None:
            return
        from .permissions import TOOL_DOMAIN

        deny_set = {t for t, a in (profile.permissions or {}).items() if a == "deny"}
        ask_set = {t for t, a in (profile.permissions or {}).items() if a == "ask"}
        inferred: Dict[str, str] = {}
        for t in profile.tools:
            dom = TOOL_DOMAIN.get(t)
            if not dom or dom == "misc":
                continue
            if dom not in inferred or inferred[dom] == "deny":
                inferred[dom] = "allow"
        for t in deny_set:
            dom = TOOL_DOMAIN.get(t)
            if dom:
                inferred[dom] = "deny"
        for t in ask_set:
            dom = TOOL_DOMAIN.get(t)
            if dom and inferred.get(dom) != "deny":
                inferred[dom] = "ask"
        if inferred:
            profile.domains = inferred

    def delete(self, agent_id: str) -> None:
        """删除一个自定义 agent（内置 agent 用 register(默认) 恢复，不走这里）。"""
        self._agents.pop(agent_id, None)

    def save(self, profile: AgentProfile, agents_dir: str) -> str:
        """持久化一个自定义 agent 到 agents 目录（JSON 文件）。"""
        os.makedirs(agents_dir, exist_ok=True)
        path = os.path.join(agents_dir, f"{profile.id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(profile.to_dict(), f, ensure_ascii=False, indent=2)
        self.register(profile)
        return path
