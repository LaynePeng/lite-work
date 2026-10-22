# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""PPT 模板库：把 ppt-master 的版式/品牌/风格资产包成可列举、可套用的接口。

三类资产的形态差异很大，不能一视同仁：

- **layouts/**（9 套，共 20+ 页 SVG）：真正的可填模板。每页带
  `{{TITLE}}` / `{{PAGE_TITLE}}` / `{{CONTENT_AREA}}` 之类占位符，
  填充后即可导出 pptx——这是「选模板 → 出稿」的主路径。
- **brands/**（23 个）：只有 `brands_index.json` 里的主色 + 一段适用场景，
  没有现成页面。作用是给生成结果定色板，属于「参考」而非「模板」。
- **styles/**（16 个）：`design_spec.md` 设计方法说明，是给模型读的
  写作/排版规范，不参与机械填充。

因此本模块只对 layouts 做机械填充，brands/styles 作为元数据提供给上层
（前端选色、Agent 写提示词），避免把「规范文档」硬当成模板用。
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("litework.tools.ppt_templates")

# 版式套件 → 中文展示名（前端直接显示）
LAYOUT_LABELS: Dict[str, str] = {
    "presentation_core": "通用演示（16:9，20 页）",
    "presentation_core_43": "通用演示（4:3）",
    "editorial_bleed": "杂志内页（出血大图）",
    "report_core": "报告体",
    "moments_square": "方图（社交媒体）",
    "story_vertical": "竖版故事（手机）",
    "xiaohongshu_post": "小红书笔记",
}

# 页面 slug → 中文语义（供「按内容选版式」与前端展示）
PAGE_LABELS: Dict[str, str] = {
    "title_slide": "封面",
    "title_content": "标题 + 内容",
    "section_header": "章节分隔",
    "two_content": "双栏内容",
    "comparison": "对比",
    "title_only": "仅标题",
    "blank": "空白",
    "content_caption": "内容 + 图注",
    "picture_caption": "图片 + 图注",
    "hero_statement": "观点金句",
    "editorial_split": "分栏编排",
    "three_card": "三卡片",
    "kpi_dashboard": "KPI 看板",
    "process_timeline": "流程时间线",
    "data_story": "数据叙事",
    "title_picture": "标题 + 图",
    "two_picture_caption": "双图 + 图注",
    "screenshot_focus": "截图聚焦",
    "chart_insight": "图表洞察",
    "table_summary": "表格汇总",
}

_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")
# 占位符槽的 bounds：`<g data-pptx-bounds="x y w h">...{{KEY}}...</g>`
# 必须在 _LAYOUT_META_RE 剥离前抓取——它给出「这段文字能用多宽多高」，
# 是自动折行与字号缩排的唯一依据（模板本身就是固定画布）。
_SLOT_BOUNDS_RE = re.compile(
    r'<g\b[^>]*data-pptx-bounds="(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)"'
    r'[^>]*>(.*?)</g>', re.S)
# 中文字宽≈字号、西文≈字号×0.55；折行估算用保守系数，宁可早断不要溢出
_CJK_RATIO = 1.0
_ASCII_RATIO = 0.55
_LATIN_EXTRA = 0.12          # 西文词间空格余量
# 字号阶梯：放不下时按这些档位下调（模板原始字号是其上限）
_FONT_LADDER = (0.86, 0.75, 0.66, 0.58, 0.5)


#: 字号设计尺度（9 档）：模板里 31 种零散 font-size 归一到这张表。
#: 视觉上 2px 内差异人眼几乎不可分——收敛统一观感，也让 spec_lock 的
#: typography 声明可控（缩排引擎产生的中间值也吸到档位上）。
_TYPE_SCALE = (12, 14, 16, 18, 22, 28, 36, 54, 72)

# ---------------------------------------------------------------- CJK 断行禁则
# 规则摘自上游 ppt-master v6.7.2 的 scripts/text_measure.py（MIT）。
# 只复刻断行策略常量，不引入它的 uharfbuzz 依赖。

#: 不得出现在行首的闭合标点（中西文）
_CLOSING_PUNCTUATION = frozenset(',.;:!?)]}、，。；：！？）》」』】"\'،؛؟')
#: 不得留在行尾的开括号
_OPENING_PUNCTUATION = frozenset('([{（《「『【"\'')
#: 语义停顿：优先在这里换行
_PREFERRED_BREAK_PUNCTUATION = frozenset('，。；：')
#: 优先断点须达到贪心行的这个比例，否则宁可贪心填满（避免明显短行）
_PREFERRED_BREAK_MIN_FILL = 0.75
#: 紧贴数字不宜拆开的单位符号（71% 不能断成 71 / %）
_NUMBER_SUFFIXES = frozenset("%‰°")
#: 日文行首禁则追加（小假名、长音符、叠字符、中点）
_NO_LINE_START = _CLOSING_PUNCTUATION | frozenset(
    "ぁぃぅぇぉっゃゅょゎゕゖァィゥェォッャュョヮヵヶー々ゝゞヽヾ・")

#: 深色主题色板映射（C3）：模板浅色 → 深色变体。
#: 键值都是模板里实际出现的 fill/stroke 色（见配色收敛统计）——
#: 只做等值替换，不动布局，保证版式不漂移。
_DARK_PALETTE: Dict[str, str] = {
    "#FFFFFF": "#0F172A",   # 页面底：白 → 深板岩
    "#F8FAFC": "#1E293B",   # 面板浅 → 深面板
    "#F4F6F8": "#16203A",   # 面板 → 更深一档
    "#EEF2F6": "#1B2740",
    "#E2E8F0": "#24304A",   # 表头/分隔 → 深色分隔
    "#D6DCE3": "#33415E",   # 边框
    "#CBD5E1": "#3D4C6B",   # 强调条/轴线
    "#94A3B8": "#8FA3BF",   # 次级文字（保持可读，不降对比）
    "#64748B": "#A5B4CB",   # 正文灰 → 提亮
    "#475569": "#C3CFE0",   # 正文深灰 → 再提亮
    "#334155": "#D7E0EC",
    "#1E293B": "#EDF2F8",   # 标题深色 → 反白
    "#0F172A": "#F5F8FC",
}
# flat 模式禁止的 Layout/Master/占位符元数据（导出前须剥离，否则质检报错）
_LAYOUT_META_RE = re.compile(
    r'\s+data-pptx-(?:master|master-name|layout|layout-name|layer|carrier|idx'
    r'|editable|placeholder|bounds|slot)="[^"]*"'
)
# 只包一个 <text> 的 <g> 会让导出变成「组合框」，文字进不去——直接展开
_SINGLE_TEXT_GROUP_RE = re.compile(r"<g\b[^>]*>\s*(<text\b.*?</text>)\s*</g>", re.S)

# 图片位：`<g data-pptx-placeholder="picture" ...>` 里包一个 <image>
_IMAGE_SLOT_RE = re.compile(
    r'<g\b[^>]*data-pptx-placeholder="picture"[^>]*>.*?</g>', re.S)
_IMAGE_TAG_RE = re.compile(r"<image\b[^>]*/?>", re.S)
_IMAGE_ID_RE = re.compile(r'id="([^"]+)"')
# 图片槽在 values 里的键：用「IMAGE_n」表示第 n 个图位（1 起）
_IMAGE_KEY_RE = re.compile(r"^IMAGE_(\d+)$")

