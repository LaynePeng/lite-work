"""办公/生产力工具集：文档、表格、演示、图表、数据分析。

将 lite-work 从代码 Agent 扩展到通用办公场景，让 Agent 能直接产出
docx/xlsx/pptx/pdf 等办公文件，以及进行数据分析和生成图表。
v1.2.0 新增：已有文件的格式化编辑（字体/粗体/斜体/颜色/高亮/对齐/行距等）、
查找替换、docx 生成时基础字体字号控制。

所有依赖包已包含在主依赖中（pyproject.toml dependencies），
`pip install -e .` 时自动安装。
"""
# 同步自 lite-work 主仓库 litework/tools/office.py（社区独立分发版）
# v1.2.0+ 含社区版独有功能（格式化/查找替换），主仓库同步时需保留
from __future__ import annotations

import io
import json
import logging
import os
import re
import sys
import tempfile
from typing import Any, Dict, List, Optional

from litework.core.types import ToolDefinition

logger = logging.getLogger("litework.tools.office")

# ---------------------------------------------------------------- 依赖检查

_HAS_DOCX: bool = False
_HAS_OPENPYXL: bool = False
_HAS_PPTX: bool = False
_HAS_PANDAS: bool = False
_HAS_MATPLOTLIB: bool = False

try:
    from docx import Document as _DocxDoc
    from docx.shared import Inches, Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    _HAS_DOCX = True
except ImportError:
    _DocxDoc = None  # type: ignore

try:
    import openpyxl as _openpyxl

    _HAS_OPENPYXL = True
except ImportError:
    _openpyxl = None  # type: ignore

try:
    from pptx import Presentation as _PptxPresentation
    from pptx.util import Inches as _PptxInches

    _HAS_PPTX = True
except ImportError:
    _PptxPresentation = None  # type: ignore

try:
    import pandas as _pd

    _HAS_PANDAS = True
except ImportError:
    _pd = None  # type: ignore

try:
    import matplotlib
    matplotlib.use("Agg")  # 非交互后端，服务器安全
    import matplotlib.pyplot as _plt

    _HAS_MATPLOTLIB = True
except ImportError:
    _plt = None  # type: ignore

_HAS_PYPDF: bool = False
try:
    import pypdf as _pypdf
    _HAS_PYPDF = True
except ImportError:
    _pypdf = None  # type: ignore


def _missing_dep_msg(pkg: str, tools: str) -> str:
    return (
        f"[Office Tools] 需要安装 {pkg} 才能使用 {tools} 工具。\n"
        f"请运行: pip install lite-work[office]  或   pip install {pkg}"
    )


# ---------------------------------------------------------------- 工具函数


# 产出物/素材目录名（1.3.0 起为工作区内**可见**目录——交付物要在
# Finder/IDE 里直接可见，不再藏进 .outputs 隐藏目录；历史 .outputs/.uploads
# 保留原地不动，不迁移）。
OUTPUT_DIR_NAME = "产出物"
UPLOADS_DIR_NAME = "素材"


def _ensure_output_dir(workspace: str, subdir=None):
    """确保输出目录存在，返回绝对路径。"""
    if subdir is None:
        subdir = OUTPUT_DIR_NAME
    elif not subdir.startswith(OUTPUT_DIR_NAME):
        subdir = os.path.join(OUTPUT_DIR_NAME, subdir)
    out_dir = os.path.join(os.path.abspath(workspace), subdir)
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def _safe_filename(name: str) -> str:
    """清理文件名，移除不安全字符。"""
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name.strip() or "output"


# ---------------------------------------------------------------- OfficeTools


