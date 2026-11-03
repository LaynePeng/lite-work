# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""Cordis 风格工具插件（课程第 9/10 课「空间解耦」落地）。

空间解耦：Kernel 只保留管道与服务容器，具体工具能力全部由插件提供。
每个工具插件 install() 时把自己的 ToolDefinition 注册进 kernel 的 "tools"
服务（ToolRegistry），并按 "tool_filter" 服务（Agent 工具裁剪策略）过滤；
其他插件可通过依赖注入（kernel.get_service("tools")）复用工具集。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..core.kernel import Kernel
from ..core.types import Plugin, ToolDefinition
from .ast_tools import ASTTools
from .codebase import CodebaseTools
from .editor import EditorTools
from .filesystem import FileSystemTools
from .git import GitTools
from .registry import ToolRegistry
from .review import ReviewTools
from .shell import BackgroundRegistry, ShellTools
from .skills import SkillsTools
from .web import WebFetchTools

# office / ocr 的**模块级**导入会连带拉起 cv2、onnxruntime、pandas、matplotlib
# 等重库（实测 +190MB 常驻、冷启动 +0.45s），而它们的工具定义只在请求
# /api/tools、/api/plugins 时才被读取——serve 启动路径并不需要。
# 故改为在 __init__ 内按需导入（实例化仍很轻，重成本全在模块导入）。
# 注：tools/office.py 与 tools/ocr.py 是与社区插件同源的副本（AGENTS.md §6），
# 不可在本仓库改动，因此把延迟导入放在本文件（主仓库原生）。

logger = logging.getLogger("litework.tools")

TOOLS_SERVICE = "tools"
TOOL_FILTER_SERVICE = "tool_filter"


def _runtime_workspace(bound: Optional[str]) -> str:
    """工具执行期的真实工作区（供内置 office/ocr 包装类使用）。

    **为什么不直接用装配时传入的 workspace**：构造参数只反映装配那一刻的
    `app.workspace`。桌面多端模式下 `POST /api/workspace` 只登记该端的
    client-view、不写全局值（见 server/routers/workspace.py），
    `AgentApp._builtin_plugins` 于是在全局为 None 时用 `~` 兜底
    （`ws = workspace_override or self.workspace or ~`）——面板等非任务路径
    构造出的插件就以家目录为工作区，项目内文件会被判「路径越界」。

    优先级（与社区 email-plugin 的附件解析口径一致）：

    1. `header_context["workspace"]`：当前任务钉死的工作区（worktree 隔离
       时即工作树路径，由 AgentLoop.run_task 每任务设置）；
    2. `bound`：装配时绑定值（单端旧语义）。

    只接受**已存在的绝对路径**；相对路径一律丢弃（AgentLoop 在无项目时会
    把 workspace 置为 "."，按 CWD 解析等于把进程目录当成工作区）。
    会话 metadata 一级不在此处实现：包装类拿不到 session_store，而任务路径
    已由第 1 级覆盖（TaskManager 传的 plugins_workspace 与其一致）。
    """
    import os as _os

    def ok(raw: Any) -> str:
        s = str(raw or "").strip()
        if not s or not _os.path.isabs(s):
            return ""
        try:
            p = _os.path.abspath(_os.path.expanduser(s))
        except Exception:
            return ""
        return p if _os.path.isdir(p) else ""

    try:
        from ..core.types import header_context

        ctx = header_context.get() or {}
        ws = ok(ctx.get("workspace")) if isinstance(ctx, dict) else ""
        if ws:
            return ws
    except Exception:  # 无 header_context（旧核心 / 单测）
        pass
    return ok(bound)