def _skill_templates_dir() -> Optional[str]:
    from .ppt_edit import _skill_dir

    try:
        base = os.path.join(_skill_dir(), "templates")
    except Exception:
        return None
    return base if os.path.isdir(base) else None


# ---------------------------------------------------------------- 可用性探测


def availability() -> Dict[str, Any]:
    """PPT 模板能力是否可用（给前端提示安装/修复用）。

    为什么要单独探测：`list_layouts()` 在技能缺失时返回空列表——前端据此
    只能得出「没有模板」的结论，于是「从模板做 PPT」永远卡在加载态、
    用户完全不知道是少了 ppt-master。这里把「技能目录在哪、缺的是什么、
    该怎么修」显式说出来。

    三个条件缺一不可：
    - 技能目录存在（`skills/ppt-master`，随安装包分发）；
    - `templates/layouts` 有内容（本文件负责的模板资产）；
    - 转换脚本存在（`scripts/pptx_to_svg.py`，填充/导出都要跑它）。
    """
    info: Dict[str, Any] = {
        "ready": False,
        "skill_dir": None,
        "layouts": 0,
        "scripts": False,
        "reason": "",
        "hint": "",
    }
    from .ppt_edit import _skill_dir

    try:
        skill = _skill_dir()
    except Exception as exc:                       # 典型：未随包分发 / 被删
        info["reason"] = str(exc)
        info["hint"] = (
            "ppt-master 是随安装包分发的内置技能，不需要单独安装。"
            "请确认安装完整（打包态检查 resources/skills/ppt-master），"
            "开发态确认仓库根 skills/ppt-master 存在；修复后重新打开本页签。"
        )
        return info

    info["skill_dir"] = skill
    info["scripts"] = os.path.isfile(os.path.join(skill, "scripts", "pptx_to_svg.py"))
    base = _skill_templates_dir()
    if base:
        try:
            info["layouts"] = len(list_layouts())
        except Exception:
            info["layouts"] = 0

    if not info["scripts"]:
        info["reason"] = "ppt-master 技能里缺少转换脚本 scripts/ppt_to_svg.py 家族"
        info["hint"] = "安装包不完整（技能脚本缺失），请重新安装 lite-work。"
        return info
    if not base or not info["layouts"]:
        info["reason"] = "ppt-master 技能里没有可用模板（templates/layouts 为空）"
        info["hint"] = "安装包不完整（模板资产缺失），请重新安装 lite-work。"
        return info

    info["ready"] = True
    return info


