# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""静态 System Prompt 骨架（对应课程第3课 DynamicSystemPromptBuilder）。

System 只含任务内恒定内容（角色 / 环境 / 工具摘要 / 规则），保证缓存前缀稳定；
git 状态等会随时间变化的信息由 git_status 工具按需获取。
"""
from __future__ import annotations

import platform
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .types import ToolDefinition


# This instruction is intentionally explicit and duplicated in the numbered rules:
# agents must always leave the user with a completion report, not just stop after tools.
FINAL_REPORT_REQUIREMENT = """### 强制交付要求（必须遵守）
任务结束前必须向用户汇报结果，不能只执行工具后停止，也不能只回复“已完成”。最终回复必须使用简洁的 Markdown，至少包含：
- **完成内容**：实际完成了什么；
- **改动文件/关键结果**：涉及哪些文件或得到什么结论（如适用）；
- **验证情况**：运行了哪些测试、构建或检查，以及结果；
- **未完成事项**：仍有问题、风险或未验证内容必须明确说明，没有则写“无”。
如果任务因失败、停止、超时或达到步骤上限而结束，也必须向用户说明当前状态、原因和后续建议。"""

# 内容展示偏好：AI 自选最佳展示形式（intelligent UI / generative UI 思路——
# Agent 输出「数据 + 组件类型」，前端用设计系统原生渲染，不手写整页 HTML），
# 核心是区分「展示型回复」（聊天内直接渲染）与「文件型交付」（导出 docx/pdf/md 等）。
# 精简版（v2 性能优化）：每类围栏只留一行「什么时候用 + 关键形态」，完整 JSON
# 语法细节移到 display_manual.MANUAL——首次使用对应围栏时由 Agent 按需取用
# （对齐 Muse 多级工具思路：L0 常驻速查，L1 按需手册），System Prompt 省 ~2k tokens。
CONTENT_DISPLAY_PREFERENCE = """### 内容展示偏好（必须遵守）
你有多种展示形式，**按内容特点自己选最合适的一种**，不要一律落盘成文件，
也不要动辄手写整页 HTML——优先用系统能**原生渲染**的形式：
- **Markdown 表格 / 列表**：简单对比（2-4 项、几个维度）、结论摘要——最轻量，优先它；
- **```chart**：原生图表（bar/line/pie/barh + scatter/boxplot/histogram/heatmap/
  combo/waterfall/stacked 等专业统计图），围栏内写紧凑 JSON，聊天气泡内直接
  渲染，**不要**为此生成图片或 HTML 文件；图表右上角有 ⇩ 导出菜单；
- **```cards**：实体卡片（商品/方案对比，带图带价带标签）；
- **```kpi**：指标卡（3-4 个关键数字 + 环比涨跌）——汇报/复盘/体检结论首选；
- **```timeline**：时间线（带时间的进展/事件序列，status: done/active/pending）；
- **```table**：数据表（窄栏多列数值对比）；
- **```compare**：左右对比（前后差异/取舍，tone: good/bad/neutral）；
- **```board**：看板（分组列：待办/进行中/完成/阻塞）；
- **```progress**：进度条（多目标完成度，0-100 自动色阶）；
- **```interactive**：交互式图解（点击分块看说明，适合结构/原理拆解）；
- **```diagram**：图形图解（SVG 原语画示意图，有空间比例的结构用）；
- **```mermaid**：流程图/时序图/状态图/架构图**一律用它**（自动布局最可靠）；
- **```layout**：语义拓扑图（节点+边，坐标前端算，适合关系/系统组成）；
  三图选择：流程/时序/状态 → mermaid；关系拓扑 → layout；有空间比例 → diagram；