class ToolPlugin(Plugin):
    """工具插件基类：install 时把 get_tools() 的工具注册进内核 tools 服务。

    - get_tools() 返回的工具：同名即覆盖内置/其他插件已有工具（更新能力）
    - removed_tools 声明要移除的工具名（内置或其他插件的），先删后注册
    - read_only_tools 声明**纯只读**工具名：无人值守任务（盯梢/定时）因此
      能在 read 档下调用它们（见下）
    - version: 插件版本号，空字符串表示跟随主应用版本
    """

    version: str = ""
    description: str = ""
    removed_tools: List[str] = []

    #: **纯只读**工具名白名单（插件自述）。
    #:
    #: 为什么需要它：插件工具默认归 `misc` 域，而 `misc` 在无人值守档
    #: （read / read+web）下被强制 deny——因为注册时**无法判断**一个未知
    #: 插件工具会不会写盘/执行命令，宁可不给。结果是盯盘这类插件在盯梢任务里
    #: 拿不到任何自己的工具，Agent 只能回「数据工具缺失」。
    #:
    #: 插件作者主动声明「这些工具只查询、不改状态」后，它们归入 `read` 域，
    #: 无人值守 read 档即可使用。**不声明就仍然被禁**——安全默认不变。
    #:
    #: 声明要求（必须自己保证）：
    #:   1. 不发写请求、不写盘、不写数据库、不改计划/配置；
    #:   2. 不做跨会话/跨用户的状态变更；
    #:   3. 单个工具内有写分支的（如 action=set_rules）**必须整条不要声明**，
    #:      不能按 action 拆——域是工具级的。
    read_only_tools: List[str] = []

    def get_tools(self) -> List[ToolDefinition]:
        raise NotImplementedError

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        raise NotImplementedError

    def install(self, kernel: Kernel) -> None:
        registry: ToolRegistry = kernel.get_service(TOOLS_SERVICE)
        allow = (
            kernel.get_service(TOOL_FILTER_SERVICE)
            if kernel.has_service(TOOL_FILTER_SERVICE)
            else None
        )
        # 先移除声明删除的工具（用户显式要求，不受 Agent 裁剪策略影响）
        for name in self.removed_tools:
            if registry.has(name):
                registry.unregister(name)
                logger.info("[ToolPlugin %s] 已移除工具: %s", self.name, name)
        for tool in self.get_tools():
            if allow is not None and not allow(tool.name):
                logger.debug("[ToolPlugin %s] 工具 %s 被 Agent 策略裁剪", self.name, tool.name)
                continue
            registry.register(
                tool.name, tool.description, tool.parameters,
                lambda args, n=tool.name: self.execute(n, args),
            )


class FileSystemPlugin(ToolPlugin):
    name = "filesystem-plugin"
    # 1.1.0：write_file 新增 then 验证命令参数（动作融合）
    version = "1.1.0"
    description = "文件系统：读写文件、目录浏览、文件树"

    def __init__(self, workspace: str) -> None:
        self._tools = FileSystemTools(workspace)

    def get_tools(self) -> List[ToolDefinition]:
        return self._tools.get_tools()

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        return await self._tools.execute(name, args)


class CodebasePlugin(ToolPlugin):
    name = "codebase-plugin"
    version = "1.0.0"
    description = "代码搜索：grep 搜索、Ripgrep 全文检索"

    def __init__(self, workspace: str) -> None:
        self._tools = CodebaseTools(workspace)

    def get_tools(self) -> List[ToolDefinition]:
        return self._tools.get_tools()

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        return await self._tools.execute(name, args)


class ASTPlugin(ToolPlugin):
    name = "ast-plugin"
    version = "1.0.0"
    description = "AST 分析：文件大纲、符号聚焦"

    def __init__(self, workspace: str) -> None:
        self._tools = ASTTools(workspace)

    def get_tools(self) -> List[ToolDefinition]:
        return self._tools.get_tools()

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        return await self._tools.execute(name, args)


class EditorPlugin(ToolPlugin):
    name = "editor-plugin"
    # 1.1.0：apply_search_replace / apply_unified_diff 新增 then 验证命令参数（动作融合）
    version = "1.1.0"
    description = "代码编辑：Search-Replace、Unified Diff 精确编辑"

    def __init__(self, workspace: str) -> None:
        self._tools = EditorTools(workspace)

    def get_tools(self) -> List[ToolDefinition]:
        return self._tools.get_tools()

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        return await self._tools.execute(name, args)