def _load_index(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------- 资产列举


def list_layouts() -> List[Dict[str, Any]]:
    """列出所有版式套件及其页面。"""
    base = _skill_templates_dir()
    if not base:
        return []
    root = os.path.join(base, "layouts")
    index = _load_index(os.path.join(root, "layouts_index.json"))
    out: List[Dict[str, Any]] = []
    for suite in sorted(os.listdir(root)):
        sdir = os.path.join(root, suite, "templates")
        if not os.path.isdir(sdir):
            continue
        pages: List[Dict[str, Any]] = []
        for name in sorted(os.listdir(sdir)):
            if not name.endswith(".svg"):
                continue
            slug = name[:-4].split("_", 1)[-1]           # 01_title_slide → title_slide
            try:
                with open(os.path.join(sdir, name), "r", encoding="utf-8") as fh:
                    content = fh.read()
            except OSError:
                continue
            pages.append({
                "file": name,
                "slug": slug,
                "label": PAGE_LABELS.get(slug, slug),
                "placeholders": sorted(set(_PLACEHOLDER_RE.findall(content))),
                # 图片位：这些 g 里是 <image>，填文字无效，要用 images 传图
                "image_slots": _image_slots(content),
            })
        meta = index.get(suite) or {}
        out.append({
            "suite": suite,
            "label": LAYOUT_LABELS.get(suite, suite),
            "summary": meta.get("summary", ""),
            "canvas_format": meta.get("canvas_format", "ppt169"),
            "page_count": len(pages),
            "pages": pages,
        })
    return out


def list_brands() -> List[Dict[str, Any]]:
    """列出品牌配色（仅供定调，不是可填模板）。"""
    base = _skill_templates_dir()
    if not base:
        return []
    root = os.path.join(base, "brands")
    index = _load_index(os.path.join(root, "brands_index.json"))
    out = []
    for key, meta in sorted(index.items()):
        if not isinstance(meta, dict):
            continue
        out.append({
            "key": key,
            "label": key.replace("-", " ").title(),
            "summary": meta.get("summary", ""),
            "primary_color": meta.get("primary_color", ""),
        })
    return out


def list_styles() -> List[Dict[str, Any]]:
    """列出设计风格（写作/排版规范，交给 Agent 作为上下文）。"""
    base = _skill_templates_dir()
    if not base:
        return []
    root = os.path.join(base, "styles")
    index = _load_index(os.path.join(root, "styles_index.json"))
    out = []
    for key, meta in sorted(index.items()):
        if not isinstance(meta, dict):
            continue
        out.append({
            "key": key,
            "label": key.replace("-", " ").replace("_", " ").title(),
            "summary": meta.get("summary", ""),
            "keywords": meta.get("keywords", []),
        })
    return out


# ---------------------------------------------------------------- 套用模板


def resolve_page_file(suite: str, want: str) -> str:
    """把用户/模型给的页标识解析成模板文件名。

    接受三种写法：完整文件名（01_title_slide.svg）、带序号的 stem
    （01_title_slide）、以及只写语义 slug（title_slide）。定位逻辑放在
    模板模块（而不是 ppt_edit）里，因为规划/生成两条路都要用它。
    """
    lay = next((x for x in list_layouts() if x["suite"] == suite), None)
    if lay is None:
        raise FileNotFoundError(f"未知版式套件 {suite}（先用 op=templates 查看）")
    files = [p["file"] for p in lay["pages"]]
    if want in files:
        return want
    for f in files:
        stem = f[:-4]
        slug = stem.split("_", 1)[-1]
        if want in (stem, slug):
            return f
    raise FileNotFoundError(
        f"套件 {suite} 没有页面 {want}；可用：{', '.join(files[:6])} …")


def describe_suite(suite: str) -> Dict[str, Any]:
    """套件的可填充说明（给模型规划版式用）。

    `image_slots` 与 `placeholders` 分开列：前者是**图片位**（`data-pptx-placeholder="picture"`），
    填法是给 `images` 传路径，不是给 `values` 传文本——两条通道混在一起会让
    模型以为能往图片位塞文字。
    """
    lay = next((x for x in list_layouts() if x["suite"] == suite), None)
    if lay is None:
        raise FileNotFoundError(f"未知版式套件 {suite}")
    return {
        "suite": lay["suite"],
        "label": lay["label"],
        "summary": lay.get("summary", ""),
        "canvas_format": lay.get("canvas_format", "ppt169"),
        "pages": [
            {"page": p["slug"], "label": p["label"], "placeholders": p["placeholders"],
             "image_slots": p.get("image_slots", [])}
            for p in lay["pages"]
        ],
    }


def prepare_page_svg(suite: str, page_file: str) -> Tuple[str, List[str]]:
    """取一页模板并清理成 flat 可导出形态，返回 (svg, 占位符列表)。

    清理（剥 Layout/Master/占位符元数据）是 flat 导出的硬要求，但那也把
    `data-pptx-bounds` 一起删了——而**排版引擎正需要它**（不知道可用宽高
    就没法折行，长文案会水平溢出导致整份稿子导不出来）。
    因此在这里剥离前先抓一份 bounds，按「已清理 svg」为键缓存，
    `fill_page_svg` 再按同一个 svg 取回。
    """
    base = _skill_templates_dir()
    if not base:
        raise FileNotFoundError("找不到内置模板库")
    # 防目录穿越：只允许简单名字
    if not re.fullmatch(r"[\w.-]+", suite) or not re.fullmatch(r"[\w.-]+", page_file):
        raise ValueError("模板名非法")
    path = os.path.join(base, "layouts", suite, "templates", page_file)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"模板不存在：{suite}/{page_file}")
    with open(path, "r", encoding="utf-8") as fh:
        svg = fh.read()
    placeholders = sorted(set(_PLACEHOLDER_RE.findall(svg)))
    bounds = _slot_bounds(svg)              # 必须在剥离元数据前抓
    # flat 导出模式不接受 Layout/Master 元数据
    svg = _LAYOUT_META_RE.sub("", svg)
    # 单 text 的 g 会导出成组合框，文字进不去
    svg = _SINGLE_TEXT_GROUP_RE.sub(r"\1", svg)
    _BOUNDS_CACHE[svg] = bounds
    if len(_BOUNDS_CACHE) > 256:            # 防止长时间运行累积
        _BOUNDS_CACHE.pop(next(iter(_BOUNDS_CACHE)))
    return svg, placeholders


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------- 文字排版引擎


def _char_width(ch: str, font_size: float) -> float:
    """单字符估算宽度（px）。

    按 Unicode 范围粗分档而不是装字体度量库：模板正文字号 16–28px，
    差几个像素由折行留的余量吸收。宁可折早一行，也不要溢出画布——
    溢出会被 quality checker 判 blocking，整份稿子直接导不出来。
    """
    o = ord(ch)
    if o < 0x2E80:                       # ASCII / 拉丁
        if ch in "iljI.,'!|:;":
            return font_size * 0.28
        if ch in "mwMW@":
            return font_size * 0.85
        return font_size * _ASCII_RATIO
    if 0xFF00 <= o <= 0xFFEF:            # 全角标点
        return font_size * _CJK_RATIO
    return font_size * _CJK_RATIO        # CJK 及宽字符


def _text_width(text: str, font_size: float) -> float:
    return sum(_char_width(c, font_size) for c in text)


def wrap_text(text: str, max_width: float, font_size: float) -> List[str]:
    """把一段文字按 max_width 折成多行（含 CJK 禁则与语义断点）。

    规则取自上流 ppt-master 的 text_measure.py（v6.7.2），但**不引入它的
    uharfbuzz 依赖**——只复刻断行策略，字宽仍用本模块的估算：

    1. **禁则（_NO_LINE_START）**：闭合标点（，。；：！？）」】""' 等）不得
       出现在行首——直接断字会把「，」挤到下一行开头，是最刺眼的排版瑕疵；
       对应地，开括号（（《「【"'）不得留在行尾；
    2. **优先断点（_PREFERRED_BREAK_PUNCTUATION）**：，。；： 是语义停顿，
       优先在这些位置换行，而不是把词从中间切开；
    3. **回退阈值**：只有当标点断点能让该行达到贪心填充的 75% 以上时才采用，
       否则宁可继续贪心填满——避免为了断在标点而留下明显短行。

    西文仍在空格处断（单词超宽才硬切），`\\n` 视为强制换行。
    """
    if max_width <= 0 or font_size <= 0:
        return [text]
    out: List[str] = []
    for para in text.split("\n"):
        if not para.strip():
            out.append("")
            continue
        out.extend(_wrap_para(para, max_width, font_size))
    return out or [""]


