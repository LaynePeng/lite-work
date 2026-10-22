# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""PPT 规划：把一句需求（或一份旧稿）变成「逐页版式 + 占位符文案」。

为什么需要独立一层：模板填充本身是机械的（见 ppt_templates），但「这页讲什么、
用哪个版式、文案压缩到多长」需要语义判断。原先前端把这段判断全交给对话页签
里的 Agent，代价是用户被踢出当前界面、还要等一整轮 Agent 任务；现在收成一个
确定性的单次 LLM 调用，结果可编辑、可直接生成。

两条来源：
- **提示词**：用户输入一句话需求，模型按套件的版式清单排页并写文案；
- **源文件**：已有 pptx 的逐页结构（ppt_edit op=extract 的产物），重排映射。

输出统一为 `{"pages": [{"page": slug, "label": 中文名, "values": {...}}]}`，
即 `create_from_template` 的 `template.pages`，两者可无缝衔接。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("litework.tools.ppt_plan")

#: 页数上限：再多就不是「一句话生成」而是长文档了，也避免单次调用过大。
MAX_PAGES = 30
#: 占位符文案长度上限（字符）：超了在提示词里压缩不了，直接截断兜底。
MAX_VALUE_CHARS = 600

#: 占位符 → 中文用途（模型据此判断该写什么）
PLACEHOLDER_HINTS: Dict[str, str] = {
    "TITLE": "演示文稿主标题",
    "PAGE_TITLE": "本页标题（≤18 字）",
    "SUBTITLE": "副标题 / 一句话说明",
    "CHAPTER_TITLE": "章节标题",
    "CHAPTER_DESC": "章节简述",
    "CHAPTER_NUM": "章节序号（01/02…）",
    "CONTENT_AREA": "正文要点，每行一条，用换行分隔",
    "BODY_TEXT": "正文要点，每行一条，用换行分隔",
    "TOP_CONTENT": "上半区正文",
    "BOTTOM_CONTENT": "下半区正文",
    "KEY_MESSAGE": "本页核心观点（一句话结论）",
    "SUPPORT_TEXT": "支撑说明 / 论据",
    "QUOTE_TEXT": "金句原文",
    "ATTRIBUTION": "金句出处",
    "CLOSING_MESSAGE": "结束语",
    "CTA_TEXT": "行动号召",
    "CONTACT_LINE": "联系方式",
    "BRAND_LINE": "品牌落款",
    "FOOTER_NOTE": "页脚说明",
    "PAGE_NUM": "页码（留空自动编号也可以）",
    "DATE": "日期",
    "SOURCE": "数据来源",
    "CAPTION": "图注",
    "CAPTION_1": "图注 1",
    "CAPTION_2": "图注 2",
    "CAPTION_3": "图注 3",
    "CAPTION_4": "图注 4",
    "ANNOTATION": "注释",
    "LEFT_TITLE": "左栏标题",
    "LEFT_CONTENT": "左栏要点",
    "LEFT_CAPTION": "左栏图注",
    "RIGHT_TITLE": "右栏标题",
    "RIGHT_CONTENT": "右栏要点",
    "RIGHT_CAPTION": "右栏图注",
    "CARD_1": "卡片 1 文案",
    "CARD_2": "卡片 2 文案",
    "CARD_3": "卡片 3 文案",
    "BLOCK_1": "区块 1 文案",
    "BLOCK_2": "区块 2 文案",
    "BLOCK_3": "区块 3 文案",
    "ITEM_1": "条目 1",
    "ITEM_2": "条目 2",
    "ITEM_3": "条目 3",
    "ITEM_4": "条目 4",
    "ITEM_5": "条目 5",
    "STEP_1": "步骤 1",
    "STEP_2": "步骤 2",
    "STEP_3": "步骤 3",
    "STEP_4": "步骤 4",
    "KPI_1": "指标 1（数字 + 单位）",
    "KPI_2": "指标 2",
    "KPI_3": "指标 3",
    "KPI_4": "指标 4",
    "QUADRANT_1": "象限 1",
    "QUADRANT_2": "象限 2",
    "QUADRANT_3": "象限 3",
    "QUADRANT_4": "象限 4",
    "X_AXIS": "横轴说明",
    "Y_AXIS": "纵轴说明",
}


class PptPlanError(ValueError):
    """规划失败（文案可读，直接回给前端/模型）。"""


def _hints(placeholders: List[str]) -> str:
    return "、".join(f"{{{p}}}={PLACEHOLDER_HINTS.get(p, p)}" for p in placeholders)


