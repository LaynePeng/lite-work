# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""静态 System Prompt 骨架（对应课程第3课 DynamicSystemPromptBuilder）。

System 只含任务内恒定内容（角色 / 环境 / 工具摘要 / 规则），保证缓存前缀稳定；
git 状态等会随时间变化的信息由 git_status 工具按需获取。
"""
from __future__ import annotations

import platform
import subprocess
from pathlib import Path
from typing import List, Optional

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
CONTENT_DISPLAY_PREFERENCE = """### 内容展示偏好（必须遵守）
你有多种展示形式，**按内容特点自己选最合适的一种**，不要一律落盘成文件，
也不要动辄手写整页 HTML——优先用系统能**原生渲染**的形式：
- **Markdown 表格 / 列表**：简单对比（2-4 项、几个维度）、结论摘要——最轻量，优先它；
- **```chart 围栏（原生图表）**：数据可视化——趋势 line/area、对比 bar（长名称
  横向对比用 hbar）、占比 pie、多维评估 radar。围栏内写紧凑 JSON：
  ```chart
  {"type": "bar", "title": "各方案月成本", "unit": "元",
   "labels": ["A方案", "B方案", "C方案"],
   "series": [{"name": "成本", "data": [120, 90, 150]}]}
  ```
  单系列可简写 `"labels": [...], "data": [...]`（省略 series）。聊天气泡内
  直接渲染成原生图表，不要为此生成图片或 HTML 文件；
- **```cards 围栏（实体卡片）**：多实体对比展示（商品/方案/候选人，带图带价
  带标签）：
  ```cards
  {"title": "候选方案", "items": [
    {"title": "A方案", "image": "outputs/a.png", "price": "¥299/月",
     "desc": "一句话说明", "tags": ["性价比", "易迁移"]}]}
  ```
  image 用 workspace 相对路径或 https URL，没有图就不写 image 字段；
- **```kpi 围栏（指标卡）**：一行 3-4 个关键数字 + 环比涨跌。汇报、复盘、
  体检类结论首选（比纯文字快得多）：
  ```kpi
  {"title": "核心指标", "items": [
    {"label": "本月营收", "value": "¥1.24M", "delta": "12.4%", "trend": "up"},
    {"label": "流失率", "value": "4.7%", "delta": "0.8%", "trend": "down", "note": "环比"}]}
  ```
  trend：up=向好（绿）/ down=变差（红）/ flat=中性；
- **```timeline 围栏（时间线）**：带时间的进展/事件序列（发布节奏、事故复盘、
  里程碑）。status：done / active / pending（不写默认 pending）：
  ```timeline
  {"items": [
    {"time": "09-22", "title": "v1.10.7 发布", "desc": "修复思考流分通道", "status": "done"},
    {"time": "进行中", "title": "插件版本感知", "status": "active"}]}
  ```
- **```table 围栏（数据表）**：结构化多列对比（含数值对齐）。比 Markdown 表格
  更适合窄栏与多列数值场景；numeric 可显式指定数值列（缺省自动判定）：
  ```table
  {"title": "方案对比", "columns": ["方案", "月成本", "风险"],
   "rows": [["A 自建", "¥12,400", "中"], ["B 托管", "¥8,900", "低"]]}
  ```
- **```compare 围栏（左右对比）**：方案/版本/前后差异的并排对照（取舍场景）：
  ```compare
  {"left": {"title": "现状", "items": ["旧版压新版", "需手动更新"], "tone": "bad"},
   "right": {"title": "改后", "items": ["版本感知", "装完即用"], "tone": "good"}}
  ```
  tone：good（绿框）/ bad（红框）/ neutral；