def _wrap_para(para: str, max_width: float, font_size: float) -> List[str]:
    """单段折行：先切成「不可拆单元」（禁则合并），再贪心 + 优先断点。"""
    units = _protected_units(para, max_width, font_size)
    lines: List[str] = []
    start = 0
    while start < len(units):
        # 贪心：尽量多塞单元，同时记住最后一个「优先断点」位置
        ended = start
        preferred: Optional[int] = None
        while ended < len(units):
            candidate = "".join(units[start:ended + 1]).rstrip()
            if _text_width(candidate, font_size) > max_width:
                break
            ended += 1
            if _preferred_break_after(candidate):
                preferred = ended
        if ended >= len(units):
            lines.append("".join(units[start:]).rstrip())
            break
        if ended == start:                  # 单个单元就超宽：独占一行硬切
            lines.append(units[start].rstrip())
            start += 1
            continue
        # 优先断点需达到贪心行的 75%，否则短行太明显
        chosen = ended
        if preferred is not None and preferred > start:
            greedy_w = _text_width(
                "".join(units[start:ended]).rstrip(), font_size)
            pref_w = _text_width(
                "".join(units[start:preferred]).rstrip(), font_size)
            if pref_w >= greedy_w * _PREFERRED_BREAK_MIN_FILL:
                chosen = preferred
        lines.append("".join(units[start:chosen]).rstrip())
        start = chosen
    return lines


def _protected_units(para: str, max_width: float,
                     font_size: float) -> List[str]:
    """把段落切成不可拆单元：词（西文）、字（CJK），并按禁则合并。

    合并规则（同上游）：
    - 闭合标点/小假名等不得在行首 → 并入**前一个**单元；
    - 开括号不得在行尾 → 让**后一个**单元并进来（实现上是看前一单元末字）。
    数字与紧随的单位符号（`71%`）也不拆。
    """
    # 先按空格切词，超宽的词再逐字拆
    raw: List[str] = []
    for chunk in para.split(" "):
        if not chunk:
            continue
        if _text_width(chunk, font_size) <= max_width:
            raw.append(chunk + " ")
        else:
            raw.extend(list(chunk))
            raw.append(" ")
    if not raw:
        return [para]
    units: List[str] = []
    for unit in raw:
        content = unit.lstrip()
        if not content:
            continue
        merged = False
        if units:
            prev_tail = units[-1].rstrip()
            # 本单元以「不可行首字符」开头，或前一单元以「不可行尾字符」结尾
            starts_forbidden = content[0] in _NO_LINE_START
            prev_open = bool(prev_tail) and prev_tail[-1] in _OPENING_PUNCTUATION
            # 数字 + 单位符号（71%）：不拆开
            num_unit = content[0] in _NUMBER_SUFFIXES and prev_tail[-1:].isdigit()
            if starts_forbidden or prev_open or num_unit:
                units[-1] += unit
                merged = True
        if not merged:
            units.append(unit)
    return units


def _preferred_break_after(text: str) -> bool:
    """该行末尾是否落在语义停顿标点上（，。；：）。

    句末的其它闭合标点（如 `）` `」`）先剥掉再判断：`……方案（见附录），`
    的语义停顿在逗号，括号只是夹注。
    """
    tail = text.rstrip()
    while (tail and tail[-1] in _CLOSING_PUNCTUATION
           and tail[-1] not in _PREFERRED_BREAK_PUNCTUATION):
        tail = tail[:-1]
    return bool(tail) and tail[-1] in _PREFERRED_BREAK_PUNCTUATION


def fit_text(
    text: str, width: float, height: float, font_size: float,
) -> Tuple[List[str], float, bool]:
    """让文字塞进 width×height：先折行，放不下就按阶梯降字号。

    返回 (行列表, 实际字号, 是否仍溢出)。

    为什么必须做：模板是**固定画布**，`{{CONTENT_AREA}}` 换成长段落时会
    水平溢出（实测 3784px 塞进 1280px 画布 → 质检判 blocking → 整份稿子
    导不出来）。此前只有用户手动写 `\\n` 才能避免，而 AI 生成的文案
    几乎必然超长。
    """
    if not text:
        return [""], font_size, False
    line_h = 1.5
    size = font_size
    for ratio in (1.0, *(_FONT_LADDER)):
        size = round(font_size * ratio, 1)
        lines = wrap_text(text, width, size)
        if len(lines) * size * line_h <= height or ratio == _FONT_LADDER[-1]:
            overflow = len(lines) * size * line_h > height
            # 吸附到设计档位（12/14/16/18/22/28/…）：缩排产生的中间值
            # （16.5、12.8…）会让整套稿子字号零散
            snapped = float(min(_TYPE_SCALE, key=lambda s: abs(s - size)))
            if snapped <= size or snapped - size <= 2:   # 只向上吸 2px 内
                size = snapped
                lines = wrap_text(text, width, size)
                overflow = len(lines) * size * line_h > height
            return lines, size, overflow
    return wrap_text(text, width, size), size, False

def _slot_bounds(svg: str) -> Dict[str, List[float]]:
    """抓每个占位符的可用区域 {KEY: [x, y, w, h]}（剥离元数据前调用）。"""
    out: Dict[str, List[float]] = {}
    for m in _SLOT_BOUNDS_RE.finditer(svg):
        body = m.group(5) or ""
        for key in _PLACEHOLDER_RE.findall(body):
            if key not in out:      # 同一 KEY 出现多次时取第一处（模板约定唯一）
                out[key] = [float(m.group(i)) for i in range(1, 5)]
    return out


def _image_slots(svg: str) -> List[Dict[str, str]]:
    """列出页面里的图片位（给前端选图 / 给规划提示词说明可放几张图）。"""
    slots: List[Dict[str, str]] = []
    for idx, block in enumerate(_IMAGE_SLOT_RE.finditer(svg), 1):
        m = _IMAGE_ID_RE.search(block.group(0))
        slots.append({
            # 填图时用的键（values/gallery 的键名）
            "key": f"IMAGE_{idx}",
            "label": f"图片位 {idx}",
            "element_id": m.group(1) if m else "",
        })
    return slots


