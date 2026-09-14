#!/usr/bin/env python3
"""Markdown → 美化 PDF。

两种引擎：

- ``reportlab``（默认）：中文字体自动嵌入，页眉线 + 页脚页码，表格底纹齐全。
- ``chrome``：Markdown → HTML/CSS → 系统 Chrome 无头打印，排版观感更好，
  但**无法输出页脚页码**（Chromium 未实现 CSS 分页边距框），需页码时用 reportlab。
- ``auto``：检测到 Chrome 用 chrome，否则 reportlab。

用法::

    python3 md2pdf.py 输入.md --theme business-blue --title "标题" --out 产出物/xxx.pdf
    python3 md2pdf.py 输入.md --engine chrome --out 产出物/xxx.pdf
    cat 输入.md | python3 md2pdf.py - --out 产出物/xxx.pdf
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import themes as _themes   # noqa: E402
import _md                 # noqa: E402

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(SKILL_DIR, "assets", "html")

_CJK = None
_CJK_BOLD = None

_CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "google-chrome",
    "chromium",
    "chromium-browser",
]


# --------------------------------------------------------------- 中文字体

def register_cjk():
    """注册一个可嵌入的中文字体，返回 (regular, bold)。"""
    global _CJK, _CJK_BOLD
    if _CJK:
        return _CJK, _CJK_BOLD or _CJK
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if sys.platform == "darwin":
        cands = [
            ("/System/Library/Fonts/PingFang.ttc", "PingFangSC-Regular", "PingFangSC-Semibold"),
            ("/System/Library/Fonts/STHeiti Medium.ttc", 0, 0),
            ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0, 0),
            ("/System/Library/Fonts/Supplemental/Songti.ttc", 0, 0),
            ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 0, 0),
        ]
    elif sys.platform.startswith("win"):
        cands = [
            ("C:/Windows/Fonts/msyh.ttc", 0, 0),
            ("C:/Windows/Fonts/simsun.ttc", 0, 0),
            ("C:/Windows/Fonts/simhei.ttf", 0, 0),
        ]
    else:
        cands = [
            ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0, 0),
            ("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc", 0, 0),
            ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0, 0),
            ("/usr/share/fonts/truetype/arphic/uming.ttc", 0, 0),
        ]

    for path, reg, bold in cands:
        if not os.path.isfile(path):
            continue
        try:
            pdfmetrics.registerFont(TTFont("CJK", path, subfontIndex=reg))
        except Exception:
            try:
                pdfmetrics.registerFont(TTFont("CJK", path, subfontIndex=0))
            except Exception:
                continue
        b = "CJK"
        try:
            if bold not in (reg, None):
                pdfmetrics.registerFont(TTFont("CJK-Bold", path, subfontIndex=bold))
                b = "CJK-Bold"
        except Exception:
            pass
        pdfmetrics.registerFontFamily("CJK", normal="CJK", bold=b,
                                      italic="CJK", boldItalic=b)
        _CJK, _CJK_BOLD = "CJK", b
        return _CJK, _CJK_BOLD

    try:
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        pdfmetrics.registerFontFamily("STSong-Light", normal="STSong-Light",
                                      bold="STSong-Light", italic="STSong-Light",
                                      boldItalic="STSong-Light")
        _CJK = _CJK_BOLD = "STSong-Light"
    except Exception:
        _CJK = _CJK_BOLD = "Helvetica"
    return _CJK, _CJK_BOLD


def _has_cjk(text: str) -> bool:
    return any("\u2e80" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff"
               or "\uff00" <= ch <= "\uffef" for ch in text)


# --------------------------------------------------------------- reportlab 引擎

def _styles(font, bold, theme):
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
    from reportlab.lib.units import mm

    accent = theme.get("color_primary", "#1F4E79")
    text_color = theme.get("color_text", "#1A1A1A")
    s_body = float(theme.get("size_body", 12)) - 1.5
    s_h = {1: theme.get("size_h1", 22), 2: theme.get("size_h2", 16), 3: theme.get("size_h3", 14)}

    def para(name, **kw):
        kw.setdefault("wordWrap", "CJK")
        return ParagraphStyle(name, **kw)

    out = {
        "title": para("T", fontName=bold, fontSize=float(theme.get("size_h1", 22)) + 2,
                      leading=float(theme.get("size_h1", 22)) + 10, alignment=TA_CENTER,
                      spaceAfter=10 * mm, textColor=accent),
        "body": para("B", fontName=font, fontSize=s_body, leading=s_body * 1.7,
                     alignment=TA_JUSTIFY, spaceAfter=2.5 * mm, textColor=text_color),
        "code": para("C", fontName="Courier", fontSize=8.5, leading=11,
                     leftIndent=4 * mm, backColor=theme.get("color_code_bg", "#F5F5F5"),
                     borderPadding=4, spaceAfter=3 * mm),
        "code_cjk": para("C2", fontName=font, fontSize=8.5, leading=11,
                         leftIndent=4 * mm, backColor=theme.get("color_code_bg", "#F5F5F5"),
                         borderPadding=4, spaceAfter=3 * mm),
        "cell": para("cell", fontName=font, fontSize=9.5, leading=13),
        "cellh": para("cellh", fontName=bold, fontSize=9.5, leading=13, textColor="#FFFFFF"),
    }
    for lvl, size in s_h.items():
        out["h%d" % lvl] = para("H%d" % lvl, fontName=bold, fontSize=float(size),
                                leading=float(size) * 1.35,
                                spaceBefore=(6 if lvl == 1 else 4) * mm,
                                spaceAfter=2 * mm, textColor=accent)
    out["h4"] = out["h5"] = out["h3"]
    return out


def _inline(text: str) -> str:
    text = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)

    def _code(m):
        inner = m.group(1)
        face = _CJK if (_CJK and _has_cjk(inner)) else "Courier"
        return '<font face="%s">%s</font>' % (face, inner)

    text = re.sub(r"`([^`]+)`", _code, text)
    text = re.sub(r"~~(.+?)~~", r"<strike>\1</strike>", text)
    text = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<i>\1</i>", text)
    return text


def _table(rows, styles, theme):
    from reportlab.platypus import Table, TableStyle, Paragraph
    from reportlab.lib import colors
    data = [[Paragraph(_inline(c), styles["cellh" if r == 0 else "cell"])
             for c in row] for r, row in enumerate(rows)]
    t = Table(data, repeatRows=1, hAlign="LEFT")
    cmds = [
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D0D7DE")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(theme.get("color_table_head", "#1F4E79"))),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]
    band = theme.get("color_table_band")
    if band:
        for i in range(2, len(data), 2):
            cmds.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor(band)))
    t.setStyle(TableStyle(cmds))
    return t


def build_reportlab(blocks, title, theme, out_path, footer=True) -> str:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer,
                                    ListFlowable, ListItem, Preformatted)

    font, bold = register_cjk()
    st = _styles(font, bold, theme)
    margin = float(theme.get("margin_mm", 25.4)) * mm
    label = title if isinstance(footer, bool) and footer else (footer or "")

    def draw(canvas, doc):
        from reportlab.lib import colors
        canvas.saveState()
        w, h = doc.pagesize
        canvas.setStrokeColor(colors.HexColor("#DDDDDD"))
        canvas.setLineWidth(0.4)
        canvas.line(margin, h - 15 * mm, w - margin, h - 15 * mm)
        canvas.setFont(font, 8)
        canvas.setFillColor(colors.HexColor(theme.get("color_muted", "#6B7280")))
        if label:
            canvas.drawString(margin, h - 13 * mm, str(label))
        canvas.drawCentredString(w / 2, 12 * mm, "第 %d 页" % canvas.getPageNumber())
        canvas.restoreState()

    doc = SimpleDocTemplate(out_path, pagesize=A4, topMargin=margin,
                            bottomMargin=margin, leftMargin=margin, rightMargin=margin)
    story = []
    if title:
        story.append(Paragraph(_inline(title), st["title"]))
        story.append(Spacer(1, 4 * mm))

    for blk in blocks:
        kind = blk[0]
        if kind == "h":
            story.append(Paragraph(_inline(blk[2]), st["h%d" % min(blk[1], 4)]))
        elif kind == "p":
            story.append(Paragraph(_inline(blk[1]), st["body"]))
        elif kind in ("ul", "ol"):
            story.append(ListFlowable(
                [ListItem(Paragraph(_inline(blk[1]), st["body"]))],
                bulletType="bullet" if kind == "ul" else "1",
                leftIndent=24, bulletOffsetY=-2))
        elif kind == "code":
            story.append(Preformatted(
                blk[2], st["code_cjk"] if _has_cjk(blk[2]) else st["code"]))
        elif kind == "img":
            try:
                from reportlab.platypus import Image as RLImage
                from PIL import Image as PILImage
                with PILImage.open(blk[2]) as im:
                    w_px, h_px = im.size
                scale = min(160 * mm / w_px, 1.0) if w_px else 1.0
                story.append(RLImage(blk[2], width=w_px * scale, height=h_px * scale))
                story.append(Spacer(1, 3 * mm))
            except Exception:
                story.append(Paragraph(_inline("[图片未找到: %s]" % blk[2]), st["body"]))
        elif kind == "table":
            story.append(_table(blk[1], st, theme))
            story.append(Spacer(1, 3 * mm))

    # 回调必须传给 build()（构造函数会静默忽略）
    doc.build(story, onFirstPage=draw, onLaterPages=draw)
    return out_path


# --------------------------------------------------------------- chrome 引擎

def find_chrome():
    for c in _CHROME_CANDIDATES:
        if os.path.isabs(c):
            if os.path.isfile(c):
                return c
        else:
            from shutil import which
            p = which(c)
            if p:
                return p
    return None


def _to_html(blocks, title, theme) -> str:
    def esc(s):
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    def inline(s):
        s = esc(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"~~(.+?)~~", r"<del>\1</del>", s)
        s = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", s)
        return s

    parts = []
    for blk in blocks:
        k = blk[0]
        if k == "h":
            lvl = min(blk[1], 4)
            parts.append("<h%d>%s</h%d>" % (lvl, inline(blk[2]), lvl))
        elif k == "p":
            parts.append("<p>%s</p>" % inline(blk[1]))
        elif k == "ul":
            parts.append("<ul><li>%s</li></ul>" % inline(blk[1]))
        elif k == "ol":
            parts.append("<ol><li>%s</li></ol>" % inline(blk[1]))
        elif k == "code":
            parts.append("<pre><code>%s</code></pre>" % esc(blk[2]))
        elif k == "img":
            parts.append('<figure><img src="%s" alt="%s"></figure>'
                         % (esc(blk[2]), esc(blk[1])))
        elif k == "table":
            rows = blk[1]
            head = "".join("<th>%s</th>" % inline(c) for c in rows[0])
            body = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % inline(c) for c in r)
                           for r in rows[1:])
            parts.append("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>"
                         % (head, body))

    css_path = os.path.join(ASSETS, "print.css")
    css = open(css_path, encoding="utf-8").read() if os.path.isfile(css_path) else ""
    vars_css = (
        ":root{--primary:%s;--accent:%s;--text:%s;--muted:%s;"
        "--thead:%s;--band:%s;--code-bg:%s;--margin:%smm;--font:%s;--font-head:%s;}"
        % (theme.get("color_primary"), theme.get("color_accent"), theme.get("color_text"),
           theme.get("color_muted"), theme.get("color_table_head"), theme.get("color_table_band"),
           theme.get("color_code_bg"), theme.get("margin_mm", 25.4),
           theme.get("font_zh"), theme.get("font_heading_zh"))
    )
    h1 = '<h1 class="doc-title">%s</h1>' % esc(title) if title else ""
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<style>%s\n%s</style></head><body>%s\n%s</body></html>"
            % (vars_css, css, h1, "\n".join(parts)))


def build_chrome(blocks, title, theme, out_path) -> str:
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError("未找到 Chrome/Chromium")
    html = _to_html(blocks, title, theme)
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False,
                                     encoding="utf-8") as f:
        f.write(html)
        html_path = f.name
    try:
        cmd = [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
               "--no-pdf-header-footer",
               "--print-to-pdf=%s" % os.path.abspath(out_path),
               "file://%s" % html_path]
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
        if proc.returncode != 0 or not os.path.isfile(out_path):
            cmd[4] = "--print-to-pdf-no-header"
            proc = subprocess.run(cmd, capture_output=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode("utf-8", "ignore")[:300])
    finally:
        try:
            os.remove(html_path)
        except OSError:
            pass
    return out_path


# --------------------------------------------------------------- CLI

def build(md_text, title, theme_name_or_dict, out_path, engine="reportlab", footer=True):
    theme = (theme_name_or_dict if isinstance(theme_name_or_dict, dict)
             else _themes.load(theme_name_or_dict))
    blocks = _md.parse(md_text)
    d = os.path.dirname(os.path.abspath(out_path))
    if d:
        os.makedirs(d, exist_ok=True)

    if engine in ("auto", "chrome"):
        try:
            return build_chrome(blocks, title, theme, out_path)
        except Exception as exc:
            if engine == "chrome":
                raise
            sys.stderr.write("[warn] Chrome 引擎不可用（%s），回退 reportlab\n" % exc)
    return build_reportlab(blocks, title, theme, out_path, footer=footer)


def main() -> int:
    ap = argparse.ArgumentParser(description="Markdown → 美化 PDF")
    ap.add_argument("input", help="Markdown 文件（- 表示 stdin）")
    ap.add_argument("--theme", default="business-blue")
    ap.add_argument("--title", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--engine", default="reportlab",
                    choices=["reportlab", "chrome", "auto"])
    ap.add_argument("--no-footer", action="store_true")
    a = ap.parse_args()

    md = sys.stdin.read() if a.input == "-" else open(a.input, encoding="utf-8").read()
    theme = _themes.load(a.theme)
    out = a.out or (os.path.splitext(os.path.basename(a.input))[0] + ".pdf")
    path = build(md, a.title, theme, out, engine=a.engine, footer=not a.no_footer)
    print("[OK] 已生成 %s（主题：%s，引擎：%s）"
          % (path, theme.get("label", a.theme), a.engine))
    return 0


if __name__ == "__main__":
    sys.exit(main())