class ShellPlugin(ToolPlugin):
    name = "shell-plugin"
    version = "1.0.0"
    description = "受限终端：执行命令"

    def __init__(self, workspace: str, timeout_seconds: float = 60.0,
                 registry: Optional[BackgroundRegistry] = None,
                 proxy_env: Optional[Dict[str, str]] = None) -> None:
        # registry：app 级共享的后台命令注册表（任务归属与工作区解耦，
        # 切换项目后旧 task_id 仍可查询/终止）；缺省自建（单实例场景）
        self._tools = ShellTools(workspace, timeout_seconds=timeout_seconds,
                                 registry=registry, proxy_env=proxy_env)

    def get_tools(self) -> List[ToolDefinition]:
        return self._tools.get_tools()

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        return await self._tools.execute(name, args)


class SkillsPlugin(ToolPlugin):
    name = "skills-plugin"
    version = "1.0.0"
    description = "技能加载：load_skill 按名称加载技能"

    def __init__(self, workspace: str) -> None:
        self._tools = SkillsTools(workspace)

    def get_tools(self) -> List[ToolDefinition]:
        return self._tools.get_tools()

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        return await self._tools.execute(name, args)


class GitPlugin(ToolPlugin):
    name = "git-plugin"
    version = "1.0.0"
    description = "Git 操作：status/diff/log/commit/branch"

    def __init__(self, workspace: str) -> None:
        self._tools = GitTools(workspace)

    def get_tools(self) -> List[ToolDefinition]:
        return self._tools.get_tools()

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        return await self._tools.execute(name, args)


class ReviewPlugin(ToolPlugin):
    name = "review-plugin"
    version = "1.0.0"
    description = "代码审查：review_code 审查代码变更"

    def __init__(self, workspace: str) -> None:
        self._tools = ReviewTools(workspace)

    def get_tools(self) -> List[ToolDefinition]:
        return self._tools.get_tools()

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        return await self._tools.execute(name, args)

class WebFetchPlugin(ToolPlugin):
    name = "webfetch-plugin"
    version = "1.0.0"
    description = "Web 抓取：webfetch/webfetch_batch 联网获取信息"

    def __init__(self, cache_dir: Optional[str] = None, cache_ttl: float = 3600,
                 proxy_mounts: Optional[Dict[str, Any]] = None,
                 ssl_verify: bool = True,
                 proxy_url: str = "") -> None:
        self._tools = WebFetchTools(cache_dir=cache_dir, cache_ttl=cache_ttl,
                                     proxy_mounts=proxy_mounts, ssl_verify=ssl_verify,
                                     proxy_url=proxy_url)

    def get_tools(self) -> List[ToolDefinition]:
        return self._tools.get_tools()

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        return await self._tools.execute(name, args)


class OcrPlugin(ToolPlugin):
    name = "ocr-plugin"
    # 与社区 ocr-plugin v1.2.1 同源：litework/tools/ocr.py 为社区副本
    # （MIT、无许可证头），版本以社区 manifest 为准；v1.2.0 重库惰性导入
    # 1.2.1：与社区 v1.2.1 同源——执行期重解析工作区（见 _runtime_workspace）
    version = "1.2.1"
    description = "OCR 识别：图片/PDF 页面/PPT 内嵌图片中的文字提取"

    def __init__(self, workspace: str) -> None:
        from .ocr import OCRTools  # 延迟导入：见文件头说明

        self._tools = OCRTools(workspace)
        # 装配时绑定的工作区（可能只是家目录兜底，见 _runtime_workspace）
        self._bound_workspace = workspace

    def get_tools(self) -> List[ToolDefinition]:
        return self._tools.get_tools()

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        ws = _runtime_workspace(self._bound_workspace)
        if ws and ws != self._bound_workspace:
            from .ocr import OCRTools  # 延迟导入：见文件头说明

            return await OCRTools(ws).execute(name, args)
        return await self._tools.execute(name, args)