def _image_to_href(src: str, workspace: Optional[str]) -> str:
    """图片值 → 可直接写进 `<image href>` 的字符串。

    三条分支：
    - `data:` URI：原样用；
    - `http(s)://`：原样用（ppt-master 导出阶段下载）；
    - 其余视为文件路径：读字节转 data URI。**必须内联**——导出器在临时
      工作目录里跑，相对路径（如 `hero.png`）解析失败会静默保留 1×1 占位图，
      表现就是「填了图但导出来是空白」（踩过）。
    """
    if src.startswith("data:"):
        return src
    if re.match(r"^https?://", src, re.I):
        return src

    import base64
    import mimetypes

    path = src
    if not os.path.isabs(path):
        if not workspace:
            raise FileNotFoundError(f"图片 {src} 是相对路径，但不知道相对哪个工作区")
        path = os.path.join(workspace, src)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"图片不存在：{src}")
    mime, _ = mimetypes.guess_type(path)
    if not mime or not mime.startswith("image/"):
        raise ValueError(f"不是图片文件：{src}")
    with open(path, "rb") as fh:
        blob = base64.b64encode(fh.read()).decode("ascii")
    return f"data:{mime};base64,{blob}"


def _fill_images(svg: str, values: Dict[str, str],
                 workspace: Optional[str] = None) -> str:
    """把 `IMAGE_n` 值写进第 n 个图片位的 `<image href>`。

    为什么要单独一条通道：模板里的图片位是 `<image>`（带 1×1 透明占位图），
    占位符替换（`{{TEXT}}`）碰不到它——21 个带图版式此前**永远出不了图**。

    按 `<image>` 出现顺序编号，而**不是**匹配 `data-pptx-placeholder="picture"`：
    `prepare_page_svg` 会先剥掉 flat 模式禁止的图片槽元数据（否则质检报错），
    那时按 placeholder 匹配一个都命中不了（踩过）。模板里 `<image>` 与图片槽
    严格一一对应（21 个模板实测），按顺序分配是安全的。

    值支持三种形态：
    - `data:` URI：直接写进 href；
    - 工作区相对/绝对路径：读文件转 data URI（导出时由 ppt-master 落成媒体）；
    - `http(s)://` URL：原样写 href（导出阶段下载）。

    索引从 1 开始，与 `_image_slots()` 的 key 一致。
    """
    wanted: Dict[int, str] = {}
    for key, raw in values.items():
        m = _IMAGE_KEY_RE.match(str(key))
        if m and str(raw).strip():
            wanted[int(m.group(1))] = str(raw).strip()
    if not wanted:
        return svg

    counter = {"n": 0}

    def repl_img(im: re.Match[str]) -> str:
        counter["n"] += 1
        tag = im.group(0)
        src = wanted.get(counter["n"])
        if not src:
            return tag
        href = _image_to_href(src, workspace)
        if re.search(r'\bhref="[^"]*"', tag):
            return re.sub(r'\bhref="[^"]*"', f'href="{_attr_escape(href)}"', tag, count=1)
        return tag.replace("<image", f'<image href="{_attr_escape(href)}"', 1)

    return _IMAGE_TAG_RE.sub(repl_img, svg)


def _attr_escape(text: str) -> str:
    return _xml_escape(text).replace('"', "&quot;")


# ---------------------------------------------------------------- 表格 / 图表数据

#: 模板里的语义载荷：`<metadata type="application/json">{schema:...}</metadata>`
#: 配合 `data-pptx-replace-with="table|chart"`，导出器会把它换成 PowerPoint
#: **原生表格/图表**（可继续编辑，不是贴图）。
_SEMANTIC_META_RE = re.compile(
    r'<metadata\s+type="application/json"\s*>(.*?)</metadata>', re.S)


def _set_meta_json(svg: str, mutate: Any) -> str:
    """把每段语义 metadata JSON 交给 mutate 修改后写回。

    改完必须同时打上 `data-pptx-native-authority="json"`：模板里的
    `data-pptx-fallback-sha256` 是**可见视觉子树**的指纹，我们只换 JSON
    不重画那些 `<rect>/<text>`，哈希必然对不上 → 导出器判定「视觉被手改过」
    → 拒绝 native 转换，退回视觉渲染（外观对、但不是可编辑表格）。
    声明 json 为权威后，导出器直接以 JSON 为准（这是模板作者预留的
    程序化改数据通道）。
    """
    def repl(m: re.Match[str]) -> str:
        raw = m.group(1)
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return m.group(0)
        if not isinstance(data, dict):
            return m.group(0)
        changed = mutate(data)
        if not changed:
            return m.group(0)
        return ('<metadata type="application/json">'
                + json.dumps(data, ensure_ascii=False, separators=(",", ":"))
                + "</metadata>")
    out = _SEMANTIC_META_RE.sub(repl, svg)
    if out == svg:
        return svg
    # 给承载 metadata 的 <g data-pptx-replace-with=...> 打权威标记
    return re.sub(
        r'(<g\b[^>]*data-pptx-replace-with="(?:table|chart)"[^>]*?)(>)',
        lambda m: m.group(1) if "data-pptx-native-authority" in m.group(1)
        else f'{m.group(1)} data-pptx-native-authority="json"{m.group(2)}',
        out,
    )