- **```html 围栏**：仅当确有独特版式需求（海报/页面原型）才手写；
- **render_card**：引用本地图片的复杂 HTML 卡片；
- **文件交付（docx/pdf/md/xlsx）**：仅在用户**明确要交付物**时。「展示/预览/
  看看效果」= 聊天内渲染，**不是**生成文件——展示型回复保留简短文字说明即可。
富围栏是**流式渐进渲染**的，可放在回答开头让用户先看到结论。各围栏的完整
JSON 字段与语法细节，首次使用时用 `read_manual` 工具查阅（只查一次）。
引用本地图片一律写 workspace 相对路径（不要绝对路径、不要 /api/ URL）。"""


# 中间产物治理：回答问题 ≠ 生成文件；做完即清理自己产生的临时产物。
# 个人助理定位——用户的感受应该是"问完就有答案，做完就干净"，而不是
# 每问一个问题 workspace 里就多一堆文档/脚本/草稿。
INTERMEDIATE_ARTIFACTS_RULE = """### 中间产物与收尾清理（必须遵守）
- **回答问题不落盘**：用户只是问一件事、要一个解释或结论时，直接在回复里
  给出（展示形式按「内容展示偏好」选），**不要**为此生成文档、脚本、草稿等
  任何文件；
- **确需中间文件时**（格式转换、临时脚本、下载缓存、调试产物等），集中放进
  一个明确的临时子目录，不要散落在 workspace 根目录；
- **做完即清理**：任务结束（最终回复 / 交付完成）前，删除过程中**你自己产生的**
  临时与中间文件（delete_file / shell 清理临时目录均可），只保留用户明确要求
  的交付物与应有的代码改动——干净收尾，不留垃圾。绝不删除用户的既有文件。"""


# 工具行摘要（snippet）的人工覆盖表：name → 一行摘要。
# 默认由 description 自动摘（首句 / 首个分句，≤ SNIPPET_MAX_CHARS），只在自动摘
# 出来没用时（如 astock_watch 的「盯盘与自选股管理。」丢掉 action 列表）才在这里补。
TOOL_SNIPPETS: Dict[str, str] = {
    "astock_watch": "盯盘规则与自选股管理（status/plan/set_rules/start/stop）",
    "spawn_agent": "派生一个子 Agent 并行执行任务（结果异步送达）",
}

# 一行摘要的字符上限。60 大约是一句话的信息量，超过就只截到最近的标点，
# 保证「一眼知道这工具干什么」而不复述参数细节——细节在 tool schema 里。
SNIPPET_MAX_CHARS = 60

# 摘要截断时优先在这些标点处断开（按顺序尝试），避免把词组切成半个
_SNIPPET_BREAKS = ("。", "；", "，", "、", ", ", " ")


def tool_snippet(tool: ToolDefinition) -> str:
    """工具的一行摘要：System Prompt 工具清单用，**不替代** tool schema 的 description。

    为什么需要：tool schema 已经把完整 description 投喂给模型，System Prompt 的
    工具清单若再贴一遍全文，同一段文字每次请求都要付两遍钱（lite-work 现状：
    93 个工具 ≈ 11.4k tokens，其中绝大部分与 schema 重复）。这里只留「一行
    能定位工具」的信息，完整语义交给 schema（pi 的 promptSnippet 同思路）。
    """
    override = TOOL_SNIPPETS.get(tool.name)
    if override:
        return override
    text = re.sub(r"[*`>#]", "", tool.description or "")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "—"
    # 首句：句号/分号在合理长度内出现就以它断（整句语义完整）
    for sep in ("。", "；"):
        idx = text.find(sep)
        if 0 < idx <= SNIPPET_MAX_CHARS:
            return text[:idx + 1]
    if len(text) <= SNIPPET_MAX_CHARS:
        return text
    # 超长：退到最近的逗号/顿号/空格，避免半个词
    for sep in _SNIPPET_BREAKS:
        idx = text.rfind(sep, 0, SNIPPET_MAX_CHARS)
        if idx > SNIPPET_MAX_CHARS // 2:
            return text[:idx] + "…"
    return text[:SNIPPET_MAX_CHARS] + "…"