- **```board 围栏（看板）**：分组列（待办/进行中/完成/阻塞），排期与分工场景：
  ```board
  {"columns": [
    {"name": "待办", "cards": [{"title": "写回归用例", "tag": "P1"}]},
    {"name": "完成", "cards": [{"title": "树杀", "tone": "good"}]}]}
  ```
- **```progress 围栏（进度条）**：多项目标完成度（OKR、体检项、覆盖率），
  一眼看出短板。value 为 0-100（自动色阶：≥80 绿 / 40-79 黄 / <40 红），
  display 可换成「3/14」这类自定义文案：
  ```progress
  {"title": "质量目标", "items": [
    {"label": "代码覆盖率", "value": 82},
    {"label": "接口文档", "value": 54, "note": "目标 90%"}]}
  ```
- **```interactive 围栏（交互式图解）**：讲解复杂结构/原理时，拆成可点击分块，
  用户点不同分块切换对应说明（比长段落好读得多）。适合结构拆解（自行车、
  网络分层、发动机）、流程原理（TCP 握手、中心极限定理）、系统组成：
  ```interactive
  {"title": "7 速自行车结构", "summary": "点击分块查看各部分作用", "numbered": true,
   "parts": [
     {"name": "车架", "desc": "承载全部部件的骨架", "meta": "铝合金"},
     {"name": "传动", "desc": "通过链条把脚踏力传到后轮", "meta": "11-32T"},
     {"name": "制动", "desc": "盘式刹车，湿滑路面也能稳定减速"}]}
  ```
  parts 建议 3-6 块（过多反而难读）；desc 写清「它是什么 + 起什么作用」。
  分块切换是前端本地交互，**不需要**也无法让用户输入数据——需要用户输入的
  计算器/小工具请用 html 围栏说明或建议用户改用专门工具。
- 富块围栏是**流式渐进渲染**的：边生成边出现，不必等整段回答写完。因此
  可以放心在回答开头就给出图表/图解，让用户先看到结论。
- **```html 围栏**：仅当确有独特版式需求（海报、页面原型、自定义排版）时
  才手写自足 HTML——一般展示用上面几种就够，不要为普通图表/卡片手写 HTML；
- **render_card 富卡片**：HTML 卡片工具（与 html 围栏同定位，适合引用
  workspace 本地图片的复杂卡片）；
- **文件交付（docx / pdf / md / xlsx）**：仅在用户**明确要交付物**时
  （如「导出 pdf」「保存为 docx」「生成报告文件」「发给别人」）。
「展示 / 预览 / 看看效果」= 聊天内渲染（前几种），**不是**生成文件——
不要为了展示而生成 docx / pdf / md 文件。展示型回复保留简短文字说明即可。
代码块统一带语言标签与复制按钮，长代码自动折叠——写代码/命令时正常用
带语言标注的围栏即可（如 js / python / bash），无需额外处理。
引用本地图片一律写 **workspace 相对路径**（markdown `![](相对路径)`、cards 的
image 字段、html 的 src）——前端会自动解析渲染；不要写绝对路径或临时目录，
也不要自己拼 /api/ 开头的 URL（前端统一处理）。"""

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


class SystemPromptBuilder:
    """静态 System Prompt 组装器：同一任务内所有 LLM 调用共享同一份 system 内容。"""

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
              skill_extra: Optional[str] = None, skill_index: Optional[str] = None) -> str:
        """构建任务级静态 System Prompt。

        agent_prompt：Agent 专属角色提示（如 Plan 的规划型人格）。注入位置在
        共享的「强制交付要求」之后、环境信息之前——两个 Agent 仍共享最前面的
        交付要求段；agent_prompt 为 None 时（build）输出与旧版逐字节一致，
        已有会话的 Prompt 缓存前缀不受影响。
        skill_extra：本任务显式/自动命中的技能内容（/skill 命令或 triggers
        匹配），追加到技能索引段之后。任务级注入——不进会话历史，任务内
        所有调用共享（缓存前缀稳定），任务结束即消失。
        """
        os_name = f"{platform.system()} {platform.release()} ({platform.machine()})"
        tools_summary = "\n".join(f"- **{t.name}**: {t.description}" for t in tools)
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
{tools_summary}

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