def inject_table(svg: str, table: Any) -> str:
    """把表格数据写进 templates 的 table 载荷（导出为原生表格）。

    `table` 接受两种形状：
    - `[[表头...], [行1...], ...]`：首行当表头（最常见，AI 也最好写）；
    - `{"columns": [...], "rows": [[...], ...]}`：显式列名。

    为什么必须做：模板里 `table_summary` 的表格槽此前只保留**示例数据**
    （Adoption 68%/Cycle time 12d…），用户传什么都不会进 PPT——页面永远
    显示模板自带的假数据。
    """
    cols: List[str] = []
    rows: List[List[str]] = []
    if isinstance(table, dict):
        cols = [str(c) for c in (table.get("columns") or [])]
        for r in table.get("rows") or []:
            if isinstance(r, (list, tuple)):
                rows.append([str(c) for c in r])
        if not cols and rows:
            cols, rows = rows[0], rows[1:]
    elif isinstance(table, list):
        grid = [[str(c) for c in r] for r in table if isinstance(r, (list, tuple))]
        if grid:
            cols, rows = grid[0], grid[1:]
    if not cols:
        return svg

    def mutate(data: Dict[str, Any]) -> bool:
        if "table" not in str(data.get("schema", "")):
            return False
        data["columns"] = [{"text": c, "bold": True} for c in cols]
        data["rows"] = [
            [{"text": c, "align": "r" if i else "l"} if i else {"text": c}
             for i, c in enumerate(r + [""] * (len(cols) - len(r)))]
            for r in rows
        ]
        # 行高按实际行数重算，否则超出的行会被裁在框外
        n = len(data["rows"]) + 1
        data["row_heights"] = [1] * n
        # 列宽必须与列数一致（质检强制）；均分即可，模板原值只对示例列数有效
        data["column_widths"] = [1] * len(cols)
        return True
    return _set_meta_json(svg, mutate)


def inject_chart(svg: str, chart: Any) -> str:
    """把图表数据写进 templates 的 chart 载荷（导出为原生图表）。

    接受：
    - `{"categories": [...], "series": [{"name": ..., "values": [...]}]}`
    - `{"labels": [...], "values": [...]}`（单系列简写）
    """
    cats: List[str] = []
    series: List[Dict[str, Any]] = []
    if isinstance(chart, dict):
        cats = [str(c) for c in (chart.get("categories") or chart.get("labels") or [])]
        raw_series = chart.get("series")
        if isinstance(raw_series, list):
            for s in raw_series:
                if isinstance(s, dict):
                    vals = [float(v) for v in (s.get("values") or [])]
                    series.append({"name": str(s.get("name") or "数值"), "values": vals})
        elif chart.get("values"):
            series.append({
                "name": str(chart.get("name") or "数值"),
                "values": [float(v) for v in chart["values"]],
            })
    if not cats or not series:
        return svg

    def mutate(data: Dict[str, Any]) -> bool:
        # 图表 payload **没有 schema 字段**（表格有 semantic-table.v2），
        # 只能按形状识别：有 series + categories 且带 style 就是图表槽。
        # 早先按 schema 判断 → 永远不命中 → 用户数据根本进不去（踩过）。
        if not ("series" in data and "categories" in data):
            return False
        data["categories"] = cats
        data["series"] = series
        return True

    return _set_meta_json(svg, mutate)

def apply_dark_theme(svg: str) -> str:
    """浅色模板 → 深色变体（色板等值替换，不动布局）。

    为什么用替换而不是新画模板：模板色板高度收敛（11 个主色占 95%+），
    等值替换能覆盖全部视觉面；且布局/字号/占位符零改动——深浅两版
    严格同构，切换主题不会让已编辑的内容错位。

    踩过的坑：直接顺序替换会**链式覆盖**——`#FFFFFF→#0F172A` 先执行，
    随后 `#0F172A→#F5F8FC` 又把刚换的深色换回浅色（dict 顺序巧合）。
    必须两遍走：先全部换成不冲突的中间占位符，再统一落到目标色。
    """
    out = svg
    # 第一遍：浅色 → 私有占位符（\x00N\x00 不会出现在合法 SVG 里）
    for i, (light, dark) in enumerate(_DARK_PALETTE.items()):
        token = f"\x00{i}\x00"
        out = out.replace(light, token)
        out = out.replace(light.lower(), token)
    # 第二遍：占位符 → 深色
    for i, (_light, dark) in enumerate(_DARK_PALETTE.items()):
        out = out.replace(f"\x00{i}\x00", dark)
    return out

def strip_native_markers(svg: str) -> str:
    """摘掉表格/图表的 native 替换标记（保留视觉渲染）。

    用途：用户选了 table_summary/chart_insight 版式但没给数据时，模板
    自带的示例数据与可见回退配色不一致（模板上游问题），开着
    `--native-charts-and-tables` 会被导出器拒绝。摘掉
    `data-pptx-replace-with` 后该页按普通形状导出——不是可编辑表格，
    但至少出稿不失败；给了数据则走 inject_* + authority=json 正常原生。
    """
    svg = re.sub(r'\s+data-pptx-replace-with="(?:table|chart)"', "", svg)
    # authority 只允许挂在 replace-with 标记上，一起摘（否则质检报
    # "authority is allowed only on ... markers"）
    return re.sub(r'\s+data-pptx-native-authority="json"', "", svg)

def page_slots(suite: str, page_file: str) -> Dict[str, bool]:
    """这一页有没有表格槽 / 图表槽（供规划与前端提示）。"""
    svg, _ = prepare_page_svg(suite, page_file)
    return {
        "table": bool(re.search(r'data-pptx-replace-with="table"', svg)),
        "chart": bool(re.search(r'data-pptx-replace-with="chart"', svg)),
    }


#: 套件/页 → 槽位容量（进程内缓存：每页都要读模板，规划时会被反复查）
_CAPACITY_CACHE: Dict[str, Dict[str, Dict[str, Any]]] = {}