class SystemPromptBuilder:
    """静态 System Prompt 组装器：同一任务内所有 LLM 调用共享同一份 system 内容。"""

    @staticmethod
    def _pending_groups_section(loadout: Optional[Any]) -> str:
        """「按需工具组」段：告诉模型哪些领域工具要先 load_tools 装载。

        这段是渐进式披露的**路由信息**——没有它模型不知道自己缺工具，
        只会看到「可用工具」里没有 office 就以为做不到。组清单是静态的
        （不随装载变化），保证缓存前缀稳定。
        """
        if loadout is None:
            return ""
        groups = loadout.pending_groups() if hasattr(loadout, "pending_groups") else []
        if not groups:
            return ""
        lines = [
            "",
            "",
            "### 按需工具组 (On-Demand Tool Groups)",
            "下列领域工具**默认不装载**（不占上下文）。需要其中任何能力时，",
            "先调用 `load_tools(group=\"组名\")` 装载该组，再调用具体工具；",
            "一次可装一组，装载后本任务内一直可用。",
        ]
        for group in groups:
            tools_preview = "、".join(group.tools[:6])
            if len(group.tools) > 6:
                tools_preview += f" 等 {len(group.tools)} 个"
            lines.append(f"- **{group.name}**：{group.summary}")
            lines.append(f"  含：{tools_preview}")
        return "\n".join(lines)

    @staticmethod
    def _git_info(cwd: str) -> str:
        try:
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=cwd,
                capture_output=True,
                text=True, encoding='utf-8', errors='replace',
                timeout=3,
            )
            branch_name = branch.stdout.strip() if branch.returncode == 0 else "N/A"
            status = subprocess.run(
                ["git", "status", "--short"],
                cwd=cwd,
                capture_output=True,
                text=True, encoding='utf-8', errors='replace',
                timeout=3,
            )
            changed = len([l for l in status.stdout.splitlines() if l.strip()])
            return f"分支: {branch_name} | 未提交改动文件数: {changed}"
        except Exception:
            return "不是 Git 仓库 / Git 不可用"

    @staticmethod
    def _global_rules() -> str:
        """全局规则（~/.lite-work/RULES.md）：用户跨项目的稳定行为约束。

        等价于「全局 AGENTS.md」——lite-work 版的 dots custom rules。与项目
        AGENTS.md 的关系：全局先注入（基线），项目指令后注入可覆盖（同名
        冲突时以项目为准，符合就近原则）。文件不存在 → 空串，prompt 与旧版
        逐字节一致（缓存前缀不受影响）。
        """
        path = Path.home() / ".lite-work" / "RULES.md"
        try:
            if not path.is_file():
                return ""
            content = path.read_text(encoding="utf-8").strip()
            return content
        except OSError:
            return ""

    @staticmethod
    def _project_instructions(cwd: str) -> str:
        """读取 workspace 根目录的项目指令文件。"""
        sections = []
        root = Path(cwd).resolve()
        for filename in ("AGENTS.md", "Claude.md", "CLAUDE.md"):
            path = root / filename
            if not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if content:
                sections.append(f"### {filename}\n{content}")
        return "\n\n".join(sections)

    @classmethod
    def build(cls, cwd: str, tools: List[ToolDefinition], agent_prompt: Optional[str] = None,
              skill_extra: Optional[str] = None, skill_index: Optional[str] = None,
              loadout: Optional[Any] = None) -> str:
        """构建任务级静态 System Prompt。

        agent_prompt：Agent 专属角色提示（如 Plan 的规划型人格）。注入位置在
        共享的「强制交付要求」之后、环境信息之前——两个 Agent 仍共享最前面的
        交付要求段；agent_prompt 为 None 时（build）输出与旧版逐字节一致，
        已有会话的 Prompt 缓存前缀不受影响。
        skill_extra：本任务显式/自动命中的技能内容（/skill 命令或 triggers
        匹配），追加到技能索引段之后。任务级注入——不进会话历史，任务内
        所有调用共享（缓存前缀稳定），任务结束即消失。
        loadout：按需工具组视图（tools/loadout.py）。有按需组时追加
        「按需工具组」段，告诉模型哪些领域能力要先 load_tools 装载。
        """
        os_name = f"{platform.system()} {platform.release()} ({platform.machine()})"
        # 按需工具不在「可用工具」里逐条列（它们在「按需工具组」段按组说明），
        # 否则同一批工具名在 prompt 里出现两遍。骨架工具才逐条列。
        deferred_names = set()
        if loadout is not None and getattr(loadout, "enabled", False):
            deferred_names = set(getattr(loadout, "tool_group", {}) or {})
        listed = [t for t in tools if t.name not in deferred_names]
        tools_summary = "\n".join(f"- **{t.name}**: {tool_snippet(t)}" for t in listed)
        pending_section = cls._pending_groups_section(loadout)
        project_instructions = cls._project_instructions(cwd)
        skill_index = skill_index if skill_index is not None else ""
        if not skill_index:
            try:
                from ..tools.skills import SkillsTools
                skill_index = SkillsTools(cwd).index()
            except Exception:
                skill_index = "（技能索引不可用）"
        global_rules = cls._global_rules()
        global_rules_section = (
            "\n\n### 全局规则 (Global Rules)\n"
            "以下是用户设置的全局行为规则，适用于所有项目（项目指令可就近覆盖）：\n"
            f"{global_rules}"
            if global_rules else ""
        )
        instruction_section = (
            "\n\n### 项目指令 (Project Instructions)\n"
            "以下内容来自 workspace 中的项目指令文件，请在不违反系统安全规则的前提下遵守：\n"
            f"{project_instructions}"
            if project_instructions else ""
        )
        # 项目运行环境清单（.litework/project.json）——安装/验证命令进稳定层。
        # 无清单 → 空串，prompt 不变（保证已有会话的缓存前缀不受影响）。
        from .runtime_manifest import load_runtime_manifest, runtime_summary_for_prompt
        runtime_section = runtime_summary_for_prompt(load_runtime_manifest(cwd))
        skill_section = (
            "\n\n### 可用技能 (Skills)\n"
            "需要专项流程时，使用 `load_skill` 按名称加载完整 SKILL.md；不要猜测技能内容。\n"
            f"{skill_index}"
        )
        if skill_extra:
            skill_section += (
                "\n\n### 本任务已加载技能\n"
                "以下技能内容由 /skill 命令或自动匹配注入，请优先遵循其指引：\n"
                f"{skill_extra}"
            )
        # Agent 角色段：专属提示优先（Plan/自定义 Agent），否则通用角色行
        role_section = agent_prompt.strip() if agent_prompt and agent_prompt.strip() else (
            "你是一个专业的 AI 软件工程师 Code Agent，运行在用户本地的开发环境中。"
        )

        return (FINAL_REPORT_REQUIREMENT + "\n\n" + CONTENT_DISPLAY_PREFERENCE
                + "\n\n" + INTERMEDIATE_ARTIFACTS_RULE + "\n\n" + f"""{role_section}

### 环境信息 (Environment Context)
- **操作系统**: {os_name}
- **当前工作目录**: `{cwd}`

### 可用工具 (Available Tools)
{tools_summary}{pending_section}

### 工作规则 (Operating Rules)
1. 修改代码前，先用工具探查代码库结构与相关文件内容，不要盲目猜测；
2. 修改文件使用 apply_search_replace / apply_unified_diff 等精确编辑工具，
   避免整文件重写；编辑前先 read_file 获取精确上下文；
3. 需要执行命令时使用 execute_command；命令失败时分析错误输出并换一种策略，
   不要连续用完全相同参数重试同一个失败的工具；你的工具清单之外的能力一律不要尝试；
4. 涉及删除、强制推送、sudo 提权等操作时，系统会要求用户确认；
5. 用简洁的 Markdown 回复用户；中文优先；
6. 涉及多个独立模块、需要广泛搜索或可并行调研时，优先使用 spawn_agent 的 explorer 角色；
7. **完成工作后必须向用户提交完整汇报**：说明完成内容、改动文件/关键结果、验证情况和未完成事项；绝不能在工具调用后无回复结束。
8. 你的能力边界由「可用工具」清单决定：未列出的工具不可调用，不要尝试调用不存在的工具名；需要的能力不在清单内时，明确告知用户切换对应 Agent。{global_rules_section}{instruction_section}{runtime_section}{skill_section}

"""
        )
