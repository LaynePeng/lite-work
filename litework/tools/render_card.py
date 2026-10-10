# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""富内容卡片（render_card）：Agent 生成的 HTML 卡片在聊天流内渲染。

场景对齐 Muse 的富气泡：多实体对比（「找便宜运动相机」→ 带图带价的商品
对比卡）、图表、结构化展示。纯文字仍走普通 markdown 回复。

安全模型：
- HTML 只落盘（.lite-work/cards/<session>/<id>.html），消息流/LLM 上下文里
  只存紧凑结果（卡片 id）——不把大段 HTML 回填进上下文，也不污染会话存储；
- 前端在 sandbox iframe 里渲染（无 allow-scripts）——脚本不执行；
- 相对路径引用（<img src="a.png">）由前端 rewrite 到 /api/files/raw
  （复用其越界检查与鉴权），本工具不做路径校验。

工具结果返回结构化标记行，前端 ToolCard 据此特判渲染：
  [Rich Card]: <card_id>
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List

from ..core.types import Plugin, ToolDefinition

#: 卡片 HTML 大小上限（字节）：LLM 单次产出不该超过；防异常膨胀
_MAX_HTML_BYTES = 256 * 1024

#: 卡片 id 合法字符（URL 安全；防路径注入）
_CARD_ID_RE = re.compile(r"^card_[a-z0-9]{6,32}$")

_RESULT_PREFIX = "[Rich Card]:"


def cards_dir(config_dir: str, session_id: str) -> str:
    """会话的卡片目录（不创建；写入时 makedirs）。"""
    import os
    return os.path.join(config_dir, "cards", session_id)


def is_valid_card_id(card_id: str) -> bool:
    return bool(_CARD_ID_RE.match(card_id or ""))


def card_path(config_dir: str, session_id: str, card_id: str) -> str:
    """卡片文件路径；card_id 非法时抛 ValueError（调用方 404）。"""
    if not is_valid_card_id(card_id):
        raise ValueError(f"非法卡片 id: {card_id!r}")
    import os
    return os.path.join(cards_dir(config_dir, session_id), f"{card_id}.html")


class RenderCardPlugin(Plugin):
    """render_card：生成富内容卡片（HTML，聊天流内 sandbox 渲染）。"""

    name = "render-card-plugin"

    def __init__(self, app) -> None:
        self._app = app

    def get_tools(self) -> List[ToolDefinition]:
        return [ToolDefinition(
            name="render_card",
            description=(
                "生成富内容卡片：一段 HTML 在聊天流中直接渲染成可视化卡片。"
                "适用场景：**多实体对比**（商品/方案/候选，配图片、价格、参数）、"
                "图表、结构化摘要（日历/表格/看板）、需要图文混排的结果展示。"
                "不适用：纯文字说明（直接用 markdown 回复）。"
                "规则：①HTML 里可引用图片/音频/视频：远程 https URL 直接写；"
                "workspace 相对路径写相对文件名即可（渲染时自动代理）；"
                "②不要写 <script>（不执行，卡片纯展示）；③自适应宽度（容器约 "
                "600-800px），关键信息前置；④内联样式写在 style 属性或 <style> 里；"
                "⑤一次回复可多张卡片，每张一次调用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "html": {
                        "type": "string",
                        "description": "卡片 HTML（纯展示片段，不含 <script>）",
                    },
                    "title": {
                        "type": "string",
                        "description": "卡片标题（折叠态与卡片头显示）",
                    },
                    "height": {
                        "type": "number",
                        "description": "建议渲染高度（px，0=自适应）",
                    },
                },
                "required": ["html"],
            },
        )]

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        if name != "render_card":
            return f'[Error]: 未知工具 "{name}"。'
        import os
        from .todos import current_session_id

        session_id = current_session_id.get("")
        if not session_id:
            return "[Error]: 无会话上下文，无法生成卡片。"

        html = str(args.get("html") or "").strip()
        if not html:
            return "[Error]: html 为空。"
        if len(html.encode("utf-8")) > _MAX_HTML_BYTES:
            return f"[Error]: 卡片 HTML 超过 {_MAX_HTML_BYTES // 1024}KB 上限。"

        # <script> 防御性剔除：前端 sandbox 本就不执行，这里再剥一层，
        # 降低持久化文件里的攻击面（如后续被非 sandbox 场景读取）
        html_sanitized = re.sub(r"<script\b[^>]*>.*?</script\s*>", "", html,
                                flags=re.IGNORECASE | re.DOTALL)

        title = str(args.get("title") or "").strip()[:120]
        height = int(args.get("height") or 0)

        card_id = f"card_{int(time.time() * 1000):x}{os.urandom(4).hex()}"
        out_dir = cards_dir(self._app.config_dir, session_id)
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{card_id}.html")
        # 元信息以注释头存进 HTML 自身（读取端点一并下发，前端渲染用）
        meta = (f"<!-- litework-card: id={card_id} title={title} "
                f"height={height} at={int(time.time())} -->\n")
        with open(path, "w", encoding="utf-8") as f:
            f.write(meta + html_sanitized)

        # 紧凑结果：不回填 HTML 进上下文（大 JSON 会挤占 token 与存储）
        title_note = f"「{title}」" if title else ""
        return (f"{_RESULT_PREFIX} {card_id}\n"
                f"卡片 {title_note}已渲染在聊天流中。"
                "（用户可见富卡片；此处仅存卡片 id）")