def slot_capacity(suite: str, page_file: str) -> Dict[str, Dict[str, Any]]:
    """单页各槽位的文字容量：{占位符: {chars_per_line, lines, capacity}}。

    数据来自上游 ppt-master v6.7.2 的 `slot_capacity_report`（advisory JSON，
    用估算而非字体 shaping，所以主进程可直接算、不必起子进程）。

    用途：**生成前**就能告诉模型/用户「这个版面放不下这么多字」，
    而不是等生成后被质检拦下来再回查。与 `fit_text` 的自动缩排互补：
    缩排兜底、容量预警前置提醒。

    槽位 id 形如 `title-content-object-slot`，这里按占位符名归并
    （同页多个 object 槽取各自独立条目，用序号区分）。
    """
    key = f"{suite}/{page_file}"
    if key in _CAPACITY_CACHE:
        return _CAPACITY_CACHE[key]
    out: Dict[str, Dict[str, Any]] = {}
    base = _skill_templates_dir()
    if not base:
        return out
    path = os.path.join(base, "layouts", suite, "templates", page_file)
    if not os.path.isfile(path):
        return out
    try:
        import sys as _sys

        from .ppt_edit import _skill_dir

        scripts = os.path.join(_skill_dir(), "scripts")
        if scripts not in _sys.path:
            _sys.path.insert(0, scripts)
        from svg_quality.slot_capacity import slot_capacity_report

        report = slot_capacity_report([__import__("pathlib").Path(path)])
    except Exception:
        return out            # 容量报告是增强项：失败不影响生成
    # slot 报告用的是 data-pptx-placeholder（title / object / picture…），
    # 而 values 的键是 {{KEY}}（PAGE_TITLE / CONTENT_AREA…）——两者名字不通，
    # 必须按 slot id 里的前缀回字形到 KEY（title-content-object-slot ↔ {{CONTENT_AREA}}）。
    key_map = _slot_placeholder_keys(path)
    counter: Dict[str, int] = {}
    for slot in report.get("slots", []):
        ph = str(slot.get("placeholder") or "")
        if not ph:
            continue
        counter[ph] = counter.get(ph, 0) + 1
        slot_id = str(slot.get("slot_id") or "")
        name = (key_map.get(slot_id)
                or (ph if counter[ph] == 1 else f"{ph}_{counter[ph]}"))
        cjk = slot.get("cjk") or {}
        per_line = int(cjk.get("characters_per_line") or 0)
        lines = int(cjk.get("lines") or 0)
        out[name] = {
            "chars_per_line": per_line,
            "lines": lines,
            "capacity": per_line * lines,
        }
    _CAPACITY_CACHE[key] = out
    return out

