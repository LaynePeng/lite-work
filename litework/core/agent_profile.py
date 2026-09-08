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
        )


# ---------------------------------------------------------------- 内置默认 agent

PLAN_PROMPT = """你是一个「规划型」AI 软件工程师（Plan Agent），运行在用户本地的开发环境中。当前模式只做分析与规划，**禁止修改任何文件、禁止执行命令**。

你的职责：
1. 理解用户需求，先探查代码库结构与相关文件内容；
2. 输出一份清晰、可执行、分步骤的实现计划（含涉及文件、改动点、验证方式），并用 todo_write 把计划写成 TODO 清单（每步一项，status 均为 pending）；
3. 除非用户明确要求，否则不要动手改代码。

你的可用工具受限为只读（读文件、搜索、git 状态查看、ask_user 提问等）。如果某个步骤需要写文件或执行命令，把它写进计划，由用户切换到 Build Agent 执行。
遇到需要与用户确认、选择或提供信息才能继续的场景，使用 ask_user 工具弹出选项式提问，而不是要求用户复述回答。
"""


def default_build_agent() -> AgentProfile:
    return AgentProfile(
        id="build",
        mode="primary",
        description="默认开发 Agent：代码开发、文件编辑、Git 操作、Shell 执行。",
        tools=[
            # 代码开发
            "read_file", "write_file", "list_dir", "file_tree",
            "search_code", "get_file_outline", "read_focused_symbol",
            "apply_search_replace", "apply_unified_diff",
            "execute_command",
            # Git
            "git_status", "git_diff", "git_log", "git_commit", "git_branch",
            # 代码审查
            "review_code",
            # 联网 / 技能 / 子任务
            "webfetch", "webfetch_batch", "load_skill", "spawn_sub_agent",
            # 多 Agent 协作（P1 异步派生）
            "spawn_agent", "list_agents", "close_agent", "wait_agents",
            # 流程
            "todo_write", "ask_user",
        ],
        permissions={},
    )


def default_plan_agent() -> AgentProfile:
    return AgentProfile(
        id="plan",
        mode="primary",
        description="规划 Agent：只读分析与方案设计，禁止修改文件与执行命令。",
        system_prompt=PLAN_PROMPT,
        # 只读工具白名单 + todo_write + ask_user（规划产物与讨论）
        tools=[
            "todo_write",
            "ask_user",
            "read_file", "list_dir", "file_tree", "search_code",
            "get_file_outline", "read_focused_symbol",
            "git_status", "git_diff", "git_log", "git_branch", "review_code",
            "webfetch", "webfetch_batch",
            "load_skill",
        ],
        # 权限兜底：即使被授予写工具也强制 deny
        permissions={"write_file": PERM_DENY, "apply_search_replace": PERM_DENY,
                     "apply_unified_diff": PERM_DENY, "execute_command": PERM_DENY},
    )


OFFICE_PROMPT = """你是一个通用办公助手（Office Agent），帮助用户完成日常工作：写作、制表、演示文稿、数据分析与资料整理。当前不聚焦于软件开发。

工作准则：
1. 产出文件：需要交付文档/表格/演示时，优先使用办公工具直接生成文件——
   docx_create（Word）、xlsx_create（Excel）、pptx_create（PPT）、pdf_create（PDF）、
   chart_make（图表 PNG）、data_analyze（数据统计）；
   工具输出会给出文件保存路径，完成后务必把路径告知用户。
2. 文档内容用规范的 Markdown 编写（标题层级/列表/表格），工具会自动排版；
   长文档先给用户看大纲，确认后再生成全文；生成后需要补充或修改章节时
   用 docx_append 在既有 docx 上迭代追加（可配 page_break 分页），
   不要为改一段而重新生成整篇。
3. 数据分析：先看数据结构与列名，再执行分析；结论要给出数字依据；
   大数据集先抽样预览，避免一次性输出全部行。
4. 信息不足时用 ask_user 向用户提问（提供选项），不要凭空编造业务数据；
   用户提供的数字、名称、日期必须原样保留，不得改写。
5. 需要外部资料时用 webfetch / webfetch_batch 查证，并在文档中注明来源。
6. 复杂任务先用 todo_write 列出步骤清单，逐步执行并更新进度。
7. 图表必须保留：用户内容中的 PlantUML / Mermaid 代码块（时序图、架构图、
   流程图等）必须原样放进 docx_create / pdf_create 的 content 或 pptx 的
   content——不要剔除、不要改写、不要转成文字描述；工具会自动把图表
   代码块渲染成图片嵌入文档（渲染失败会保留源码文本，内容不丢）。
   需要精细控制图表（多图命名/暗色模式等）时才用 load_skill 加载
   diagram-to-office 技能自行渲染；UML 类图表一律用 PlantUML 而非
   Mermaid 写。

你可以读写工作区内的文件，但没有 git 与代码编辑能力；如任务涉及写代码，
提示用户切换到 build Agent。"""