def build_prompt(
    *,
    suite_info: Dict[str, Any],
    brief: str,
    audience: str = "",
    page_hint: int = 0,
    source_slides: Optional[List[Dict[str, Any]]] = None,
    with_images: bool = False,
) -> List[Dict[str, str]]:
    """组规划用的提示词（system + user）。纯函数，便于单测断言。

    with_images=True 时额外要求模型给带图版式写 `image_prompts`：生图模型
    渲染文字几乎必错，所以提示词只描述**画面**（主体/场景/风格），不写标题
    文案——标题由版式自己排。
    """
    pages_lines = []
    for p in suite_info.get("pages", []):
        line = f"- page={p['page']}（{p['label']}）：{_hints(p['placeholders'])}"
        slots = p.get("image_slots") or []
        if with_images and slots:
            line += f"【可配图 {len(slots)} 张：{','.join(s['key'] for s in slots)}】"
        pages_lines.append(line)
    pages_text = "\n".join(pages_lines)

    system = (
        "你是资深演示文稿架构师。把用户需求拆成逐页方案，并按给定版式的占位符写文案。\n"
        "硬性规则：\n"
        "1. page 只能取给定版式清单里的值，不得编造；\n"
        "2. 正文类占位符（CONTENT_AREA/BODY_TEXT/LEFT_CONTENT/RIGHT_CONTENT 等）"
        "每个要点单独一行，用 \\n 分隔，不要编号符号，不要写成一整段；\n"
        "3. 一个版式可以重复使用；页数与用户要求一致（未指定则 8–12 页）；\n"
        "4. 文案要具体、可讲，不写「此处介绍…」这类占位话术；没有数据就写判断与动作；\n"
        "5. 只输出 JSON，不要解释、不要 Markdown 代码块。\n"
    )
    if with_images:
        system += (
            "6. 带【可配图】的版式请额外给 `image_prompts`：键是图片位（IMAGE_1 等），"
            "值是一句**英文**画面描述（主体 + 场景 + 风格，如 "
            "\"aerial view of a modern logistics warehouse, corporate photography\"）。"
            "绝对不要在画面描述里要求出现任何文字、字母、数字或 logo。\n"
        )
    system += (
        '输出格式：{"pages":[{"page":"封面版式的 page 值","values":{"PLACEHOLDER":"文案"},'
        '"image_prompts":{"IMAGE_1":"english visual description"}}]}'
        if with_images else
        '输出格式：{"pages":[{"page":"封面版式的 page 值","values":{"PLACEHOLDER":"文案"}}]}'
    )

    parts = [f"版式套件：{suite_info.get('label') or suite_info.get('suite')}"]
    if suite_info.get("summary"):
        parts.append(f"套件说明：{suite_info['summary']}")
    parts.append(f"可用版式（page 取值 → 占位符含义）：\n{pages_text}")
    if audience:
        parts.append(f"受众/场合：{audience}")
    if page_hint:
        parts.append(f"页数要求：{page_hint} 页")
    if source_slides:
        parts.append("已有讲稿的逐页内容（把它重排到上面的版式，允许精简改写）：")
        for s in source_slides:
            body = "\n".join(f"  · {x}" for x in s.get("paragraphs", [])[:8]) or "  （无文本）"
            parts.append(f"第 {s.get('slide')} 页｜标题：{s.get('title') or '（无）'}\n{body}")
        parts.append("要求：保留原稿的信息量与顺序，地图到最合适的版式；表格/图表页保留关键数字。")
    parts.append(f"需求：{brief}")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.S)
_JSON_RE = re.compile(r"\{.*\}", re.S)


def parse_plan(raw: str, suite_info: Dict[str, Any]) -> List[Dict[str, Any]]:
    """解析模型输出为 pages；非法页标识/多余额外占位符在此收口。"""
    text = _FENCE_RE.sub("", raw or "").strip()
    if not text:
        raise PptPlanError("模型没有返回内容")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_RE.search(text)
        if not m:
            raise PptPlanError(f"模型输出不是 JSON：{text[:200]}")
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError as exc:
            raise PptPlanError(f"模型输出不是合法 JSON：{exc}") from exc

    pages = data.get("pages") if isinstance(data, dict) else data
    if not isinstance(pages, list) or not pages:
        raise PptPlanError("模型没有给出任何页面")

    by_page = {p["page"]: p for p in suite_info.get("pages", [])}
    out: List[Dict[str, Any]] = []
    for idx, item in enumerate(pages[:MAX_PAGES], 1):
        if not isinstance(item, dict):
            continue
        want = str(item.get("page") or "").strip()
        # 模型常写 "01_title_slide.svg" / "01_title_slide" / "title_slide"，
        # 统一归一到 slug
        slug = want
        for cand in (want, want[:-4] if want.endswith(".svg") else want):
            if cand in by_page:
                slug = cand
                break
            if "_" in cand:
                tail = cand.split("_", 1)[-1]
                if tail in by_page:
                    slug = tail
                    break
        meta = by_page.get(slug)
        if meta is None:
            raise PptPlanError(
                f"第 {idx} 页用了不存在的版式 {want!r}；"
                f"可用：{', '.join(list(by_page)[:8])} …")
        raw_values = item.get("values") or {}
        values: Dict[str, str] = {}
        if isinstance(raw_values, dict):
            allowed = set(meta["placeholders"])
            for k, v in raw_values.items():
                key = str(k).strip()
                if key not in allowed:
                    continue
                text_v = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                values[key] = text_v[:MAX_VALUE_CHARS]
        # 配图提示词：只保留该版式真实存在的图片位（模型偶尔会编 IMAGE_9）
        prompts: Dict[str, str] = {}
        raw_prompts = item.get("image_prompts") or {}
        if isinstance(raw_prompts, dict):
            allowed_slots = {s["key"] for s in (meta.get("image_slots") or [])}
            for k, v in raw_prompts.items():
                key = str(k).strip()
                if key in allowed_slots and isinstance(v, str) and v.strip():
                    prompts[key] = v.strip()[:400]
        page: Dict[str, Any] = {"page": slug, "label": meta["label"], "values": values}
        if prompts:
            page["image_prompts"] = prompts
        out.append(page)
    if not out:
        raise PptPlanError("解析后没有可用的页面")
    return out