def _slot_placeholder_keys(template_path: str) -> Dict[str, str]:
    """slot id → {{KEY}} 映射（模板里每个占位槽装哪些占位符）。

    读模板原始 SVG（元数据未被剥离）：每个带 data-pptx-bounds 的 <g id=...>
    里出现的 {{KEY}} 就是该槽的文案键。同槽多键（少见）取第一个。
    """
    out: Dict[str, str] = {}
    try:
        with open(template_path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return out
    for m in _SLOT_BOUNDS_RE.finditer(raw):
        head = raw[m.start():m.start() + 200]
        id_m = _IMAGE_ID_RE.search(head)
        keys = _PLACEHOLDER_RE.findall(m.group(5) or "")
        if id_m and keys:
            out[id_m.group(1)] = keys[0]
    return out


def capacity_warnings(suite: str, pages: Any) -> List[str]:
    """对一份 pages 计划做容量体检，返回人话警告（空 = 全部放得下）。

    在 `create_from_template` 之前调用：用户能提前知道哪页文案过长，
    而不是生成完才发现字被缩小或溢出。
    """
    warns: List[str] = []
    if not isinstance(pages, list):
        return warns
    for idx, item in enumerate(pages, 1):
        if not isinstance(item, dict):
            continue
        want = str(item.get("page") or "").strip()
        values = item.get("values") or {}
        if not want or not isinstance(values, dict):
            continue
        try:
            file_name = resolve_page_file(suite, want)
        except FileNotFoundError:
            continue
        caps = slot_capacity(suite, file_name)
        for ph, cap in caps.items():
            text = str(values.get(ph) or "")
            if not text or cap["capacity"] <= 0:
                continue
            # 中文按 1 字宽、西文按 0.55 粗算（与排版引擎同口径）
            used = sum(1 if ord(c) > 0x2E80 else _ASCII_RATIO
                       for c in text if c != "\n")
            if used > cap["capacity"] * 1.05:      # 5% 容差
                warns.append(
                    f"第 {idx} 页 {ph} 文案偏长（约 {int(used)} 字宽 > "
                    f"版面容量 {cap['capacity']}），生成时会自动缩小字号")
    return warns

#: 已清理 svg → {占位符: [x, y, w, h]}。prepare_page_svg 剥离元数据前抓取，
#: fill_page_svg 按同一个 svg 字符串取回（排版引擎要知道能用多宽多高）。
_BOUNDS_CACHE: Dict[str, Dict[str, List[float]]] = {}

#: 最近一次 fill_page_svg 里仍溢出的占位符（供 fill_page_svg_checked 取用）。
#: 用模块级容器而不是改返回类型：fill_page_svg 有十几个调用点与测试，
#: 改签名会连锁破坏；溢出信息是「附加诊断」，不需要进主返回值。
_FILL_OVERFLOW: List[str] = []


def fill_page_svg_checked(svg: str, values: Dict[str, str],
                          workspace: Optional[str] = None) -> Tuple[str, List[str]]:
    """同 fill_page_svg，但额外返回「缩小字号后仍放不下」的占位符。

    调用方用它给用户明确提示（哪一页哪个槽位文案过长），而不是让用户
    自己发现某页字被挤小了或溢出被裁。
    """
    _FILL_OVERFLOW.clear()
    out = fill_page_svg(svg, values, workspace)
    return out, list(_FILL_OVERFLOW)


def fill_page_svg(svg: str, values: Dict[str, str],
                  workspace: Optional[str] = None) -> str:
    """用给定值替换 {{PLACEHOLDER}}。

    未提供值的占位符替换为空串（而不是留着 `{{X}}`——那会被当成正文导出）。

    多行值转成 `<tspan dy>` 软换行：DrawingML 的段落模型里，纯文本中的 `\\n`
    会被折叠成空格（导出后实测「要点一 要点二 要点三」挤成一行），只有
    tspan 分组才会被 converter 折叠成硬换行（`\\x0b`）。模板正文占位符
    （CONTENT_AREA / BODY_TEXT 等）天然是多要点，这里必须按行拆分。
    每个 tspan 显式带 x：converter 把带 x/dy 的 tspan 视为独立行，
    缺 x 时会退回父 text 的基线导致整行右移。

    `IMAGE_n` 键走图片通道（见 `_fill_images`）：值可以是工作区相对路径、
    绝对路径或 `data:` URI，替换掉图片位里的 1×1 占位图。

    **自动排版**：按占位符的 `data-pptx-bounds` 实测可用宽高，先折行、再按
    字号阶梯缩排。用户不必手写 `\\n`——AI 生成的长文案会自动排进版面，
    而不是像以前那样水平溢出导致整份稿子导不出来。
    `overflows` 输出仍放不下的占位符（调用方据此给用户明确提示）。
    """
    # bounds 来自 prepare_page_svg 的缓存（它剥离元数据前抓的）；
    # 直接传原始未清理 svg 时仍能就地抓取（向后兼容）。
    bounds = _BOUNDS_CACHE.get(svg) or _slot_bounds(svg)
    overflow_keys: List[str] = []

    # 单行占位符先替换，需折行的占位符单独处理（要按父 <text> 的 x 展开）
    wrap_keys: Dict[str, List[str]] = {}

    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        text = str(values.get(key, ""))
        box = bounds.get(key)
        # 已知可用区域 + 文案非空 → 交给排版引擎决定怎么断
        if box and text.strip():
            wrap_keys[key] = text.split("\n")
            return "\0%s\0" % key
        if "\n" in text and key not in _IMAGE_KEY_RE.pattern:
            wrap_keys[key] = text.split("\n")
            return "\0%s\0" % key
        return _xml_escape(text)

    out = _PLACEHOLDER_RE.sub(repl, svg)
    out = _fill_images(out, values, workspace)
    if not wrap_keys:
        return out

    def per_text(m: re.Match[str]) -> str:
        block = m.group(0)
        keys = re.findall(r"\0([A-Z][A-Z0-9_]*)\0", block)
        if not keys:
            return block
        xm = re.search(r'\bx="(-?[\d.]+)"', block)
        x_attr = f' x="{xm.group(1)}"' if xm else ""
        fm = re.search(r'font-size="([\d.]+)"', block)
        base_size = float(fm.group(1)) if fm else 22.0
        for key in keys:
            box = bounds.get(key)
            if box:
                _x, _y, w, h = box
                # 左右各留 4% 安全边（文字有内边距，贴边会被裁）
                lines, size, overflow = fit_text(
                    "\n".join(wrap_keys[key]), max(1.0, w * 0.96),
                    max(1.0, h), base_size)
                if overflow:
                    overflow_keys.append(key)
            else:
                lines, size = wrap_keys[key], base_size
            if abs(size - base_size) > 0.01:
                block = re.sub(r'font-size="[\d.]+"', f'font-size="{size:g}"', block)
            line_h = round(size * 1.5)
            spans = "".join(
                f'<tspan{x_attr} dy="{0 if i == 0 else line_h}">{_xml_escape(line)}</tspan>'
                for i, line in enumerate(lines)
            )
            block = block.replace("\0%s\0" % key, spans)
        return block

    _FILL_OVERFLOW.clear()
    result = re.sub(r"<text\b.*?</text>", per_text, out, flags=re.S)
    _FILL_OVERFLOW.extend(overflow_keys)
    return result


def collect_font_sizes(svg: str) -> List[float]:
    """取一页 SVG 里出现的字号（供 spec_lock 的 typography 声明）。"""
    return sorted({float(x) for x in re.findall(r'font-size="([\d.]+)"', svg)})


def write_spec_lock(project_dir: str, *, viewbox: str, canvas_format: str,
                    brand_color: str = "", style: str = "",
                    title_family: str = "Microsoft YaHei",
                    body_family: str = "Microsoft YaHei",
                    title_size: float = 54, body_size: float = 20,
                    extra_sizes: Optional[List[float]] = None) -> str:
    """生成导出所需的最小 spec_lock.md。

    ppt-master 的 release 导出强制要求这份文件（配色/字体/结构模式）。
    flat 模式用于纯视觉 SVG——正是模板填充产物的形态。

    extra_sizes 必须覆盖模板里实际出现的所有字号：质检会校验「未声明的
    字号出现次数」，模板自带的 28/32 之类小标题字号不声明就会判失败。
    """
    # 把 body/title 与模板实际字号合并成完整的 typography 声明
    declared = sorted({float(body_size), float(title_size), *(extra_sizes or [])})
    size_lines = []
    for i, size in enumerate(declared):
        key = "body" if i == 0 else ("title" if i == len(declared) - 1 else f"h{i}")
        size_lines.append(f"- {key}: {size:g}")
    # body / title 必须有且是首尾两端；重复键会被后者覆盖，故显式收尾
    size_lines = [ln for ln in size_lines if not ln.startswith("- body:")]
    size_lines.insert(0, f"- body: {min(declared):g}")
    size_lines = [ln for ln in size_lines if not ln.startswith("- title:")]
    size_lines.append(f"- title: {max(declared):g}")

    text = (
        "<!-- ppt-master-schema: spec-lock/v1 -->\n"
        "# Execution Lock\n\n"
        "## canvas\n"
        f"- viewBox: {viewbox}\n"
        f"- format: {canvas_format}\n\n"
        "## communication\n"
        "- primary_language: zh-CN\n"
        "- audience: 通用\n"
        "- objective: 按模板生成演示文稿\n"
        "- core_message: -\n"
        "- consumption_mode: 现场汇报\n\n"
        "## mode\n- mode: narrative\n\n"
        "## visual_style\n"
        f"- visual_style: {style or 'consulting-decision'}\n\n"
        "## colors\n"
        "- bg: #FFFFFF\n"
        f"- primary: {brand_color or '#1E293B'}\n"
        f"- accent: {brand_color or '#2563EB'}\n"
        "- text: #1E293B\n\n"
        "## typography\n"
        f"- font_family: {title_family}\n"
        f"- title_family: {title_family}\n"
        f"- body_family: {body_family}\n"
        + "\n".join(size_lines) + "\n\n"
        "## icons\n- library: tabler-outline\n- inventory: none\n\n"
        "## page_rhythm\n- P01: hero\n\n"
        "## pptx_structure\n- mode: flat\n\n"
        "## forbidden\n- `mask`, `<style>`, `class`\n"
    )
    path = os.path.join(project_dir, "spec_lock.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path
