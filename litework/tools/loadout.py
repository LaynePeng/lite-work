# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""工具渐进式披露：按需装载领域工具组（设计见 docs/deferred-tools-design.md）。

为什么需要：tool schema 常驻是全量工具集里最贵的固定开销——93 个工具约
28.2k tokens，其中 parameters 占 75%。这些工具里大部分是「特定任务才用」的
领域能力（office / email / stock / …），却每一轮都随请求发出。

做法（对齐 pi 的 prepareLoadout）：ToolRegistry 保持完整不变（执行、存在性、
压缩上下文的对齐都依赖它），只在**构成本次请求的 tools 列表**这一步裁剪。
按需组默认不发 schema，模型调 load_tools(group) 装载，装载后本任务内保持。

缓存前缀：装载是单调的（只增不减），一次任务通常装 1~2 次，不是每轮抖动。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..core.types import ToolDefinition


@dataclass
class ToolGroup:
    """一个按需装载的工具组。"""

    name: str
    summary: str
    tools: List[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.tools)


# 骨架组：任何任务都可能用到，**不参与按需装载**（永远常驻）。
# 判定标准：文件/编辑/命令/git/搜索/AST/联网/交互（todo/ask/技能/唤醒）/代码审查。
CORE_PLUGINS = frozenset({
    "filesystem-plugin", "editor-plugin", "shell-plugin", "codebase-plugin",
    "ast-plugin", "git-plugin", "review-plugin", "webfetch-plugin",
    "question-plugin", "todo-plugin", "skills-plugin", "wakeup-plugin",
    "path-permissions-plugin", "plan-save-plugin", "render-card-plugin",
})

# 组名 → 展示说明。组名由插件名推导（去掉 "-plugin" 后缀），这里只补说明。
# 未登记的组用默认说明（仍可装载，只是提示文案通用）。
GROUP_SUMMARIES: Dict[str, str] = {
    "office": "Word/Excel/PPT/PDF 文档生成与读取、数据分析、图表、格式修改",
    "email": "邮箱收发、读取、回复、转发、文件夹与标签管理",
    "astock": "A 股行情/K线/资金流/选股/回测/台账/舆情/事件",
    "watch": "盯盘计划与定时任务（规则、启停、自选股）",
    "multi-agent": "派生与管理子 Agent、共享任务池、Agent 间消息",
    "ppt-edit": "图形化编辑已有 pptx（改文字/表格/图表、导出）",
    "ocr": "图片与 PDF/PPTX 内的文字识别",
}

# 组名的展示别名（插件名推导出的组名不一定是最好叫的）。
# 只影响 UI/提示文案，`load_tools` 同时接受组名与插件名。
GROUP_ALIASES: Dict[str, str] = {"ppt-edit": "ppt"}

DEFAULT_GROUP_SUMMARY = "该组的领域工具"


def group_name_for_plugin(plugin_name: str) -> str:
    """插件名 → 组名（`office-plugin` → `office`）。"""
    name = (plugin_name or "").strip()
    if name.endswith("-plugin"):
        name = name[: -len("-plugin")]
    return name or "misc"


@dataclass
class ToolLoadout:
    """一次请求的「可装载视图」：决定哪些工具的 schema 发给模型。

    registry 保持完整；本类只回答「这次请求发哪些」。装载状态是本任务级的
    （由 AgentLoop 持有），单调递增。
    """

    # 组名 → 组。仅含**按需**组（骨架工具不在其中）
    groups: Dict[str, ToolGroup] = field(default_factory=dict)
    # 工具名 → 所属按需组名（反向索引：select 时判断某工具是否被延迟）
    tool_group: Dict[str, str] = field(default_factory=dict)
    # 已装载的组名
    loaded: set = field(default_factory=set)
    # 总开关：False 时全部常驻（等价旧行为，可一键退回）
    enabled: bool = True

    # ---------------------------------------------------------------- 构建

    @classmethod
    def from_plugin_tools(
        cls,
        plugin_tools: Dict[str, List[str]],
        enabled: bool = True,
    ) -> "ToolLoadout":
        """按「插件名 → 工具名列表」构建。

        骨架插件（CORE_PLUGINS）的工具不进 groups，因此永远常驻。
        """
        groups: Dict[str, ToolGroup] = {}
        tool_group: Dict[str, str] = {}
        for plugin_name, tool_names in plugin_tools.items():
            if plugin_name in CORE_PLUGINS or not tool_names:
                continue
            gname = group_name_for_plugin(plugin_name)
            group = groups.setdefault(
                gname, ToolGroup(name=gname, summary=GROUP_SUMMARIES.get(gname, DEFAULT_GROUP_SUMMARY)),
            )
            for tname in tool_names:
                if tname in tool_group:      # 同名工具后者覆盖（与注册语义一致）
                    group.tools = [t for t in group.tools if t != tname]
                group.tools.append(tname)
                tool_group[tname] = gname
        # 组内按名字排序，保证 System Prompt 与 select 输出稳定（缓存友好）
        for group in groups.values():
            group.tools.sort()
        return cls(groups=groups, tool_group=tool_group, enabled=enabled)

    # ---------------------------------------------------------------- 查询

    def is_deferred(self, tool_name: str) -> bool:
        """该工具此刻是否**不**发 schema（属于未装载的按需组）。"""
        if not self.enabled:
            return False
        gname = self.tool_group.get(tool_name)
        return gname is not None and gname not in self.loaded

    def group_of(self, tool_name: str) -> Optional[str]:
        return self.tool_group.get(tool_name)

    def pending_groups(self) -> List[ToolGroup]:
        """尚未装载的组（System Prompt 的「按需工具组」段用）。"""
        if not self.enabled:
            return []
        return [g for name, g in sorted(self.groups.items()) if name not in self.loaded]

    def all_groups(self) -> List[ToolGroup]:
        return [g for _, g in sorted(self.groups.items())]

    # ---------------------------------------------------------------- 选择

    def select(self, tools: List[ToolDefinition]) -> List[ToolDefinition]:
        """从完整工具集里挑出本次请求要发的那些（保持原顺序，缓存前缀稳定）。"""
        if not self.enabled:
            return list(tools)
        return [t for t in tools if not self.is_deferred(t.name)]

    # ---------------------------------------------------------------- 装载

    def load(self, group: str) -> Optional[ToolGroup]:
        """装载一个组；返回该组（组名不存在返回 None）。已装载则幂等返回。

        接受三种写法：组名（astock）、插件名（astock-plugin）、展示别名（ppt）。
        """
        raw = (group or "").strip().lower()
        target = raw
        # 别名 → 真组名（ppt → ppt-edit）
        for real, alias in GROUP_ALIASES.items():
            if raw == alias:
                target = real
                break
        found = self.groups.get(target)
        if found is None:
            # 容错：允许传插件名（office-plugin）
            found = self.groups.get(group_name_for_plugin(raw))
        if found is None:
            return None
        self.loaded.add(found.name)
        return found

    def unavailable_hint(self, tool_name: str) -> Optional[str]:
        """模型调用了未装载的工具时给的自解释提示（而不是「未注册」）。"""
        gname = self.tool_group.get(tool_name)
        if gname is None or gname in self.loaded or not self.enabled:
            return None
        return (
            f"[Tool Not Loaded]: 工具 {tool_name} 属于按需工具组 `{gname}`，"
            f"当前未装载。请先调用 load_tools(group=\"{gname}\") 装载该组，再重试本次调用。"
        )