class OfficeTools:
    def __init__(self, workspace: Optional[str]) -> None:
        # 桌面应用启动时可能未打开项目（workspace=None），此时回落到用户目录，
        # 真正的产出目录在每次任务时以当前 workspace 为准重建
        self.workspace = os.path.abspath(workspace) if workspace else os.path.expanduser("~")
        self._cjk_font_applied = False
        # 渲染期基础字体上下文（docx_create/append 的 base_font/base_size）
        self._ctx_font: Optional[str] = None
        self._ctx_size: Optional[float] = None

    # ------------------------------------------------------------ 工具定义

    def get_tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="docx_create",
                description=(
                    "根据 Markdown 内容生成 Word (.docx) 文档，支持标题、段落、"
                    "列表、表格、粗体/斜体/下划线(<u>文字</u>)/删除线(~~文字~~)"
                    "与文字颜色/字体/字号（<span style=\"color:red;font-family:宋体;"
                    "font-size:16\">文字</span>，颜色支持命名色/#RGB/#RRGGBB/rgb()），"
                    "以及图片嵌入（Markdown 图片语法 ![图注](图片路径)）。"
                    "可选 base_font/base_size 设置全文基础字体字号。返回文件路径。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "Markdown 格式的文档正文，支持图片嵌入语法 ![图注](图片路径)、内联格式 <span style='color:red;font-family:楷体;font-size:16'>文字</span> 等",
                        },
                        "filename": {
                            "type": "string",
                            "description": "输出文件名（不含路径，默认 '文档.docx'）",
                        },
                        "title": {
                            "type": "string",
                            "description": "文档标题（文档第一行大标题，可选）",
                        },
                        "base_font": {
                            "type": "string",
                            "description": "全文基础字体（如 'Microsoft YaHei'、'宋体'），西文与中文字体同时设置，含标题",
                        },
                        "base_size": {
                            "type": "number",
                            "description": "全文正文字号（pt，如 12），作用于正文/列表/表格，不影响标题与代码",
                        },
                    },
                    "required": ["content"],
                },
            ),
            ToolDefinition(
                name="docx_append",
                description=(
                    "向已有的 Word (.docx) 文档追加内容（Markdown 格式，支持标题/段落/"
                    "列表/表格/图片/粗体/斜体/下划线/删除线/颜色字体字号），保留原有"
                    "内容与样式。可选 base_font/base_size 设置追加内容的基础字体字号。"
                    "用于迭代式写作：在生成的文档上补充章节、追加内容。返回文件路径。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "已有 docx 文件路径（相对工作区，如 产出物/方案.docx）",
                        },
                        "content": {
                            "type": "string",
                            "description": "要追加的 Markdown 内容（支持图片语法 ![图注](路径)）",
                        },
                        "page_break": {
                            "type": "boolean",
                            "description": "追加前是否先插入分页符（默认 false）",
                        },
                        "base_font": {
                            "type": "string",
                            "description": "追加内容的基础字体（如 'Microsoft YaHei'），含已有内容的标题",
                        },
                        "base_size": {
                            "type": "number",
                            "description": "追加内容的正文字号 pt，如 12",
                        },
                    },
                    "required": ["path", "content"],
                },
            ),
            # -------------------------------------------------------- v1.2.0 格式编辑 / 查找替换
            ToolDefinition(
                name="docx_format",
                description=(
                    "修改已有 Word (.docx) 文档的文字与段落格式：字体（自动覆盖中文 "
                    "eastAsia 字体）、字号、粗体、斜体、下划线、删除线、文字颜色、"
                    "高亮背景、水平对齐、行距。定位方式 target：all=全文档（默认）；"
                    "heading=指定层级标题（配合 heading_level 1-6）；search=按文字"
                    "搜索（配合 search_text，正文与表格内均搜索，优先只修改命中文字"
                    "所在 run）。只传需要修改的属性，未传的保持不变。返回修改统计。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "docx 文件路径（相对工作区，如 产出物/方案.docx）"},
                        "target": {"type": "string", "enum": ["all", "heading", "search"], "description": "格式化目标（默认 all）"},
                        "search_text": {"type": "string", "description": "target=search 时必填：要命中的文字"},
                        "heading_level": {"type": "number", "description": "target=heading 时必填：标题层级 1-6"},
                        "font": {"type": "string", "description": "字体名，如 'Microsoft YaHei'、'宋体'、'Times New Roman'"},
                        "size": {"type": "number", "description": "字号（pt）"},
                        "bold": {"type": "boolean", "description": "加粗（true 设置 / false 取消）"},
                        "italic": {"type": "boolean", "description": "斜体（true 设置 / false 取消）"},
                        "underline": {"type": "boolean", "description": "下划线（true 设置 / false 取消）"},
                        "strike": {"type": "boolean", "description": "删除线（true 设置 / false 取消）"},
                        "color": {"type": "string", "description": "文字颜色：命名色（red/blue...）/#RGB/#RRGGBB/rgb()"},
                        "highlight": {"type": "string", "description": "高亮背景色：yellow/green/cyan/teal/pink/red/blue/purple/gray/black/white/none（清除）"},
                        "alignment": {"type": "string", "enum": ["left", "center", "right", "justify"], "description": "段落对齐"},
                        "line_spacing": {"type": "number", "description": "行距倍数，如 1.5"},
                    },
                    "required": ["path"],
                },
            ),
            ToolDefinition(
                name="docx_replace",
                description=(
                    "在已有 Word (.docx) 文档中查找并替换文字（保留原格式）："
                    "作用于正文与表格；跨 run 的匹配自动合并后替换。"
                    "可选 max_count 限制替换次数。返回替换统计。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "docx 文件路径（相对工作区）"},
                        "find": {"type": "string", "description": "要查找的文字"},
                        "replace": {"type": "string", "description": "替换为的文字（可为空串=删除）"},
                        "max_count": {"type": "number", "description": "最大替换次数（可选，默认全部）"},
                    },
                    "required": ["path", "find", "replace"],
                },
            ),
            ToolDefinition(
                name="xlsx_create",
                description=(
                    "根据结构化数据生成 Excel (.xlsx) 表格，支持多 sheet。"
                    "返回文件路径。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "data": {
                            "type": "string",
                            "description": "JSON 格式数据。可以是："
                            "1) 对象数组 [{'列名': 值, ...}] 自动生成表头；"
                            "2) 嵌套对象 {\"sheet1\": [...], \"sheet2\": [...]} 多 sheet。",
                        },
                        "filename": {
                            "type": "string",
                            "description": "输出文件名（不含路径，默认 '表格.xlsx'）",
                        },
                    },
                    "required": ["data"],
                },
            ),
            ToolDefinition(
                name="xlsx_format",
                description=(
                    "修改已有 Excel (.xlsx) 单元格样式：粗体/斜体/下划线、字体、字号、"
                    "字色、背景填充色、边框、水平对齐、自动换行、数字格式，以及"
                    "auto_fit 按内容自适应列宽、freeze 冻结窗格（如 A2 冻结首行）。"
                    "range 支持 A1:C10 / A:C（整列）/ 1:5（整行）/ all（默认，已用区域）。"
                    "注意：复杂图表/透视表文件经 openpyxl 重存可能丢失部分元素。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "xlsx 文件路径（相对工作区）"},
                        "sheet": {"type": "string", "description": "sheet 名（默认活动 sheet）"},
                        "range": {"type": "string", "description": "目标区域：A1:C10 / A:C / 1:5 / all（默认）"},
                        "bold": {"type": "boolean", "description": "加粗（true 设置 / false 取消）"},
                        "italic": {"type": "boolean", "description": "斜体（true 设置 / false 取消）"},
                        "underline": {"type": "boolean", "description": "下划线（true 设置 / false 取消）"},
                        "font": {"type": "string", "description": "字体名，如 'Microsoft YaHei'、'宋体'"},
                        "size": {"type": "number", "description": "字号（pt）"},
                        "color": {"type": "string", "description": "文字颜色（命名色/#RRGGBB/rgb()）"},
                        "fill": {"type": "string", "description": "背景填充色（命名色/#RRGGBB/rgb()）"},
                        "border": {"type": ["boolean", "string"], "description": "true=细边框，或 thin/medium/thick/dashed/dotted/double/hair/none（清除）"},
                        "alignment": {"type": "string", "enum": ["left", "center", "right"], "description": "水平对齐"},
                        "wrap_text": {"type": "boolean", "description": "自动换行"},
                        "number_format": {"type": "string", "description": "数字格式，如 '0.00'、'yyyy-mm-dd'、'0.00%'"},
                        "auto_fit": {"type": "boolean", "description": "按内容自适应列宽"},
                        "freeze": {"type": "string", "description": "冻结窗格锚点，如 'A2'（冻结首行）、'B1'（冻结 A 列）；'none' 解除"},
                    },
                    "required": ["path"],
                },
            ),
            ToolDefinition(
                name="xlsx_replace",
                description=(
                    "在已有 Excel (.xlsx) 中查找并替换文字（含公式文本）。"
                    "默认全部 sheet 的已用单元格；可选 sheet、range、match_case。"
                    "注意：复杂图表/透视表文件经 openpyxl 重存可能丢失部分元素。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "xlsx 文件路径（相对工作区）"},
                        "find": {"type": "string", "description": "要查找的文字"},
                        "replace": {"type": "string", "description": "替换为的文字（可为空串=删除）"},
                        "sheet": {"type": "string", "description": "限定 sheet 名（可选，默认全部）"},
                        "range": {"type": "string", "description": "限定区域（可选，默认全部已用区域）"},
                        "match_case": {"type": "boolean", "description": "区分大小写（默认 true）"},
                    },
                    "required": ["path", "find", "replace"],
                },
            ),
            ToolDefinition(
                name="pptx_create",
                description=(
                    "根据结构化内容生成 PowerPoint (.pptx) 演示文稿，支持标题幻灯片"
                    "和正文幻灯片；每页 slide 支持 image 字段（图片路径）嵌入图片。"
                    "返回文件路径。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "slides": {
                            "type": "string",
                            "description": "JSON 数组，每项为 {title: 幻灯片标题, "
                            "content: Markdown 正文, bullets: [要点列表]（可选）, "
                            "image: 图片路径（可选，嵌入到该页）}",
                        },
                        "filename": {
                            "type": "string",
                            "description": "输出文件名（不含路径，默认 '演示文稿.pptx'）",
                        },
                        "title": {
                            "type": "string",
                            "description": "封面标题（可选）",
                        },
                    },
                    "required": ["slides"],
                },
            ),
            ToolDefinition(
                name="pptx_format",
                description=(
                    "修改已有 PowerPoint (.pptx) 的文字格式：粗体/斜体/下划线、字体"
                    "（含中文 eastAsia）、字号、颜色。slide=页码（1 起）或 all（默认）；"
                    "target=title（仅标题）/ body（仅正文）/ all（默认）。"
                    "只传需要修改的属性。返回修改统计。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "pptx 文件路径（相对工作区）"},
                        "slide": {"type": ["number", "string"], "description": "目标页码（1 起）或 'all'（默认）"},
                        "target": {"type": "string", "enum": ["title", "body", "all"], "description": "格式化对象（默认 all）"},
                        "bold": {"type": "boolean", "description": "加粗（true 设置 / false 取消）"},
                        "italic": {"type": "boolean", "description": "斜体（true 设置 / false 取消）"},
                        "underline": {"type": "boolean", "description": "下划线（true 设置 / false 取消）"},
                        "font": {"type": "string", "description": "字体名，如 'Microsoft YaHei'、'宋体'"},
                        "size": {"type": "number", "description": "字号（pt）"},
                        "color": {"type": "string", "description": "文字颜色（命名色/#RRGGBB/rgb()）"},
                    },
                    "required": ["path"],
                },
            ),
            ToolDefinition(
                name="pdf_create",
                description=(
                    "将 Markdown 内容生成为 PDF 文件。依赖 reportlab 库。"
                    "返回文件路径。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "Markdown 格式内容",
                        },
                        "filename": {
                            "type": "string",
                            "description": "输出文件名（不含路径，默认 '文档.pdf'）",
                        },
                        "title": {
                            "type": "string",
                            "description": "文档标题（可选）",
                        },
                    },
                    "required": ["content"],
                },
            ),
            ToolDefinition(
                name="data_analyze",
                description=(
                    "对 CSV/JSON 数据或数据文件进行统计分析，返回分析结果文字。"
                    "支持数据概览、统计描述、分组聚合、排序等。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "数据文件路径（相对工作区），支持 .xlsx/.xls/.csv/.json；"
                                           "用户上传的文件在 素材/ 下。与 data 二选一，path 优先",
                        },
                        "data": {
                            "type": "string",
                            "description": "CSV 格式文本（第一行为表头）或 JSON 数组字符串。与 path 二选一",
                        },
                        "instructions": {
                            "type": "string",
                            "description": "分析指令，如 '统计各分组平均值'、'按日期排序'、'描述性统计'",
                        },
                        "sheet": {
                            "type": "string",
                            "description": "（xlsx 可选）要读取的 sheet 名，默认第一个 sheet",
                        },
                        "output_format": {
                            "type": "string",
                            "enum": ["text", "json", "xlsx"],
                            "description": "输出格式：text（默认，返回文字分析）、json（返回 JSON 串）、xlsx（生成 Excel 文件）",
                        },
                    },
                    "required": ["instructions"],
                },
            ),
            ToolDefinition(
                name="chart_make",
                description=(
                    "根据数据生成图表并保存为图片（PNG），返回图片路径。"
                    "支持柱状图、折线图、饼图、散点图、横向柱状图。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "data": {
                            "type": "string",
                            "description": "JSON 格式数据，格式为："
                            "{\"labels\": [\"A\", \"B\", ...], \"values\": [10, 20, ...]}"
                            "或 {\"datasets\": [{\"label\": \"系列1\", \"values\": [...]}, ...]}",
                        },
                        "chart_type": {
                            "type": "string",
                            "enum": ["bar", "line", "pie", "scatter", "barh"],
                            "description": "图表类型：bar(柱状图)、line(折线图)、pie(饼图)、scatter(散点图)、barh(横向柱状图)",
                        },
                        "title": {
                            "type": "string",
                            "description": "图表标题（可选）",
                        },
                        "x_label": {
                            "type": "string",
                            "description": "X 轴标签（可选）",
                        },
                        "y_label": {
                            "type": "string",
                            "description": "Y 轴标签（可选）",
                        },
                        "filename": {
                            "type": "string",
                            "description": "输出文件名（不含路径，默认 'chart.png'）",
                        },
                    },
                    "required": ["data", "chart_type"],
                },
            ),
            # -------------------------------------------------------- 读取已有办公文件
            ToolDefinition(
                name="docx_read",
                description=(
                    "读取 Word (.docx) 文件内容，返回纯文本（段落、标题、表格）。"
                    "用于调研已有文档、提取参考资料。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "docx 文件路径（相对工作区，如 产出物/方案.docx 或 素材/素材.docx）",
                        },
                    },
                    "required": ["path"],
                },
            ),
            ToolDefinition(
                name="xlsx_read",
                description=(
                    "读取 Excel (.xlsx) 文件内容，返回各 sheet 的表头和数据行。"
                    "用于浏览已有表格、查看调研数据。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "xlsx 文件路径（相对工作区）",
                        },
                        "sheet": {
                            "type": "string",
                            "description": "要读取的 sheet 名（可选，默认第一个 sheet）",
                        },
                        "max_rows": {
                            "type": "number",
                            "description": "最多读取行数（含表头，默认 100）",
                        },
                    },
                    "required": ["path"],
                },
            ),
            ToolDefinition(
                name="pptx_read",
                description=(
                    "读取 PowerPoint (.pptx) 演示文稿内容，返回每页幻灯片的标题和要点。"
                    "用于查看已有演示文稿。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "pptx 文件路径（相对工作区）",
                        },
                    },
                    "required": ["path"],
                },
            ),
            ToolDefinition(
                name="pdf_read",
                description=(
                    "读取 PDF 文件内容，返回每页的文本。"
                    "用于查阅调研报告、参考资料等 PDF 文档。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "pdf 文件路径（相对工作区）",
                        },
                        "max_pages": {
                            "type": "number",
                            "description": "最多读取页数（默认 50）",
                        },
                    },
                    "required": ["path"],
                },
            ),
        ]

    # ------------------------------------------------------------ 执行入口

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        """工具执行入口：全部实现为同步重活（subprocess 渲染 / matplotlib /
        python-docx CPU），必须放线程池执行——直接同步调用会阻塞 asyncio
        事件循环，导致 SSE / 审批 / 其他任务全部冻结（表现为应用"卡死"）。
        """
        import asyncio as _asyncio

        handlers = {
            "docx_create": self._docx_create,
            "docx_append": self._docx_append,
            "docx_format": self._docx_format,
            "docx_replace": self._docx_replace,
            "xlsx_create": self._xlsx_create,
            "xlsx_format": self._xlsx_format,
            "xlsx_replace": self._xlsx_replace,
            "pptx_create": self._pptx_create,
            "pptx_format": self._pptx_format,
            "pdf_create": self._pdf_create,
            "data_analyze": self._data_analyze,
            "chart_make": self._chart_make,
            "docx_read": self._docx_read,
            "xlsx_read": self._xlsx_read,
            "pptx_read": self._pptx_read,
            "pdf_read": self._pdf_read,
        }
        handler = handlers.get(name)
        if handler is None:
            raise ValueError(f"Unknown Office Tool: {name}")
        return await _asyncio.to_thread(handler, args)

    # ------------------------------------------------------------ docx

    def _docx_create(self, args: Dict[str, Any]) -> str:
        if not _HAS_DOCX:
            return _missing_dep_msg("python-docx", "docx_create")

        content = args.get("content", "")
        filename = _safe_filename(args.get("filename", "文档.docx"))
        title = args.get("title", "")
        base_font = str(args.get("base_font") or "").strip() or None
        base_size = self._coerce_pt(args.get("base_size"), "base_size")
        if isinstance(base_size, str):
            return base_size

        if not filename.lower().endswith(".docx"):
            filename += ".docx"

        doc = _DocxDoc()

        # 标题
        if title:
            heading = doc.add_heading(title, level=0)
            heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
            if base_font:
                for r in heading.runs:
                    self._apply_run_font(r, base_font)

        # Markdown 转 docx 的简化渲染
        self._ctx_font, self._ctx_size = base_font, base_size
        try:
            self._md_to_docx(doc, content)
        finally:
            self._ctx_font, self._ctx_size = None, None

        out_dir = _ensure_output_dir(self.workspace)
        filepath = os.path.join(out_dir, filename)
        doc.save(filepath)

        return f"[Office OK]: 已生成 Word 文档 → {filepath}"

    def _docx_append(self, args: Dict[str, Any]) -> str:
        """向已有 docx 追加内容（迭代式写作）：打开→可选分页→追加 Markdown→保存。"""
        if not _HAS_DOCX:
            return _missing_dep_msg("python-docx", "docx_append")

        rel_path = str(args.get("path", "") or "").strip()
        content = args.get("content", "")
        page_break = bool(args.get("page_break", False))
        base_font = str(args.get("base_font") or "").strip() or None
        base_size = self._coerce_pt(args.get("base_size"), "base_size")
        if isinstance(base_size, str):
            return base_size

        if not rel_path:
            return "[Office Error]: 缺少 path 参数（要追加的 docx 路径）"
        resolved = os.path.abspath(
            rel_path if os.path.isabs(rel_path) else os.path.join(self.workspace, rel_path)
        )
        if not (resolved == self.workspace or resolved.startswith(self.workspace + os.sep)):
            return "[Office Error]: 路径越界：仅支持工作区内的文件"
        if not resolved.lower().endswith(".docx"):
            return "[Office Error]: 仅支持 .docx 文件"
        if not os.path.isfile(resolved):
            return f"[Office Error]: 文件不存在: {rel_path}（先用 docx_create 生成）"
        if not content.strip():
            return "[Office Error]: 追加内容为空"

        try:
            doc = _DocxDoc(resolved)
        except Exception as exc:
            return f"[Office Error]: 打开文档失败: {exc}"

        # 可选分页符：新章节从新页开始
        if page_break:
            from docx.enum.text import WD_BREAK
            p = doc.add_paragraph()
            p.add_run().add_break(WD_BREAK.PAGE)

        # 复用 Markdown 渲染（支持标题/段落/列表/表格/图片语法）
        self._ctx_font, self._ctx_size = base_font, base_size
        try:
            self._md_to_docx(doc, content)
        finally:
            self._ctx_font, self._ctx_size = None, None

        try:
            doc.save(resolved)
        except Exception as exc:
            return f"[Office Error]: 保存文档失败: {exc}"

        return f"[Office OK]: 已追加内容 → {resolved}"

    def _md_to_docx(self, doc, md_text: str) -> None:
        """将 Markdown 文本渲染到 python-docx Document 对象。"""
        lines = md_text.split("\n")
        i = 0
        in_table = False
        table_data: List[List[str]] = []
        table_cols = 0

        while i < len(lines):
            line = lines[i]

            # 表格行（管道语法 | col1 | col2 |）
            if line.strip().startswith("|") and line.strip().endswith("|"):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if not in_table:
                    table_data = [cells]
                    table_cols = len(cells)
                    in_table = True
                    # 检查下一行是否为分隔行（|---|---|）
                    if i + 1 < len(lines) and re.match(r"^\|[\s\-:]+\|", lines[i + 1].strip()):
                        i += 1  # 跳过分隔行
                else:
                    if len(cells) <= table_cols:
                        table_data.append(cells)
                i += 1
                continue
            else:
                if in_table and table_data:
                    # 渲染表格
                    if len(table_data) >= 2:
                        table = doc.add_table(rows=len(table_data), cols=table_cols)
                        table.style = "Table Grid"
                        for r_idx, row_data in enumerate(table_data):
                            for c_idx, cell_text in enumerate(row_data):
                                if c_idx < table_cols:
                                    cell = table.rows[r_idx].cells[c_idx]
                                    # 单元格内容同样支持内联格式（加粗/斜体/颜色），表头整体加粗
                                    cell.text = ""
                                    self._add_styled_run(
                                        cell.paragraphs[0], cell_text, bold=(r_idx == 0)
                                    )
                    doc.add_paragraph()  # 表后空行
                    table_data = []
                    in_table = False
                    continue

            # 空行
            if not line.strip():
                i += 1
                continue

            # 标题
            heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)
            if heading_match:
                level = len(heading_match.group(1))
                text = heading_match.group(2).strip()
                # 标题也解析内联格式（加粗/斜体/颜色），避免 ** 等原样输出
                heading = doc.add_heading("", level=level)
                self._add_styled_run(heading, text, is_heading=True)
                i += 1
                continue

            # 无序列表
            if re.match(r"^[\s]*[-*+]\s+", line):
                text = re.sub(r"^[\s]*[-*+]\s+", "", line)
                p = doc.add_paragraph(style="List Bullet")
                self._add_styled_run(p, text)
                i += 1
                continue

            # 有序列表
            if re.match(r"^\s*\d+[\.\)]\s+", line):
                text = re.sub(r"^\s*\d+[\.\)]\s+", "", line)
                p = doc.add_paragraph(style="List Number")
                self._add_styled_run(p, text)
                i += 1
                continue

            # 代码块
            cb_match = re.match(r"^```[ \t]*([A-Za-z0-9_+-]*)[ \t]*$", line.strip())
            if cb_match:
                lang = (cb_match.group(1) or "").lower()
                code_lines = []
                i += 1
                while i < len(lines) and not lines[i].strip().startswith("```"):
                    code_lines.append(lines[i])
                    i += 1
                code_text = "\n".join(code_lines)
                i += 1  # 跳过闭合 ```
                # 图表代码块 → 自动渲染成图片嵌入（工具层兜底，保证图不丢）；
                # 渲染失败回退源码文本
                if lang in ("plantuml", "puml", "mermaid", "mmd") and code_text.strip():
                    img_abs = self._render_diagram_block(lang, code_text, i)
                    if img_abs:
                        rel = os.path.relpath(img_abs, self.workspace).replace("\\", "/")
                        self._add_docx_image(doc, rel, f"图表（{lang}）")
                        continue
                p = doc.add_paragraph()
                run = p.add_run(code_text)
                run.font.name = "Courier New"
                run.font.size = Pt(9)
                p.paragraph_format.left_indent = Inches(0.3)
                continue

            # 图片 ![alt](path)
            img_match = re.match(r"^\s*!\[([^\]]*)\]\(([^)]+)\)\s*$", line)
            if img_match:
                alt = img_match.group(1).strip()
                img_path = img_match.group(2).strip().strip("\"'")
                self._add_docx_image(doc, img_path, alt)
                i += 1
                continue

            # 普通段落（支持内联格式）
            p = doc.add_paragraph()
            self._add_styled_run(p, line)
            i += 1

        # 结尾处若还有未渲染的表格
        if in_table and table_data and len(table_data) >= 2:
            table = doc.add_table(rows=len(table_data), cols=table_cols)
            table.style = "Table Grid"
            for r_idx, row_data in enumerate(table_data):
                for c_idx, cell_text in enumerate(row_data):
                    if c_idx < table_cols:
                        cell = table.rows[r_idx].cells[c_idx]
                        cell.text = ""
                        self._add_styled_run(
                            cell.paragraphs[0], cell_text, bold=(r_idx == 0)
                        )

    # 常用命名颜色（CSS 兼容子集，便于直接书写）
    _NAMED_COLORS = {
        "black": (0x00, 0x00, 0x00),
        "white": (0xFF, 0xFF, 0xFF),
        "red": (0xFF, 0x00, 0x00),
        "green": (0x00, 0x80, 0x00),
        "blue": (0x00, 0x00, 0xFF),
        "yellow": (0xFF, 0xFF, 0x00),
        "orange": (0xFF, 0xA5, 0x00),
        "purple": (0x80, 0x00, 0x80),
        "gray": (0x80, 0x80, 0x80),
        "grey": (0x80, 0x80, 0x80),
        "cyan": (0x00, 0xFF, 0xFF),
        "magenta": (0xFF, 0x00, 0xFF),
        "pink": (0xFF, 0xC0, 0xCB),
        "brown": (0xA5, 0x2A, 0x2A),
        "navy": (0x00, 0x00, 0x80),
        "darkred": (0x8B, 0x00, 0x00),
        "darkgreen": (0x00, 0x64, 0x00),
        "darkblue": (0x00, 0x00, 0x8B),
        "lightgray": (0xD3, 0xD3, 0xD3),
        "silver": (0xC0, 0xC0, 0xC0),
        "gold": (0xFF, 0xD7, 0x00),
        "teal": (0x00, 0x80, 0x80),
        "maroon": (0x80, 0x00, 0x00),
        "olive": (0x80, 0x80, 0x00),
        "lime": (0x00, 0xFF, 0x00),
    }

    def _parse_color(self, color_str: str) -> Optional[Any]:
        """解析颜色：命名色 / #RGB / #RRGGBB / RGB / RRGGBB / rgb(r,g,b)，失败返回 None。"""
        if not color_str:
            return None
        cs = color_str.strip().lower()
        if cs.startswith("#"):
            hex_val = cs[1:]
        else:
            hex_val = cs
        if re.fullmatch(r"[0-9a-f]{3}|[0-9a-f]{6}", hex_val):
            if len(hex_val) == 3:
                hex_val = "".join(ch * 2 for ch in hex_val)
            return RGBColor(int(hex_val[0:2], 16), int(hex_val[2:4], 16), int(hex_val[4:6], 16))
        if cs.startswith("rgb("):
            m = re.fullmatch(r"rgb\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)", cs)
            if m:
                r, g, b = (min(255, max(0, int(v))) for v in m.groups())
                return RGBColor(r, g, b)
        if cs in self._NAMED_COLORS:
            r, g, b = self._NAMED_COLORS[cs]
            return RGBColor(r, g, b)
        return None

    @staticmethod
    def _parse_font_size(val) -> Optional[float]:
        """解析字号："16"/"16pt"（pt）或 "21px"（×0.75 折算 pt）。"""
        if val is None:
            return None
        if isinstance(val, (int, float)):
            v = float(val)
            return v if 1.0 <= v <= 400.0 else None
        s = str(val).strip().lower()
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(pt|px)?", s)
        if not m:
            return None
        num = float(m.group(1))
        if m.group(2) == "px":
            num *= 0.75
        return num if 1.0 <= num <= 400.0 else None

    def _parse_span_style(self, style_str: str) -> Dict[str, Any]:
        """解析 style 属性声明（color/font-family/font-size/weight/style/text-decoration）。"""
        props: Dict[str, Any] = {}
        for decl in style_str.split(";"):
            if ":" not in decl:
                continue
            key, val = decl.split(":", 1)
            key = key.strip().lower()
            val = val.strip().strip("\"' ")
            if not key or not val:
                continue
            if key == "color":
                c = self._parse_color(val)
                if c is not None:
                    props["color"] = c
            elif key in ("font-family", "fontfamily", "face"):
                props["font"] = val.split(",")[0].strip()
            elif key in ("font-size", "fontsize"):
                s = self._parse_font_size(val)
                if s is not None:
                    props["size"] = s
            elif key == "font-weight":
                if val.lower() in ("bold", "bolder", "700", "800", "900"):
                    props["bold"] = True
            elif key == "font-style":
                if val.lower() in ("italic", "oblique"):
                    props["italic"] = True
            elif key == "text-decoration":
                vl = val.lower()
                if "underline" in vl:
                    props["underline"] = True
                if "line-through" in vl:
                    props["strike"] = True
        return props

    def _apply_run_font(self, run, font: str) -> None:
        """设置 docx run 字体：ascii/hAnsi（西文）+ eastAsia（中文）同时覆盖。"""
        if not font:
            return
        run.font.name = font
        try:
            from docx.oxml.ns import qn as _qn
            rpr = run._element.get_or_add_rPr()
            rfonts = rpr.get_or_add_rFonts()
            rfonts.set(_qn("w:eastAsia"), font)
        except Exception:
            pass

    def _resolve_highlight(self, value) -> Any:
        """解析高亮色参数 → WD_COLOR_INDEX / 'clear'（清除）/ None（未指定）。
        非法值返回错误消息字符串。"""
        name = str(value or "").strip().lower()
        if not name:
            return None
        if name in ("none", "clear", "无"):
            return "clear"
        from docx.enum.text import WD_COLOR_INDEX as _WCI
        mapping = {
            "yellow": _WCI.YELLOW, "green": _WCI.BRIGHT_GREEN, "brightgreen": _WCI.BRIGHT_GREEN,
            "darkgreen": _WCI.GREEN, "cyan": _WCI.TURQUOISE, "turquoise": _WCI.TURQUOISE,
            "teal": _WCI.TEAL, "pink": _WCI.PINK, "red": _WCI.RED, "darkred": _WCI.DARK_RED,
            "blue": _WCI.BLUE, "darkblue": _WCI.DARK_BLUE, "purple": _WCI.VIOLET,
            "violet": _WCI.VIOLET, "gray": _WCI.GRAY_25, "grey": _WCI.GRAY_25,
            "lightgray": _WCI.GRAY_25, "gray25": _WCI.GRAY_25, "darkgray": _WCI.GRAY_50,
            "gray50": _WCI.GRAY_50, "black": _WCI.BLACK, "white": _WCI.WHITE,
        }
        if name not in mapping:
            return ("[Office Error]: 无效的 highlight（支持 yellow/green/cyan/teal/pink/red/"
                    "blue/purple/gray/black/white/none）")
        return mapping[name]

    def _coerce_pt(self, value, field: str) -> Any:
        """把参数转为 pt 数值；非法时返回错误消息字符串。"""
        if value is None or value == "":
            return None
        try:
            pt = float(value)
        except (TypeError, ValueError):
            return f"[Office Error]: {field} 必须是数字（pt）: {value!r}"
        if not (1 <= pt <= 400):
            return f"[Office Error]: {field} 越界（1-400）: {value!r}"
        return pt

    @staticmethod
    def _replace_limited(text: str, find: str, repl: str, limit: Optional[int]) -> tuple:
        """在 text 中替换 find→repl，最多 limit 次（None=不限）。返回 (新文本, 次数)。"""
        out_parts: List[str] = []
        idx = 0
        count = 0
        while limit is None or count < limit:
            j = text.find(find, idx)
            if j < 0:
                break
            out_parts.append(text[idx:j])
            out_parts.append(repl)
            idx = j + len(find)
            count += 1
        out_parts.append(text[idx:])
        return "".join(out_parts), count

    def _add_styled_run(self, paragraph, text: str, *, bold: bool = False,
                        italic: bool = False, color: Optional[Any] = None,
                        font: Optional[str] = None, size: Optional[float] = None,
                        underline: bool = False, strike: bool = False,
                        is_heading: bool = False) -> None:
        """解析内联格式并添加到段落。

        支持：行内代码 `code`、颜色/字体/字号 span
        （<span style="color:red;font-family:宋体;font-size:16">…</span>）、
        <font color="red">…</font>、下划线 <u>…</u>、删除线 ~~…~~、
        粗体 **…** / 斜体 *…*。
        base 渲染上下文（_ctx_font/_ctx_size，来自 docx_create/append 的
        base_font/base_size）：字体作用于全部 run（含标题），字号仅正文
        （is_heading=False），显式指定的值始终优先。
        """
        # 1) 行内代码 `code`（最高优先级，内部不再解析）
        m = re.search(r"`[^`]+`", text)
        if m:
            self._add_styled_run(paragraph, text[:m.start()], bold=bold, italic=italic,
                                 color=color, font=font, size=size, underline=underline,
                                 strike=strike, is_heading=is_heading)
            run = paragraph.add_run(m.group(0)[1:-1])
            run.font.name = "Courier New"
            run.font.size = Pt(9)
            if bold:
                run.bold = True
            if italic:
                run.italic = True
            if underline:
                run.underline = True
            if strike:
                run.font.strike = True
            if color is not None:
                run.font.color.rgb = color
            self._add_styled_run(paragraph, text[m.end():], bold=bold, italic=italic,
                                 color=color, font=font, size=size, underline=underline,
                                 strike=strike, is_heading=is_heading)
            return

        # 2) 样式标签 <span style="...">…</span> / <font color="...">…</font>
        m = re.search(
            r"<span[^>]*style=[\"']([^\"']+)[\"'][^>]*>(.*?)</span>"
            r"|<font[^>]*color=[\"']([^\"']+)[\"'][^>]*>(.*?)</font>",
            text, re.IGNORECASE | re.DOTALL,
        )
        if m:
            if m.group(1) is not None:  # span：解析全部声明
                props = self._parse_span_style(m.group(1))
            else:  # font：仅颜色
                c = self._parse_color(m.group(3))
                props = {"color": c} if c is not None else {}
            inner = m.group(2) if m.group(2) is not None else m.group(4)
            self._add_styled_run(paragraph, text[:m.start()], bold=bold, italic=italic,
                                 color=color, font=font, size=size, underline=underline,
                                 strike=strike, is_heading=is_heading)
            self._add_styled_run(paragraph, inner,
                                 bold=props.get("bold", False) or bold,
                                 italic=props.get("italic", False) or italic,
                                 color=props.get("color", color),
                                 font=props.get("font", font),
                                 size=props.get("size", size),
                                 underline=props.get("underline", False) or underline,
                                 strike=props.get("strike", False) or strike,
                                 is_heading=is_heading)
            self._add_styled_run(paragraph, text[m.end():], bold=bold, italic=italic,
                                 color=color, font=font, size=size, underline=underline,
                                 strike=strike, is_heading=is_heading)
            return

        # 3) 下划线 <u>…</u>
        m = re.search(r"<u>(.*?)</u>", text, re.IGNORECASE | re.DOTALL)
        if m:
            self._add_styled_run(paragraph, text[:m.start()], bold=bold, italic=italic,
                                 color=color, font=font, size=size, underline=underline,
                                 strike=strike, is_heading=is_heading)
            self._add_styled_run(paragraph, m.group(1), bold=bold, italic=italic,
                                 color=color, font=font, size=size, underline=True,
                                 strike=strike, is_heading=is_heading)
            self._add_styled_run(paragraph, text[m.end():], bold=bold, italic=italic,
                                 color=color, font=font, size=size, underline=underline,
                                 strike=strike, is_heading=is_heading)
            return

        # 4) 删除线 ~~…~~
        m = re.search(r"~~([^~]+)~~", text)
        if m:
            self._add_styled_run(paragraph, text[:m.start()], bold=bold, italic=italic,
                                 color=color, font=font, size=size, underline=underline,
                                 strike=strike, is_heading=is_heading)
            self._add_styled_run(paragraph, m.group(1), bold=bold, italic=italic,
                                 color=color, font=font, size=size, underline=underline,
                                 strike=True, is_heading=is_heading)
            self._add_styled_run(paragraph, text[m.end():], bold=bold, italic=italic,
                                 color=color, font=font, size=size, underline=underline,
                                 strike=strike, is_heading=is_heading)
            return

        # 5) 粗体 / 斜体
        m = re.search(r"\*\*[^*]+\*\*|\*[^*]+\*", text)
        if m:
            token = m.group(0)
            self._add_styled_run(paragraph, text[:m.start()], bold=bold, italic=italic,
                                 color=color, font=font, size=size, underline=underline,
                                 strike=strike, is_heading=is_heading)
            if token.startswith("**"):
                self._add_styled_run(paragraph, token[2:-2], bold=True, italic=italic,
                                     color=color, font=font, size=size, underline=underline,
                                     strike=strike, is_heading=is_heading)
            else:
                self._add_styled_run(paragraph, token[1:-1], bold=bold, italic=True,
                                     color=color, font=font, size=size, underline=underline,
                                     strike=strike, is_heading=is_heading)
            self._add_styled_run(paragraph, text[m.end():], bold=bold, italic=italic,
                                 color=color, font=font, size=size, underline=underline,
                                 strike=strike, is_heading=is_heading)
            return

        # 6) 纯文本
        if text:
            run = paragraph.add_run(text)
            if bold:
                run.bold = True
            if italic:
                run.italic = True
            if underline:
                run.underline = True
            if strike:
                run.font.strike = True
            if color is not None:
                run.font.color.rgb = color
            eff_font = font or self._ctx_font
            eff_size = size if size is not None else (None if is_heading else self._ctx_size)
            if eff_font:
                self._apply_run_font(run, eff_font)
            if eff_size is not None:
                run.font.size = Pt(eff_size)

    def _add_docx_image(self, doc, img_path: str, alt: str = "") -> None:
        """嵌入图片到 docx（居中，自动缩放适配页宽，可带图注）。"""
        resolved = img_path
        if img_path.startswith("file://"):
            resolved = img_path[len("file://"):]
        if not os.path.isabs(resolved):
            resolved = os.path.join(self.workspace, resolved)

        if not os.path.isfile(resolved):
            p = doc.add_paragraph(f"[图片未找到: {img_path}]")
            if p.runs:
                p.runs[0].font.color.rgb = RGBColor(0xC0, 0x00, 0x00)
            return

        # 计算合适宽度：默认页宽 6.5in，留边距取 6.0in；若图片原始更小则原尺寸
        width_inches = 6.0
        try:
            from PIL import Image as _PILImage
            with _PILImage.open(resolved) as im:
                w_px, h_px = im.size
            # 96 DPI 近似换算英寸
            w_in = w_px / 96.0
            h_in = h_px / 96.0
            if w_in <= 6.0:
                width_inches = max(1.0, w_in)
            else:
                # 按宽度缩放，同时保证高度不超过页面可用高度（约 8.5in）
                scale = min(6.0 / w_in, 8.5 / h_in)
                width_inches = w_in * scale
        except Exception:
            pass  # 无法读取尺寸时使用默认宽度 6.0in

        try:
            doc.add_picture(resolved, width=Inches(width_inches))
            doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
        except Exception as exc:
            p = doc.add_paragraph(f"[图片嵌入失败: {exc}]")
            if p.runs:
                p.runs[0].font.color.rgb = RGBColor(0xC0, 0x00, 0x00)
            return

        if alt:
            cap = doc.add_paragraph(alt)
            cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            for run in cap.runs:
                run.italic = True
                run.font.size = Pt(9)

    def _render_diagram_block(self, lang: str, code: str, seq: int) -> Optional[str]:
        """把 ```plantuml / ```mermaid 代码块渲染成 PNG，返回图片绝对路径。

        工具层兜底：无论 Agent 是否调用 diagram-to-office 技能，文档中的
        图表代码块都会被渲染成图片嵌入；渲染失败返回 None（调用方回退
        保留源码文本，绝不丢弃图表内容）。
        """
        try:
            import importlib.util
            import tempfile
            import time as _time

            rd_path = None
            # 开发态：litework/tools/office.py → 仓库 skills/
            cand = os.path.abspath(os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "..", "..", "skills",
                "diagram-to-office", "render_diagram.py"))
            if os.path.isfile(cand):
                rd_path = cand
            else:
                # 打包态：PyInstaller datas → _MEIPASS/skills/...
                meipass = getattr(sys, "_MEIPASS", None)
                if meipass:
                    cand2 = os.path.join(meipass, "skills", "diagram-to-office", "render_diagram.py")
                    if os.path.isfile(cand2):
                        rd_path = cand2
            if rd_path is None:
                return None

            spec = importlib.util.spec_from_file_location("litework_render_diagram", rd_path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            chart_type = "plantuml" if lang in ("plantuml", "puml") else "mermaid"
            out_dir = _ensure_output_dir(self.workspace, "diagrams")
            out_path = os.path.join(out_dir, f"diagram_{int(_time.time() * 1000)}_{seq}.png")

            def _do_render() -> None:
                with tempfile.TemporaryDirectory(prefix="litework-md-diagram-") as tmpdir:
                    if chart_type == "plantuml":
                        mod.render_plantuml(code, out_path, 2, tmpdir, None)
                    else:
                        mod.render_mermaid(code, out_path, 2, tmpdir)

            # 60s 硬超时：单图渲染超时即放弃（回退源码文本），不拖死整个
            # 文档生成——工具执行已在事件循环外的线程池，但用户等不了几分钟
            from concurrent.futures import ThreadPoolExecutor as _TPE
            with _TPE(max_workers=1) as pool:
                future = pool.submit(_do_render)
                future.result(timeout=60)
            if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                return out_path
            return None
        except Exception:
            # 引擎缺失/语法错误/渲染失败 → 回退源码文本（内容不丢）
            return None

    # ------------------------------------------------------------ docx 格式化 / 查找替换

    def _iter_docx_paragraphs(self, doc):
        """遍历正文与表格（含嵌套）中的所有段落，按底层元素去重。"""
        seen = set()

        def _walk_table(table):
            for row in table.rows:
                for cell in row.cells:
                    for p in cell.paragraphs:
                        if p._element not in seen:
                            seen.add(p._element)
                            yield p
                    for t in cell.tables:
                        yield from _walk_table(t)

        for p in doc.paragraphs:
            if p._element not in seen:
                seen.add(p._element)
                yield p
        for t in doc.tables:
            yield from _walk_table(t)

    @staticmethod
    def _is_heading_level(p, level: int) -> bool:
        """判断段落是否为指定层级标题（兼容中文 Word 的「标题 N」样式名）。"""
        try:
            name = (p.style.name or "").strip().lower()
        except Exception:
            return False
        return name in (f"heading {level}", f"heading{level}",
                        f"标题 {level}", f"标题{level}")

    def _docx_format(self, args: Dict[str, Any]) -> str:
        if not _HAS_DOCX:
            return _missing_dep_msg("python-docx", "docx_format")

        rel_path = str(args.get("path") or "").strip()
        if not rel_path:
            return "[Office Error]: 缺少 path 参数"
        try:
            resolved = self._resolve_path(rel_path)
        except ValueError as exc:
            return f"[Office Error]: {exc}"

        # ---------- 参数解析 ----------
        target = str(args.get("target") or "all").strip().lower()
        if target not in ("all", "heading", "search"):
            return f"[Office Error]: 无效的 target {target!r}（支持 all/heading/search）"
        search_text = str(args.get("search_text") or "")
        heading_level = args.get("heading_level")
        if target == "search" and not search_text.strip():
            return "[Office Error]: target=search 需要提供 search_text"
        if target == "heading":
            try:
                heading_level = int(heading_level)
                if not 1 <= heading_level <= 6:
                    raise ValueError
            except (TypeError, ValueError):
                return "[Office Error]: target=heading 需要 heading_level（1-6）"

        bold = args.get("bold")
        italic = args.get("italic")
        underline = args.get("underline")
        strike = args.get("strike")
        font = str(args.get("font") or "").strip() or None
        size = self._coerce_pt(args.get("size"), "size")
        if isinstance(size, str):
            return size
        color = None
        if args.get("color"):
            color = self._parse_color(str(args.get("color")))
            if color is None:
                return f"[Office Error]: 无效的 color 值: {args.get('color')}"
        highlight = self._resolve_highlight(args.get("highlight"))
        if isinstance(highlight, str):
            return highlight
        alignment = None
        if args.get("alignment"):
            alignment = {
                "left": WD_ALIGN_PARAGRAPH.LEFT, "center": WD_ALIGN_PARAGRAPH.CENTER,
                "right": WD_ALIGN_PARAGRAPH.RIGHT, "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
            }.get(str(args.get("alignment")).strip().lower())
            if alignment is None:
                return "[Office Error]: 无效的 alignment（支持 left/center/right/justify）"
        line_spacing = args.get("line_spacing")
        if line_spacing is not None and str(line_spacing).strip() != "":
            try:
                line_spacing = float(line_spacing)
                if not 0.5 <= line_spacing <= 5:
                    raise ValueError
            except (TypeError, ValueError):
                return "[Office Error]: line_spacing 需为 0.5-5 的行距倍数（如 1.5）"
        else:
            line_spacing = None

        has_run_fmt = any(x is not None for x in (bold, italic, underline, strike)) or \
            bool(font) or size is not None or color is not None or highlight is not None
        has_para_fmt = alignment is not None or line_spacing is not None
        if not (has_run_fmt or has_para_fmt):
            return ("[Office Error]: 未指定任何格式属性（font/size/bold/italic/underline/"
                    "strike/color/highlight/alignment/line_spacing 至少一项）")

        try:
            doc = _DocxDoc(resolved)
        except Exception as exc:
            return f"[Office Error]: 无法打开文档: {exc}"

        matched_paras = 0
        touched_runs = 0
        for p in self._iter_docx_paragraphs(doc):
            if target == "heading" and not self._is_heading_level(p, heading_level):
                continue
            if target == "search" and search_text not in p.text:
                continue
            matched_paras += 1
            if target == "search":
                hit_runs = [r for r in p.runs if search_text in r.text]
                runs = hit_runs if hit_runs else list(p.runs)
            else:
                runs = list(p.runs)
            for run in runs:
                if bold is not None:
                    run.bold = bold
                if italic is not None:
                    run.italic = italic
                if underline is not None:
                    run.underline = underline
                if strike is not None:
                    run.font.strike = strike
                if font:
                    self._apply_run_font(run, font)
                if size is not None:
                    run.font.size = Pt(size)
                if color is not None:
                    run.font.color.rgb = color
                if highlight == "clear":
                    run.font.highlight_color = None
                elif highlight is not None:
                    run.font.highlight_color = highlight
                touched_runs += 1
            if alignment is not None:
                p.alignment = alignment
            if line_spacing is not None:
                p.paragraph_format.line_spacing = line_spacing

        if matched_paras == 0:
            return ("[Office Error]: 未找到匹配内容"
                    + (f"（search_text={search_text!r}）" if target == "search"
                       else f"（heading_level={heading_level}）" if target == "heading" else ""))

        try:
            doc.save(resolved)
        except Exception as exc:
            return f"[Office Error]: 保存文档失败: {exc}"

        prop_bits: List[str] = []
        if font:
            prop_bits.append(f"字体={font}")
        if size is not None:
            prop_bits.append(f"字号={size:g}pt")
        if bold is not None:
            prop_bits.append("加粗" if bold else "取消加粗")
        if italic is not None:
            prop_bits.append("斜体" if italic else "取消斜体")
        if underline is not None:
            prop_bits.append("下划线" if underline else "取消下划线")
        if strike is not None:
            prop_bits.append("删除线" if strike else "取消删除线")
        if color is not None:
            prop_bits.append(f"颜色=#{color}")
        if highlight is not None:
            prop_bits.append("清除高亮" if highlight == "clear" else "高亮背景")
        if alignment is not None:
            prop_bits.append(f"对齐={args.get('alignment')}")
        if line_spacing is not None:
            prop_bits.append(f"行距={line_spacing:g}")

        return (f"[Office OK]: 已格式化 {matched_paras} 个段落 / {touched_runs} 个 run"
                f"（{'、'.join(prop_bits)}）→ {resolved}")

    def _docx_replace(self, args: Dict[str, Any]) -> str:
        if not _HAS_DOCX:
            return _missing_dep_msg("python-docx", "docx_replace")

        rel_path = str(args.get("path") or "").strip()
        if not rel_path:
            return "[Office Error]: 缺少 path 参数"
        try:
            resolved = self._resolve_path(rel_path)
        except ValueError as exc:
            return f"[Office Error]: {exc}"

        find = str(args.get("find") or "")
        if not find:
            return "[Office Error]: 缺少 find 参数"
        repl = str(args.get("replace") if args.get("replace") is not None else "")
        max_count = args.get("max_count")
        if max_count is not None and str(max_count).strip() != "":
            try:
                max_count = int(max_count)
                if max_count < 1:
                    raise ValueError
            except (TypeError, ValueError):
                return "[Office Error]: max_count 需为正整数"
        else:
            max_count = None

        try:
            doc = _DocxDoc(resolved)
        except Exception as exc:
            return f"[Office Error]: 无法打开文档: {exc}"

        replaced = 0
        for p in self._iter_docx_paragraphs(doc):
            if max_count is not None and replaced >= max_count:
                break
            # 1) run 内替换（保留原格式）
            for run in p.runs:
                if max_count is not None and replaced >= max_count:
                    break
                if find in run.text:
                    new_text, n = self._replace_limited(
                        run.text, find, repl,
                        max_count - replaced if max_count is not None else None)
                    if n:
                        run.text = new_text
                        replaced += n
            # 2) 跨 run 命中：段落级合并重建（沿用首 run 格式）
            if (max_count is None or replaced < max_count) and find in p.text:
                runs = list(p.runs)
                if runs and "".join(r.text for r in runs) == p.text:
                    remaining = max_count - replaced if max_count is not None else None
                    new_text, n = self._replace_limited(p.text, find, repl, remaining)
                    if n:
                        runs[0].text = new_text
                        for r in runs[1:]:
                            r._element.getparent().remove(r._element)
                        replaced += n

        if replaced == 0:
            return f"[Office Error]: 未找到要替换的内容: {find!r}"

        try:
            doc.save(resolved)
        except Exception as exc:
            return f"[Office Error]: 保存文档失败: {exc}"
        return f"[Office OK]: 已替换 {replaced} 处 “{find}” → “{repl}” → {resolved}"

    # ------------------------------------------------------------ xlsx

    def _xlsx_create(self, args: Dict[str, Any]) -> str:
        if not _HAS_OPENPYXL:
            return _missing_dep_msg("openpyxl", "xlsx_create")

        data_str = args.get("data", "")
        filename = _safe_filename(args.get("filename", "表格.xlsx"))

        if not filename.lower().endswith(".xlsx"):
            filename += ".xlsx"

        try:
            data = json.loads(data_str)
        except json.JSONDecodeError as e:
            return f"[Office Error]: data 不是有效的 JSON: {e}"

        wb = _openpyxl.Workbook()
        # 删除默认 sheet
        wb.remove(wb.active)

        if isinstance(data, dict):
            # 多 sheet：{"sheet1": [...], "sheet2": [...]}
            for sheet_name, rows in data.items():
                if not isinstance(rows, list) or not rows:
                    continue
                ws = wb.create_sheet(title=str(sheet_name)[:31])
                self._write_rows_to_sheet(ws, rows)
        elif isinstance(data, list):
            # 单 sheet
            ws = wb.active or wb.create_sheet(title="Sheet1")
            self._write_rows_to_sheet(ws, data)
        else:
            return "[Office Error]: data 必须是 JSON 数组或对象"

        out_dir = _ensure_output_dir(self.workspace)
        filepath = os.path.join(out_dir, filename)
        wb.save(filepath)

        return f"[Office OK]: 已生成 Excel 表格 → {filepath}"

    def _write_rows_to_sheet(self, ws, rows: List[Any]) -> None:
        """将数据行写入 worksheet。第一行对象自动提取表头。"""
        if not rows:
            return

        headers: List[str] = []
        if isinstance(rows[0], dict):
            headers = list(rows[0].keys())
            # 写入表头
            for c, h in enumerate(headers, 1):
                cell = ws.cell(row=1, column=c, value=str(h))
                cell.font = _openpyxl.styles.Font(bold=True)
            # 写入数据
            for r, row in enumerate(rows, 2):
                for c, h in enumerate(headers, 1):
                    val = row.get(h, "")
                    if isinstance(val, (list, dict)):
                        val = json.dumps(val, ensure_ascii=False)
                    ws.cell(row=r, column=c, value=val)
        else:
            # 简单列表
            for r, val in enumerate(rows, 1):
                if isinstance(val, (list, tuple)):
                    for c, v in enumerate(val, 1):
                        ws.cell(row=r, column=c, value=v)
                else:
                    ws.cell(row=r, column=1, value=val)

    # ------------------------------------------------------------ xlsx 格式化 / 查找替换

    def _color_hex(self, color_str: str) -> Optional[str]:
        """解析颜色为 'RRGGBB' 十六进制（openpyxl 用），失败返回 None。"""
        c = self._parse_color(color_str)
        if c is None:
            return None
        return "%02X%02X%02X" % (c[0], c[1], c[2])

    @staticmethod
    def _parse_xlsx_range(ws, range_str: str):
        """解析 range → (min_col, min_row, max_col, max_row)。失败抛 ValueError。
        支持：all（已用区域）/ A:C（整列）/ 1:5（整行）/ A1 / A1:C10。"""
        from openpyxl.utils import column_index_from_string
        rs = (range_str or "all").strip().lower()
        if not rs or rs in ("all", "used", "已用区域"):
            return 1, 1, max(1, ws.max_column or 1), max(1, ws.max_row or 1)
        m = re.fullmatch(r"([a-z]+):([a-z]+)", rs)
        if m:
            c1 = column_index_from_string(m.group(1).upper())
            c2 = column_index_from_string(m.group(2).upper())
            return min(c1, c2), 1, max(c1, c2), max(1, ws.max_row or 1)
        m = re.fullmatch(r"(\d+):(\d+)", rs)
        if m:
            r1, r2 = int(m.group(1)), int(m.group(2))
            return 1, min(r1, r2), max(1, ws.max_column or 1), max(r1, r2)
        m = re.fullmatch(r"([a-z]+)(\d+)(?::([a-z]+)(\d+))?", rs)
        if m:
            c1 = column_index_from_string(m.group(1).upper())
            r1 = int(m.group(2))
            if m.group(3):
                c2 = column_index_from_string(m.group(3).upper())
                r2 = int(m.group(4))
            else:
                c2, r2 = c1, r1
            return min(c1, c2), min(r1, r2), max(c1, c2), max(r1, r2)
        raise ValueError(f"无效的 range: {range_str!r}（支持 all / A:C / 1:5 / A1 / A1:C10）")

    def _xlsx_format(self, args: Dict[str, Any]) -> str:
        if not _HAS_OPENPYXL:
            return _missing_dep_msg("openpyxl", "xlsx_format")

        rel_path = str(args.get("path") or "").strip()
        if not rel_path:
            return "[Office Error]: 缺少 path 参数"
        try:
            resolved = self._resolve_path(rel_path)
        except ValueError as exc:
            return f"[Office Error]: {exc}"

        sheet_name = str(args.get("sheet") or "").strip()
        range_str = str(args.get("range") or "all").strip()

        bold = args.get("bold")
        italic = args.get("italic")
        underline = args.get("underline")
        font = str(args.get("font") or "").strip() or None
        size = self._coerce_pt(args.get("size"), "size")
        if isinstance(size, str):
            return size
        color_hex = fill_hex = None
        if args.get("color"):
            color_hex = self._color_hex(str(args.get("color")))
            if color_hex is None:
                return f"[Office Error]: 无效的 color 值: {args.get('color')}"
        if args.get("fill"):
            fill_hex = self._color_hex(str(args.get("fill")))
            if fill_hex is None:
                return f"[Office Error]: 无效的 fill 值: {args.get('fill')}"
        border_style = None  # None=不动 / ''=清除 / 具体样式
        if args.get("border") is not None:
            b = args.get("border")
            if b is True:
                border_style = "thin"
            elif b is False or str(b).strip().lower() in ("none", "无"):
                border_style = ""
            else:
                border_style = str(b).strip().lower()
                if border_style not in ("thin", "medium", "thick", "dashed", "dotted",
                                        "double", "hair"):
                    return ("[Office Error]: border 支持 true 或 "
                            "thin/medium/thick/dashed/dotted/double/hair/none")
        alignment = None
        if str(args.get("alignment") or "").strip():
            alignment = str(args.get("alignment")).strip().lower()
            if alignment not in ("left", "center", "right"):
                return "[Office Error]: alignment 支持 left/center/right"
        wrap_text = args.get("wrap_text")
        number_format = args.get("number_format")
        if number_format is not None and not isinstance(number_format, str):
            number_format = str(number_format)
        auto_fit = bool(args.get("auto_fit", False))

        freeze_raw = str(args.get("freeze") or "").strip()
        freeze_coords: Optional[str] = None
        freeze_clear = False
        if freeze_raw:
            fl = freeze_raw.lower()
            if fl in ("none", "off", "无"):
                freeze_clear = True
            elif re.fullmatch(r"[a-z]{1,3}\d+", fl):
                freeze_coords = fl.upper()
            else:
                return "[Office Error]: freeze 需为单元格坐标（如 'A2' 冻结首行）或 'none' 解除冻结"

        has_cell_fmt = any(x is not None for x in (bold, italic, underline)) or bool(font) \
            or size is not None or color_hex or fill_hex or border_style is not None \
            or alignment is not None or wrap_text is not None or number_format is not None
        if not (has_cell_fmt or auto_fit or freeze_coords or freeze_clear):
            return ("[Office Error]: 未指定任何样式属性（font/size/bold/italic/underline/"
                    "color/fill/border/alignment/wrap_text/number_format/auto_fit/freeze 至少一项）")

        try:
            wb = _openpyxl.load_workbook(resolved)
        except Exception as exc:
            return f"[Office Error]: 无法打开 Excel: {exc}"
        if sheet_name:
            if sheet_name not in wb.sheetnames:
                return f"[Office Error]: sheet {sheet_name!r} 不存在（可选: {', '.join(wb.sheetnames)}）"
            ws = wb[sheet_name]
        else:
            ws = wb.active

        try:
            min_c, min_r, max_c, max_r = self._parse_xlsx_range(ws, range_str)
        except ValueError as exc:
            return f"[Office Error]: {exc}"

        cell_count = (max_c - min_c + 1) * (max_r - min_r + 1)
        if cell_count > 200_000:
            return f"[Office Error]: 区域过大（{cell_count} 单元格），请缩小 range（上限 20 万）"

        from copy import copy as _copy
        from openpyxl.styles import Border as _XBorder, PatternFill as _XFill, Side as _XSide

        styled_cells = 0
        if has_cell_fmt:
            need_font = any(x is not None for x in (bold, italic, underline)) or bool(font) \
                or size is not None or color_hex
            for row in ws.iter_rows(min_row=min_r, max_row=max_r,
                                    min_col=min_c, max_col=max_c):
                for cell in row:
                    if need_font:
                        f = _copy(cell.font)
                        if bold is not None:
                            f.b = bold
                        if italic is not None:
                            f.i = italic
                        if underline is not None:
                            f.u = "single" if underline else None
                        if font:
                            f.name = font
                        if size is not None:
                            f.sz = size
                        if color_hex:
                            f.color = color_hex
                        cell.font = f
                    if fill_hex:
                        cell.fill = _XFill(start_color=fill_hex, end_color=fill_hex,
                                           fill_type="solid")
                    if border_style is not None:
                        if border_style == "":
                            cell.border = _XBorder()
                        else:
                            side = _XSide(style=border_style)
                            cell.border = _XBorder(left=side, right=side, top=side, bottom=side)
                    if alignment is not None or wrap_text is not None:
                        a = _copy(cell.alignment)
                        if alignment is not None:
                            a.horizontal = alignment
                        if wrap_text is not None:
                            a.wrap_text = wrap_text
                        cell.alignment = a
                    if number_format is not None:
                        cell.number_format = number_format
                    styled_cells += 1

        if auto_fit:
            from openpyxl.utils import get_column_letter
            max_scan_row = min(max(1, ws.max_row or 1), 10_000)
            for c in range(min_c, max_c + 1):
                width = 8.0
                for r in range(1, max_scan_row + 1):
                    v = ws.cell(row=r, column=c).value
                    if v is None:
                        continue
                    w = sum(2 if ord(ch) > 127 else 1 for ch in str(v))
                    width = max(width, min(w + 2.0, 60.0))
                ws.column_dimensions[get_column_letter(c)].width = width

        if freeze_coords:
            ws.freeze_panes = freeze_coords
        elif freeze_clear:
            ws.freeze_panes = None

        try:
            wb.save(resolved)
        except Exception as exc:
            return f"[Office Error]: 保存 Excel 失败: {exc}"

        bits: List[str] = []
        if styled_cells:
            bits.append(f"{styled_cells} 个单元格样式")
        if auto_fit:
            bits.append(f"{max_c - min_c + 1} 列自适应列宽")
        if freeze_coords:
            bits.append(f"冻结窗格 {freeze_coords}")
        elif freeze_clear:
            bits.append("解除冻结")
        return f"[Office OK]: 已更新（{'、'.join(bits)}）→ {resolved}"

    def _xlsx_replace(self, args: Dict[str, Any]) -> str:
        if not _HAS_OPENPYXL:
            return _missing_dep_msg("openpyxl", "xlsx_replace")

        rel_path = str(args.get("path") or "").strip()
        if not rel_path:
            return "[Office Error]: 缺少 path 参数"
        try:
            resolved = self._resolve_path(rel_path)
        except ValueError as exc:
            return f"[Office Error]: {exc}"

        find = str(args.get("find") or "")
        if not find:
            return "[Office Error]: 缺少 find 参数"
        repl = str(args.get("replace") if args.get("replace") is not None else "")
        match_case = bool(args.get("match_case", True))
        sheet_name = str(args.get("sheet") or "").strip()
        range_str = str(args.get("range") or "all").strip()

        try:
            wb = _openpyxl.load_workbook(resolved)
        except Exception as exc:
            return f"[Office Error]: 无法打开 Excel: {exc}"
        if sheet_name:
            if sheet_name not in wb.sheetnames:
                return f"[Office Error]: sheet {sheet_name!r} 不存在（可选: {', '.join(wb.sheetnames)}）"
            sheets = [wb[sheet_name]]
        else:
            sheets = list(wb.worksheets)

        total = 0
        for ws in sheets:
            try:
                min_c, min_r, max_c, max_r = self._parse_xlsx_range(ws, range_str)
            except ValueError as exc:
                return f"[Office Error]: {exc}"
            for row in ws.iter_rows(min_row=min_r, max_row=max_r,
                                    min_col=min_c, max_col=max_c):
                for cell in row:
                    v = cell.value
                    if not isinstance(v, str) or not v:
                        continue
                    if match_case:
                        if find in v:
                            total += v.count(find)
                            cell.value = v.replace(find, repl)
                    else:
                        pattern = re.compile(re.escape(find), re.IGNORECASE)
                        n = len(pattern.findall(v))
                        if n:
                            total += n
                            cell.value = pattern.sub(lambda _m: repl, v)

        if total == 0:
            return f"[Office Error]: 未找到要替换的内容: {find!r}"

        try:
            wb.save(resolved)
        except Exception as exc:
            return f"[Office Error]: 保存 Excel 失败: {exc}"
        return f"[Office OK]: 已替换 {total} 处 “{find}” → “{repl}” → {resolved}"

    # ------------------------------------------------------------ pptx

    def _pptx_create(self, args: Dict[str, Any]) -> str:
        if not _HAS_PPTX:
            return _missing_dep_msg("python-pptx", "pptx_create")

        slides_str = args.get("slides", "")
        filename = _safe_filename(args.get("filename", "演示文稿.pptx"))
        title = args.get("title", "")

        if not filename.lower().endswith(".pptx"):
            filename += ".pptx"

        try:
            slides_data = json.loads(slides_str)
        except json.JSONDecodeError as e:
            return f"[Office Error]: slides 不是有效的 JSON: {e}"

        if not isinstance(slides_data, list):
            return "[Office Error]: slides 必须是 JSON 数组"

        prs = _PptxPresentation()

        # 封面
        if title:
            slide = prs.slides.add_slide(prs.slide_layouts[0])
            slide.shapes.title.text = title
            if slide.placeholders[1]:
                slide.placeholders[1].text = f"生成于 lite-work Office"

        for slide_data in slides_data:
            slide_title = slide_data.get("title", "")
            content = slide_data.get("content", "")
            bullets = slide_data.get("bullets", [])
            image = slide_data.get("image", "")

            # 图表代码块兜底：content 里的 ```plantuml/```mermaid 渲染成图片，
            # 自动作为该页 image（未显式指定时），代码块替换为占位说明（内容不丢）
            if content:
                for _lang in ("plantuml", "puml", "mermaid", "mmd"):
                    _pat = re.compile(
                        r"```[ \t]*" + _lang + r"[ \t]*\n(.*?)```", re.S)
                    _m = _pat.search(content)
                    if _m:
                        _img = self._render_diagram_block(_lang, _m.group(1), len(prs.slides._sldIdLst))
                        if _img:
                            if not image:
                                image = _img
                            _rel = os.path.relpath(_img, self.workspace).replace("\\", "/")
                            content = _pat.sub(f"📊 图表已渲染：{_rel}", content, count=1)
                            break

            slide = prs.slides.add_slide(prs.slide_layouts[1])
            slide.shapes.title.text = slide_title

            # 图片嵌入：居中放置在标题下方，宽度/高度自动缩放适配
            pic_emu_w = 0
            pic_emu_h = 0
            if image:
                img_path = image
                if img_path.startswith("file://"):
                    img_path = img_path[len("file://"):]
                if not os.path.isabs(img_path):
                    img_path = os.path.join(self.workspace, img_path)

                if not os.path.isfile(img_path):
                    body = slide.placeholders[1]
                    body.text_frame.text = f"[图片未找到: {image}]"
                else:
                    try:
                        from PIL import Image as _PILImage
                        with _PILImage.open(img_path) as im:
                            w_px, h_px = im.size
                    except Exception:
                        w_px, h_px = 0, 0

                    slide_w_in = prs.slide_width / 914400  # EMU → 英寸
                    # 图片最大占宽 90%，最大高 4.0in（给正文留底部空间）
                    max_w_in = slide_w_in * 0.9
                    max_h_in = 4.0
                    if w_px and h_px:
                        w_in = w_px / 96.0
                        h_in = h_px / 96.0
                        scale = min(max_w_in / w_in, max_h_in / h_in, 1.0)
                        w_in = max(1.0, w_in * scale)
                        h_in = max(1.0, h_in * scale)
                    else:
                        w_in, h_in = max_w_in, max_h_in

                    pic_emu_w = int(w_in * 914400)
                    pic_emu_h = int(h_in * 914400)
                    left = (prs.slide_width - pic_emu_w) // 2
                    top = _PptxInches(1.25)
                    try:
                        slide.shapes.add_picture(
                            img_path, left=left, top=top,
                            width=pic_emu_w, height=pic_emu_h,
                        )
                    except Exception as exc:
                        body = slide.placeholders[1]
                        body.text_frame.text = f"[图片嵌入失败: {exc}]"
                        pic_emu_w = pic_emu_h = 0

            if bullets:
                # 使用占位符中的文本框；有图片时把正文挪到图片下方
                if pic_emu_w:
                    tb_left = _PptxInches(0.5)
                    tb_top = _PptxInches(1.25) + pic_emu_h + _PptxInches(0.15)
                    tb_width = prs.slide_width - _PptxInches(1.0)
                    tb_height = max(_PptxInches(0.5), prs.slide_height - tb_top - _PptxInches(0.3))
                    tb = slide.shapes.add_textbox(tb_left, tb_top, tb_width, tb_height)
                    text_frame = tb.text_frame
                    text_frame.word_wrap = True
                else:
                    body = slide.placeholders[1]
                    text_frame = body.text_frame
                    text_frame.clear()
                for i, bullet in enumerate(bullets):
                    if i == 0:
                        text_frame.paragraphs[0].text = str(bullet)
                    else:
                        p = text_frame.add_paragraph()
                        p.text = str(bullet)
            elif content:
                if pic_emu_w:
                    tb_left = _PptxInches(0.5)
                    tb_top = _PptxInches(1.25) + pic_emu_h + _PptxInches(0.15)
                    tb_width = prs.slide_width - _PptxInches(1.0)
                    tb_height = max(_PptxInches(0.5), prs.slide_height - tb_top - _PptxInches(0.3))
                    tb = slide.shapes.add_textbox(tb_left, tb_top, tb_width, tb_height)
                    text_frame = tb.text_frame
                    text_frame.word_wrap = True
                else:
                    body = slide.placeholders[1]
                    text_frame = body.text_frame
                    text_frame.clear()
                text_frame.paragraphs[0].text = content[:500]

        out_dir = _ensure_output_dir(self.workspace)
        filepath = os.path.join(out_dir, filename)
        prs.save(filepath)

        return f"[Office OK]: 已生成演示文稿 → {filepath}"

    # ------------------------------------------------------------ pptx 格式化

    @staticmethod
    def _apply_pptx_ea_font(run, font: str) -> None:
        """为 pptx run 补充设置东亚字体（a:ea），保证中文字形生效。"""
        try:
            from pptx.oxml.ns import qn as _qn
            rPr = run._r.get_or_add_rPr()
            ea = rPr.find(_qn("a:ea"))
            if ea is None:
                ea = rPr.makeelement(_qn("a:ea"), {})
                latin = rPr.find(_qn("a:latin"))
                if latin is not None:
                    latin.addnext(ea)
                else:
                    rPr.append(ea)
            ea.set("typeface", font)
        except Exception:
            pass

    def _pptx_format(self, args: Dict[str, Any]) -> str:
        if not _HAS_PPTX:
            return _missing_dep_msg("python-pptx", "pptx_format")

        rel_path = str(args.get("path") or "").strip()
        if not rel_path:
            return "[Office Error]: 缺少 path 参数"
        try:
            resolved = self._resolve_path(rel_path)
        except ValueError as exc:
            return f"[Office Error]: {exc}"

        target = str(args.get("target") or "all").strip().lower()
        if target not in ("title", "body", "all"):
            return "[Office Error]: target 支持 title/body/all"
        slide_arg = str(args.get("slide") or "all").strip().lower()

        bold = args.get("bold")
        italic = args.get("italic")
        underline = args.get("underline")
        font = str(args.get("font") or "").strip() or None
        size = self._coerce_pt(args.get("size"), "size")
        if isinstance(size, str):
            return size
        color = None
        if args.get("color"):
            color = self._parse_color(str(args.get("color")))
            if color is None:
                return f"[Office Error]: 无效的 color 值: {args.get('color')}"
        if not (any(x is not None for x in (bold, italic, underline)) or font
                or size is not None or color is not None):
            return "[Office Error]: 未指定任何格式属性（font/size/bold/italic/underline/color 至少一项）"

        try:
            prs = _PptxPresentation(resolved)
        except Exception as exc:
            return f"[Office Error]: 无法打开演示文稿: {exc}"
        slides = list(prs.slides)
        if not slides:
            return "[Office Error]: 演示文稿没有幻灯片"
        if slide_arg in ("all", "*", "全部"):
            target_slides = slides
        else:
            try:
                idx = int(slide_arg)
            except ValueError:
                return f"[Office Error]: slide 需为页码（1-{len(slides)}）或 'all'"
            if not 1 <= idx <= len(slides):
                return f"[Office Error]: slide 页码越界（1-{len(slides)}）"
            target_slides = [slides[idx - 1]]

        from pptx.enum.shapes import PP_PLACEHOLDER
        from pptx.util import Pt as _PptxPt

        touched_runs = 0
        for s in target_slides:
            title_el = s.shapes.title._element if s.shapes.title is not None else None
            for shape in s.shapes:
                if not shape.has_text_frame:
                    continue
                is_title = shape._element is title_el
                if not is_title and shape.is_placeholder:
                    try:
                        ph_type = shape.placeholder_format.type
                    except Exception:
                        ph_type = None
                    if ph_type in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE):
                        is_title = True
                if target == "title" and not is_title:
                    continue
                if target == "body" and is_title:
                    continue
                for para in shape.text_frame.paragraphs:
                    for run in para.runs:
                        if bold is not None:
                            run.font.bold = bold
                        if italic is not None:
                            run.font.italic = italic
                        if underline is not None:
                            run.font.underline = underline
                        if font:
                            run.font.name = font
                            self._apply_pptx_ea_font(run, font)
                        if size is not None:
                            run.font.size = _PptxPt(size)
                        if color is not None:
                            from pptx.dml.color import RGBColor as _PptxRGB
                            run.font.color.rgb = _PptxRGB(color[0], color[1], color[2])
                        touched_runs += 1

        if touched_runs == 0:
            return "[Office Error]: 未找到匹配的文字内容"

        try:
            prs.save(resolved)
        except Exception as exc:
            return f"[Office Error]: 保存演示文稿失败: {exc}"
        scope = (f"第 {slide_arg} 页" if slide_arg not in ("all", "*", "全部")
                 else f"{len(target_slides)} 页")
        return (f"[Office OK]: 已格式化 {scope} / {touched_runs} 个文字片段"
                f"（target={target}）→ {resolved}")

    # ------------------------------------------------------------ pdf

    def _pdf_create(self, args: Dict[str, Any]) -> str:
        content = args.get("content", "")
        filename = _safe_filename(args.get("filename", "文档.pdf"))
        title = args.get("title", "")

        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"

        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.lib.units import mm
            from reportlab.platypus import (
                Paragraph, SimpleDocTemplate, Spacer,
                ListFlowable, ListItem, Preformatted,
            )
            from reportlab.lib.enums import TA_CENTER
        except ImportError:
            return (
                "[Office Tools] 需要安装 reportlab 才能使用 pdf_create 工具。\n"
                "请运行: pip install reportlab"
            )

        out_dir = _ensure_output_dir(self.workspace)
        filepath = os.path.join(out_dir, filename)

        doc = SimpleDocTemplate(filepath, pagesize=A4,
                                topMargin=20*mm, bottomMargin=20*mm,
                                leftMargin=20*mm, rightMargin=20*mm)
        styles = getSampleStyleSheet()
        story: List = []

        if title:
            title_style = ParagraphStyle(
                "Title1", parent=styles["Title"],
                fontSize=24, spaceAfter=12*mm,
                alignment=TA_CENTER,
            )
            story.append(Paragraph(title, title_style))
            story.append(Spacer(1, 6*mm))

        lines = content.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i]

            if not line.strip():
                story.append(Spacer(1, 3*mm))
                i += 1
                continue

            # 标题
            hm = re.match(r"^(#{1,5})\s+(.+)$", line)
            if hm:
                level = len(hm.group(1))
                text = hm.group(2).strip()
                sz = [22, 18, 15, 13, 11][min(level - 1, 4)]
                h_style = ParagraphStyle(
                    f"Heading{level}", parent=styles["Heading1"],
                    fontSize=sz, spaceBefore=6*mm, spaceAfter=3*mm,
                )
                story.append(Paragraph(text, h_style))
                i += 1
                continue

            # 代码块
            cb_match = re.match(r"^```[ \t]*([A-Za-z0-9_+-]*)[ \t]*$", line.strip())
            if cb_match:
                lang = (cb_match.group(1) or "").lower()
                code_lines = []
                i += 1
                while i < len(lines) and not lines[i].strip().startswith("```"):
                    code_lines.append(lines[i])
                    i += 1
                code_text = "\n".join(code_lines)
                i += 1  # 跳过闭合 ```
                # 图表代码块 → 渲染成图片嵌入（失败回退源码文本，保证不丢）
                if lang in ("plantuml", "puml", "mermaid", "mmd") and code_text.strip():
                    img_abs = self._render_diagram_block(lang, code_text, i)
                    if img_abs:
                        try:
                            from reportlab.platypus import Image as RLImage
                            from reportlab.lib.units import mm as _mm
                            from PIL import Image as _PILImage
                            with _PILImage.open(img_abs) as im:
                                w_px, h_px = im.size
                            max_w = 160 * _mm  # A4 内容宽
                            scale = min(max_w / w_px, 1.0) if w_px else 1.0
                            story.append(RLImage(img_abs, width=w_px * scale, height=h_px * scale))
                            continue
                        except Exception:
                            pass  # 图片嵌入失败 → 回退源码文本
                code_style = ParagraphStyle(
                    "Code", parent=styles["Code"],
                    fontSize=8, leading=10,
                    leftIndent=6*mm, spaceAfter=3*mm,
                )
                story.append(Preformatted(code_text, code_style))
                continue

            # 无序列表
            if re.match(r"^[\s]*[-*+]\s+", line):
                text = re.sub(r"^[\s]*[-*+]\s+", "", line)
                p = Paragraph(text, styles["Normal"])
                story.append(ListFlowable([ListItem(p)], bulletType="bullet",
                                           leftIndent=30, bulletOffsetY=-2))
                i += 1
                continue

            # 有序列表
            if re.match(r"^\s*\d+[\.\)]\s+", line):
                text = re.sub(r"^\s*\d+[\.\)]\s+", "", line)
                p = Paragraph(text, styles["Normal"])
                story.append(ListFlowable([ListItem(p)], bulletType="1",
                                           leftIndent=30, bulletOffsetY=-2))
                i += 1
                continue

            # 普通段落
            p = Paragraph(line, styles["Normal"])
            story.append(p)
            i += 1

        doc.build(story)
        return f"[Office OK]: 已生成 PDF 文档 → {filepath}"

    # ------------------------------------------------------------ 数据分析

    def _data_analyze(self, args: Dict[str, Any]) -> str:
        if not _HAS_PANDAS:
            return _missing_dep_msg("pandas", "data_analyze")

        data_str = args.get("data", "")
        instructions = args.get("instructions", "").strip()
        output_format = args.get("output_format", "text")

        df: Any = None

        # 优先：文件路径直读（.xlsx/.xls/.csv/.json，含用户上传的 素材/ 文件）
        file_path = str(args.get("path", "") or "").strip()
        if file_path:
            resolved = os.path.abspath(
                file_path if os.path.isabs(file_path) else os.path.join(self.workspace, file_path)
            )
            if not (resolved == self.workspace or resolved.startswith(self.workspace + os.sep)):
                return "[Office Error]: 路径越界：仅支持读取工作区内的文件"
            if not os.path.isfile(resolved):
                return f"[Office Error]: 文件不存在: {file_path}"
            ext = os.path.splitext(resolved)[1].lower()
            sheet = args.get("sheet") or 0
            try:
                if ext in (".xlsx", ".xls"):
                    if not _HAS_OPENPYXL and ext == ".xlsx":
                        return _missing_dep_msg("openpyxl", "data_analyze（xlsx）")
                    df = _pd.read_excel(resolved, sheet_name=sheet)
                elif ext == ".csv":
                    df = _pd.read_csv(resolved)
                elif ext == ".json":
                    df = _pd.read_json(resolved)
                else:
                    return f"[Office Error]: 不支持的文件类型 {ext}（支持 .xlsx/.xls/.csv/.json）"
            except Exception as exc:
                return f"[Office Error]: 读取文件失败: {exc}"

        data_str = args.get("data", "")

        # 尝试解析为 JSON
        if df is None and data_str:
            try:
                data_json = json.loads(data_str)
                if isinstance(data_json, list):
                    df = _pd.DataFrame(data_json)
                elif isinstance(data_json, dict):
                    df = _pd.DataFrame([data_json])
            except (json.JSONDecodeError, ValueError):
                pass

        # 尝试解析为 CSV
        if df is None and data_str:
            try:
                df = _pd.read_csv(io.StringIO(data_str))
            except Exception:
                pass

        if df is None:
            return "[Office Error]: 无法解析数据。请提供 path（文件路径）或 data（CSV 文本/JSON 数组）。"

        result_lines: List[str] = []
        result_lines.append(f"📊 数据分析结果")
        result_lines.append(f"数据形状: {df.shape[0]} 行 × {df.shape[1]} 列")
        result_lines.append(f"列名: {', '.join(str(c) for c in df.columns)}")
        result_lines.append("")

        # 根据指令执行分析
        instr_lower = instructions.lower()

        if any(kw in instr_lower for kw in ["描述", "概览", "统计", "summary", "describe"]):
            result_lines.append("--- 描述性统计 ---")
            desc = df.describe(include="all").to_string()
            result_lines.append(desc)

        if any(kw in instr_lower for kw in ["分组", "group", "agg", "聚合"]):
            result_lines.append("")
            result_lines.append("--- 分组统计 ---")
            # 尝试按第一列分组
            cat_cols = df.select_dtypes(include=["object"]).columns.tolist()
            num_cols = df.select_dtypes(include=["number"]).columns.tolist()
            if cat_cols and num_cols:
                for cc in cat_cols[:2]:
                    for nc in num_cols[:3]:
                        try:
                            grouped = df.groupby(cc)[nc].agg(["mean", "sum", "count"])
                            result_lines.append(f"\n按 {cc} 分组 · {nc} :")
                            result_lines.append(grouped.to_string())
                        except Exception:
                            pass

        if any(kw in instr_lower for kw in ["排序", "sort", "top", "最大的"]):
            result_lines.append("")
            result_lines.append("--- 排序结果 ---")
            num_cols = df.select_dtypes(include=["number"]).columns.tolist()
            if num_cols:
                sorted_df = df.sort_values(by=num_cols[0], ascending=False)
                result_lines.append(sorted_df.head(20).to_string())

        if any(kw in instr_lower for kw in ["空值", "null", "缺失", "missing"]):
            result_lines.append("")
            result_lines.append("--- 缺失值统计 ---")
            null_counts = df.isnull().sum()
            result_lines.append(null_counts.to_string())

        if any(kw in instr_lower for kw in ["相关", "corr", "correlation"]):
            result_lines.append("")
            result_lines.append("--- 相关系数矩阵 ---")
            num_df = df.select_dtypes(include=["number"])
            if num_df.shape[1] >= 2:
                result_lines.append(num_df.corr().to_string())

        if not result_lines[2:]:
            # 默认：显示前几行 + 简单统计
            result_lines.append("--- 前 10 行数据 ---")
            result_lines.append(df.head(10).to_string())
            result_lines.append("")
            result_lines.append("--- 数值列统计 ---")
            num_df = df.select_dtypes(include=["number"])
            if not num_df.empty:
                result_lines.append(num_df.describe().to_string())

        if output_format == "xlsx" and _HAS_OPENPYXL:
            # 输出为 Excel 文件
            filename = "数据分析结果.xlsx"
            out_dir = _ensure_output_dir(self.workspace)
            filepath = os.path.join(out_dir, filename)
            df.to_excel(filepath, index=False, engine="openpyxl")
            result_lines.append(f"\n[Office OK]: 已导出 Excel → {filepath}")
        elif output_format == "json":
            return json.dumps(json.loads(df.head(100).to_json(orient="records")),
                              ensure_ascii=False, indent=2)

        return "\n".join(result_lines)

    # ------------------------------------------------------------ 图表

    def _chart_make(self, args: Dict[str, Any]) -> str:
        if not _HAS_MATPLOTLIB:
            return _missing_dep_msg("matplotlib", "chart_make（需同时安装 pandas）")

        data_str = args.get("data", "")
        chart_type = args.get("chart_type", "bar")
        title = args.get("title", "")
        x_label = args.get("x_label", "")
        y_label = args.get("y_label", "")
        filename = _safe_filename(args.get("filename", "chart.png"))

        if not filename.lower().endswith((".png", ".jpg", ".jpeg", ".svg")):
            filename += ".png"

        try:
            data = json.loads(data_str)
        except json.JSONDecodeError as e:
            return f"[Office Error]: data 不是有效的 JSON: {e}"

        _plt.rcParams["figure.dpi"] = 120
        _plt.rcParams["font.size"] = 11
        _plt.rcParams["axes.unicode_minus"] = False

        # 中文字体适配：按平台选常见中文字体，避免标签显示为方框
        if not self._cjk_font_applied:
            for font in ("Microsoft YaHei", "SimHei", "PingFang SC",
                         "Noto Sans CJK SC", "WenQuanYi Micro Hei", "sans-serif"):
                try:
                    from matplotlib import font_manager
                    matches = font_manager.findfont(
                        font_manager.FontProperties(family=font), fallback_to_default=False
                    )
                    if matches:
                        _plt.rcParams["font.family"] = font
                        break
                except Exception:
                    continue
            self._cjk_font_applied = True

        fig, ax = _plt.subplots(figsize=(10, 6))

        labels = data.get("labels", [])
        datasets = data.get("datasets", [])

        if datasets:
            # 多系列
            for ds in datasets:
                ds_label = ds.get("label", "")
                values = ds.get("values", [])
                if chart_type == "bar":
                    ax.bar(labels, values, label=ds_label, alpha=0.8)
                elif chart_type == "barh":
                    ax.barh(labels, values, label=ds_label, alpha=0.8)
                elif chart_type == "line":
                    ax.plot(labels, values, marker="o", label=ds_label, linewidth=2)
                elif chart_type == "scatter":
                    ax.scatter(labels, values, label=ds_label, s=50, alpha=0.7)
                elif chart_type == "pie":
                    # 饼图只取第一个数据集
                    if ds == datasets[0]:
                        ax.pie(values, labels=labels, autopct="%1.1f%%")
                    break
            if chart_type != "pie":
                ax.legend()
        else:
            # 单系列
            values = data.get("values", [])
            if chart_type == "bar":
                ax.bar(labels, values, color="#4f8cff", alpha=0.8)
            elif chart_type == "barh":
                ax.barh(labels, values, color="#4f8cff", alpha=0.8)
            elif chart_type == "line":
                ax.plot(labels, values, marker="o", color="#4f8cff", linewidth=2)
            elif chart_type == "scatter":
                ax.scatter(labels, values, color="#4f8cff", s=50, alpha=0.7)
            elif chart_type == "pie":
                ax.pie(values, labels=labels, autopct="%1.1f%%")

        if title:
            ax.set_title(title, fontsize=14, fontweight="bold")
        if x_label and chart_type not in ("pie",):
            ax.set_xlabel(x_label)
        if y_label and chart_type not in ("pie",):
            ax.set_ylabel(y_label)

        if chart_type not in ("pie",):
            _plt.xticks(rotation=30, ha="right")
        _plt.tight_layout()

        out_dir = _ensure_output_dir(self.workspace)
        filepath = os.path.join(out_dir, filename)
        _plt.savefig(filepath, bbox_inches="tight")
        _plt.close(fig)

        return f"[Office OK]: 已生成图表 → {filepath}"

    # ------------------------------------------------------------ 读取已有办公文件

    def _docx_read(self, args: Dict[str, Any]) -> str:
        if not _HAS_DOCX:
            return _missing_dep_msg("python-docx", "docx_read")
        rel_path = str(args.get("path") or "").strip()
        if not rel_path:
            return "[Office Error]: 缺少 path 参数"
        try:
            resolved = self._resolve_path(rel_path)
        except ValueError as exc:
            return f"[Office Error]: {exc}"
        try:
            doc = _DocxDoc(resolved)
        except Exception as exc:
            return f"[Office Error]: 无法打开文档: {exc}"
        parts = []
        for p in doc.paragraphs:
            if p.text.strip():
                style = (p.style.name or "").lower()
                prefix = "#" * min(4, 1 + sum(1 for c in style if c.isdigit() and c != "0")) \
                    if "heading" in style else ""
                parts.append(f"{prefix} {p.text.strip()}".strip())
        for ti, table in enumerate(doc.tables):
            parts.append(f"\n[表格 {ti + 1}]")
            for row in table.rows[:50]:
                cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                parts.append("| " + " | ".join(cells) + " |")
        text = "\n\n".join(parts)
        return f"[Office OK]: 已读取 {os.path.basename(resolved)} ({len(text)} 字符)\n\n{text[:20000]}"

    def _xlsx_read(self, args: Dict[str, Any]) -> str:
        if not _HAS_OPENPYXL:
            return _missing_dep_msg("openpyxl", "xlsx_read")
        rel_path = str(args.get("path") or "").strip()
        if not rel_path:
            return "[Office Error]: 缺少 path 参数"
        try:
            resolved = self._resolve_path(rel_path)
        except ValueError as exc:
            return f"[Office Error]: {exc}"
        sheet_name = str(args.get("sheet") or "").strip() or None
        max_rows = max(1, min(500, int(args.get("max_rows") or 100)))
        try:
            wb = _openpyxl.load_workbook(resolved, read_only=True, data_only=True)
        except Exception as exc:
            return f"[Office Error]: 无法打开 Excel: {exc}"
        if sheet_name:
            if sheet_name not in wb.sheetnames:
                return f"[Office Error]: sheet {sheet_name!r} 不存在（可选: {', '.join(wb.sheetnames)}）"
            sheets = [sheet_name]
        else:
            sheets = wb.sheetnames[:3]
        parts = []
        for sn in sheets:
            ws = wb[sn]
            rows = []
            for row in ws.iter_rows(max_row=max_rows, max_col=50, values_only=True):
                cells = ["" if v is None else str(v) for v in row]
                while cells and cells[-1] == "":
                    cells.pop()
                rows.append(cells)
            parts.append(f"=== Sheet: {sn} ({len(rows)} 行) ===")
            for r in rows[:max_rows]:
                parts.append("| " + " | ".join(r) + " |")
        return f"[Office OK]: 已读取 {os.path.basename(resolved)}\n\n" + "\n\n".join(parts)

    def _pptx_read(self, args: Dict[str, Any]) -> str:
        if not _HAS_PPTX:
            return _missing_dep_msg("python-pptx", "pptx_read")
        rel_path = str(args.get("path") or "").strip()
        if not rel_path:
            return "[Office Error]: 缺少 path 参数"
        try:
            resolved = self._resolve_path(rel_path)
        except ValueError as exc:
            return f"[Office Error]: {exc}"
        try:
            prs = _PptxPresentation(resolved)
        except Exception as exc:
            return f"[Office Error]: 无法打开演示文稿: {exc}"
        parts = []
        for i, slide in enumerate(prs.slides, 1):
            title, bullets = "", []
            for shape in slide.shapes:
                if not shape.has_text_frame:
                    continue
                tf = shape.text_frame
                is_title = shape == getattr(slide.shapes, "title", None)
                for para in tf.paragraphs:
                    t = "".join(run.text for run in para.runs).strip()
                    if not t:
                        continue
                    if is_title and not title:
                        title = t
                    else:
                        bullets.append(t)
            parts.append(f"--- 第 {i} 页 ---")
            if title:
                parts.append(f"标题: {title}")
            for b in bullets[:20]:
                parts.append(f"  · {b}")
        return f"[Office OK]: 已读取 {os.path.basename(resolved)} ({len(prs.slides)} 页)\n\n" + "\n".join(parts)

    def _pdf_read(self, args: Dict[str, Any]) -> str:
        if not _HAS_PYPDF:
            return "[Office Tools] 需要安装 pypdf 才能使用 pdf_read 工具。\n请运行: pip install pypdf"
        rel_path = str(args.get("path") or "").strip()
        if not rel_path:
            return "[Office Error]: 缺少 path 参数"
        try:
            resolved = self._resolve_path(rel_path)
        except ValueError as exc:
            return f"[Office Error]: {exc}"
        max_pages = max(1, min(200, int(args.get("max_pages") or 50)))
        try:
            reader = _pypdf.PdfReader(resolved)
        except Exception as exc:
            return f"[Office Error]: 无法打开 PDF: {exc}"
        total = len(reader.pages)
        parts = [f"PDF 共 {total} 页，读取前 {min(max_pages, total)} 页"]
        for i in range(min(max_pages, total)):
            text = reader.pages[i].extract_text() or ""
            text = re.sub(r"[ \t\r\f\v]+", " ", text).strip()
            if text:
                parts.append(f"\n--- 第 {i + 1} 页 ---\n{text[:2000]}")
        return "\n".join(parts)

    # ------------------------------------------------------------ 路径安全辅助

    def _resolve_path(self, rel_path: str) -> str:
        """解析相对路径为绝对路径，校验越界。返回绝对路径，失败抛 ValueError。"""
        resolved = os.path.abspath(
            rel_path if os.path.isabs(rel_path) else os.path.join(self.workspace, rel_path)
        )
        if not (resolved == self.workspace or resolved.startswith(self.workspace + os.sep)):
            raise ValueError(f"路径越界：仅支持工作区内的文件: {rel_path}")
        if not os.path.isfile(resolved):
            raise ValueError(f"文件不存在: {rel_path}")
        return resolved
