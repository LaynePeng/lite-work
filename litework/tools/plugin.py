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
from .office import OfficeTools
from .registry import ToolRegistry
from .review import ReviewTools
from .shell import ShellTools
from .skills import SkillsTools
from .web import WebFetchTools
from .ocr import OCRTools

logger = logging.getLogger("litework.tools")

TOOLS_SERVICE = "tools"
TOOL_FILTER_SERVICE = "tool_filter"


class ToolPlugin(Plugin):
    """工具插件基类：install 时把 get_tools() 的工具注册进内核 tools 服务。

    - get_tools() 返回的工具：同名即覆盖内置/其他插件已有工具（更新能力）
    - removed_tools 声明要移除的工具名（内置或其他插件的），先删后注册
    - version: 插件版本号，空字符串表示跟随主应用版本
    """

    version: str = ""
    description: str = ""
    removed_tools: List[str] = []

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
    version = "1.0.0"
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
    version = "1.0.0"
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

    def __init__(self, workspace: str, timeout_seconds: float = 60.0) -> None:
        self._tools = ShellTools(workspace, timeout_seconds=timeout_seconds)

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

    def __init__(self, cache_dir: Optional[str] = None, cache_ttl: float = 3600) -> None:
        self._tools = WebFetchTools(cache_dir=cache_dir, cache_ttl=cache_ttl)

    def get_tools(self) -> List[ToolDefinition]:
        return self._tools.get_tools()

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        return await self._tools.execute(name, args)


class OcrPlugin(ToolPlugin):
    name = "ocr-plugin"
    version = "1.0.0"
    description = "OCR 识别：图片/PDF 页面/PPT 内嵌图片中的文字提取"

    def __init__(self, workspace: str) -> None:
        self._tools = OCRTools(workspace)

    def get_tools(self) -> List[ToolDefinition]:
        return self._tools.get_tools()

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        return await self._tools.execute(name, args)


class OfficePlugin(ToolPlugin):
    name = "office-plugin"
    # 独立功能版本（与社区仓库对齐比较，不借用应用版本号——否则应用升版会
    # 与社区版本撞车，隐藏「检查社区更新」的更新入口）
    version = "1.0.5"
    description = "办公生产力：Word/Excel/PPT/PDF 生成与读取、数据分析、图表"

    def __init__(self, workspace: str) -> None:
        self._tools = OfficeTools(workspace)

    def get_tools(self) -> List[ToolDefinition]:
        return self._tools.get_tools()

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        return await self._tools.execute(name, args)


class SubAgentPlugin(ToolPlugin):
    name = "sub-agent-plugin"
    version = "1.0.0"
    description = "子 Agent 编排：spawn_sub_agent 派生子任务"

    def __init__(self, app) -> None:
        from .sub_agent import make_sub_agent_handler

        self._app = app
        self._handler = make_sub_agent_handler(app)
        self._tool = ToolDefinition(
            name="spawn_sub_agent",
            description=(
                "派生一个独立且上下文隔离的子 Agent 执行耗时的调研/测试/重构子任务，"
                "返回汇总报告"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "taskDescription": {
                        "type": "string", "description": "指派给子 Agent 的具体任务指令",
                    },
                    "roleType": {
                        "type": "string",
                        "enum": ["explorer", "tester", "refactor", "general"],
                        "description": "子 Agent 角色：explorer(只读调研)/tester(测试执行)/refactor(完整重构)",
                    },
                },
                "required": ["taskDescription"],
            },
        )

    def get_tools(self) -> List[ToolDefinition]:
        return [self._tool]

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        return await self._handler(args)