class OfficePlugin(ToolPlugin):
    name = "office-plugin"
    # 独立功能版本（与社区仓库对齐比较，不借用应用版本号——否则应用升版会
    # 与社区版本撞车，隐藏「检查社区更新」的更新入口）。
    # 上游事实源是 lite-work-plugins 的 office-plugin（MIT）：版本以其为准，
    # 内置 litework/tools/office.py 是社区原样同步副本（无许可证头，保持 MIT）。
    # 1.3.0：与社区 v1.3.0 同源——产出物/素材工作区可见目录 + 格式化编辑
    #        （docx/xlsx/pptx_format）与查找替换（docx/xlsx_replace）
    # 1.4.0：与社区 v1.4.0 同源——pdf_create 中文字体嵌入/CJK 断行、Markdown
    #        表格渲染、内联格式、主题配色（theme/accent_color）、封面与页脚页码
    # 1.4.1：与社区 v1.4.1 同源——docx_read 兜底 p.style 为 None（未声明默认
    #        段落样式的文档不再 AttributeError 崩溃）
    # 1.5.0：与社区 v1.5.0 同源——LibreOffice 可选增强层（office_convert /
    #        office_render / xlsx_recalculate，soffice 缺失时优雅降级）
    # 1.6.0：与社区 v1.6.0 同源——输出目录与 v2 项目结构对齐：交付物直接写
    #        工作区根目录，中间产物写 中间产物/
    # 1.7.0：与社区 v1.7.0 同源——pandas/matplotlib/pypdf/pymupdf 惰性导入
    #        （首次调用对应工具时才加载，模块 import 保持轻量）
    # 1.7.1：与社区 v1.7.1 同源——执行期重解析工作区（见 _runtime_workspace），
    #        面板等非任务路径不再以家目录为工作区
    version = "1.7.1"
    description = "办公生产力：Word/Excel/PPT/PDF 生成与读取、格式化编辑与查找替换、数据分析、图表"

    def __init__(self, workspace: str) -> None:
        from .office import OfficeTools  # 延迟导入：见文件头说明

        self._tools = OfficeTools(workspace)
        # 装配时绑定的工作区（可能只是家目录兜底，见 _runtime_workspace）
        self._bound_workspace = workspace

    def get_tools(self) -> List[ToolDefinition]:
        return self._tools.get_tools()

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        ws = _runtime_workspace(self._bound_workspace)
        if ws and ws != self._bound_workspace:
            from .office import OfficeTools  # 延迟导入：见文件头说明

            return await OfficeTools(ws).execute(name, args)
        return await self._tools.execute(name, args)


class PptEditPlugin(ToolPlugin):
    """PPT 图形化编辑（ppt_edit）：roundtrip 工作区 + 元素级改动。

    与 office-plugin 分开：office.py 是社区 office-plugin 的同源副本，
    主仓库侧的 PPT 编辑能力不能夹带进去（AGENTS.md §6）。
    """

    name = "ppt-edit-plugin"
    version = "0.1.0"
    description = "PPT 图形化编辑：导入 pptx、按元素改内容、标注、回写导出"

    def __init__(self, workspace: str) -> None:
        from .ppt_edit import PptEditTools

        self._tools = PptEditTools(workspace)

    def get_tools(self) -> List[ToolDefinition]:
        return self._tools.get_tools()

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        return await self._tools.execute(name, args)


class PlanSavePlugin(ToolPlugin):
    """Plan Agent 专属的计划文件写入通道（plan_save）。"""

    name = "plan-save-plugin"
    version = "1.0.0"
    description = "规划产出落盘：plan_save 把计划写入 .lite-work/plans/"

    def __init__(self, app) -> None:
        from .plan_save import make_plan_save_handler

        self._app = app
        self._handler = make_plan_save_handler(app)

    def get_tools(self) -> List[ToolDefinition]:
        from .plan_save import get_plan_save_tool

        return [get_plan_save_tool()]

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        return await self._handler(args)