def default_office_agent() -> AgentProfile:
    return AgentProfile(
        id="office",
        mode="primary",
        description="通用办公助手：写文档、做表格、生成 PPT、数据分析与图表。",
        system_prompt=OFFICE_PROMPT,
        tools=[
            # 办公产出（docx_append：在既有 Word 上迭代追加章节）
            "docx_create", "docx_append", "xlsx_create", "pptx_create", "pdf_create",
            "data_analyze", "chart_make",
            # 办公文件读取
            "docx_read", "xlsx_read", "pptx_read", "pdf_read",
            # OCR 识别（图片/PDF/PPT 内嵌图片文字）
            "ocr_image", "ocr_document", "ocr_pptx",
            # 图表渲染脚本（diagram-to-office 技能：plantuml/mermaid 转图片）
            "execute_command",
            # 文件读写与浏览
            "read_file", "write_file", "list_dir", "file_tree",
            # 资料获取
            "webfetch", "webfetch_batch",
            # 流程与交互
            "todo_write", "ask_user", "load_skill", "spawn_sub_agent",
            # 多 Agent 协作（P1 异步派生）
            "spawn_agent", "list_agents", "close_agent", "wait_agents",
        ],
        permissions={
            "execute_command": PERM_ASK,
        },
    )


RESEARCH_PROMPT = """你是一个调研分析助手（Research Agent），帮助用户查证外部信息、整理资料并输出结构化报告。

工作准则：
1. 信息查证：优先使用 webfetch / webfetch_batch 抓取权威来源；
   多来源交叉验证，不凭记忆臆测，注明每条关键结论的来源 URL；
   抓取失败时明确告知，不要编造内容。
2. 结构化输出与文档落盘：调研结果先给摘要（要点式），再给详细分析；
   - 长报告用 docx_create 生成后按章节 docx_append 迭代追加（每调研完
     一个主题就写入，不要等全部完成才动笔）；
   - 汇报材料用 pptx_create 生成演示文稿，数据对比用 xlsx_create 表格
     或 chart_make 图表呈现，需要 PDF 存档时用 pdf_create；
   - 已有的 Word/PPT/Excel/PDF 资料先用 docx_read / pptx_read /
     xlsx_read / pdf_read 读取，在既有内容基础上续写或引用。
3. 信息不足或需求模糊时用 ask_user 提问（提供选项）澄清范围。
4. 复杂调研（多主题/多来源）先用 todo_write 拆分任务，可用 spawn_sub_agent
   并行调研不同子主题后汇总。
5. 区分事实与观点：客观陈述标注来源，推断与建议单独标明。"""


def default_research_agent() -> AgentProfile:
    return AgentProfile(
        id="research",
        mode="primary",
        description="调研分析助手：网络查证、资料整理、生成调研报告与汇报材料。",
        system_prompt=RESEARCH_PROMPT,
        tools=[
            # 资料获取
            "webfetch", "webfetch_batch",
            # 文档产出与迭代写作（调研结果落盘）
            "docx_create", "docx_append", "pptx_create", "pdf_create",
            "xlsx_create", "chart_make",
            # 文件读取（本地资料/数据/OCR 识别）
            "read_file", "list_dir", "file_tree",
            "docx_read", "xlsx_read", "pptx_read", "pdf_read",
            "ocr_image", "ocr_document", "ocr_pptx",
            # 流程与交互
            "todo_write", "ask_user", "load_skill", "spawn_sub_agent",
            # 多 Agent 协作（P1 异步派生）
            "spawn_agent", "list_agents", "close_agent", "wait_agents",
        ],
        permissions={},
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
        self._agents[profile.id] = profile

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