def rewrite_prompt(
    *,
    page_label: str,
    placeholders: Dict[str, str],
    current: Dict[str, str],
    instruction: str,
) -> List[Dict[str, str]]:
    """单页重写的提示词（B3：不动其他页，只改这一页的文案）。"""
    system = (
        "你是演示文稿文案编辑。只重写给定占位符的文案，输出 JSON。规则：\n"
        "1. 保留占位符键不变，只改值；\n"
        "2. 正文类占位符每行一个要点，用 \\n 分隔；\n"
        "3. 文案要具体可讲；没有数据就写判断与动作；\n"
        "4. 只输出 JSON：{\"values\": {\"KEY\": \"新文案\"}}，不要解释。"
    )
    lines = [f"页面版式：{page_label}"]
    for key, hint in placeholders.items():
        cur = current.get(key, "")
        lines.append(f"- {key}（{hint}）当前值：{cur or '（空）'}")
    lines.append(f"用户要求：{instruction}")
    return [{"role": "system", "content": system},
            {"role": "user", "content": "\n".join(lines)}]


def parse_rewrite(raw: str, allowed: set) -> Dict[str, str]:
    """解析单页重写输出，只保留合法键。"""
    text = _FENCE_RE.sub("", raw or "").strip()
    if not text:
        raise PptPlanError("模型没有返回内容")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_RE.search(text)
        if not m:
            raise PptPlanError(f"模型输出不是 JSON：{text[:200]}")
        data = json.loads(m.group(0))
    values = (data or {}).get("values") if isinstance(data, dict) else None
    if not isinstance(values, dict) or not values:
        raise PptPlanError("重写结果为空")
    out: Dict[str, str] = {}
    for k, v in values.items():
        key = str(k).strip()
        if key in allowed and isinstance(v, str):
            out[key] = v[:600]
    if not out:
        raise PptPlanError("重写结果没有命中任何占位符")
    return out


def fallback_plan(
    suite_info: Dict[str, Any], brief: str, page_hint: int = 0
) -> List[Dict[str, Any]]:
    """模型不可用时的兜底：按套件的常见顺序排出骨架页，文案留白。

    宁可给一份「结构对、只剩文案空」的稿子，也不要让用户对着报错发呆——
    生成后进编辑器补字，比从零开始快得多。
    """
    pages = suite_info.get("pages", [])
    by_slug = {p["page"]: p for p in pages}
    order = ["title_slide", "title_content", "section_header", "three_card",
             "kpi_dashboard", "process_timeline", "chart_insight", "table_summary",
             "title_content", "hero_statement"]
    picked = [s for s in order if s in by_slug]
    if not picked:
        picked = [p["page"] for p in pages[:6]]
    if page_hint > 0:
        while len(picked) < page_hint:
            picked.append("title_content" if "title_content" in by_slug else picked[-1])
        picked = picked[:page_hint]
    title = (brief or "演示文稿").strip().splitlines()[0][:40]
    out = []
    for i, slug in enumerate(picked):
        meta = by_slug[slug]
        values: Dict[str, str] = {}
        for ph in meta["placeholders"]:
            if ph in ("TITLE", "PAGE_TITLE") and i == 0:
                values[ph] = title
            elif ph == "SUBTITLE" and i == 0:
                values[ph] = "待补充副标题"
            elif ph == "TITLE":
                values[ph] = title
        out.append({"page": slug, "label": meta["label"], "values": values})
    return out
