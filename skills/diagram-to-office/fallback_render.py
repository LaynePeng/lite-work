"""内置兜底渲染器：没有任何外部引擎时，用 matplotlib 画简化版图表。

离线保证：仅依赖项目已依赖的 matplotlib，不联网、不装任何东西，
保证离线也能产出可嵌入 Office 的图片（视觉为简化版）。

支持子集：
- Mermaid flowchart（LR/TB 方向、节点框/菱形/圆形/圆角、带标签边）
- Mermaid / PlantUML 时序图（participant/actor + 消息箭头）
- 其余类型：把源码画成等宽文本图（保证有输出）
"""
from __future__ import annotations

import os
import re
import sys

# 中文字体候选（按平台常见顺序）
_FONTS = ("PingFang SC", "Heiti SC", "Hiragino Sans GB", "Songti SC",
          "Noto Sans CJK SC", "Microsoft YaHei", "SimHei", "Arial Unicode MS")


def _pick_font():
    try:
        from matplotlib import font_manager
        for name in _FONTS:
            try:
                if font_manager.findfont(
                    font_manager.FontProperties(family=name),
                    fallback_to_default=False,
                ):
                    return name
            except Exception:
                continue
    except Exception:
        pass
    return "sans-serif"


def _setup_plt():
    import matplotlib
    matplotlib.use("Agg")  # 非交互后端，服务器/无头环境安全
    import matplotlib.pyplot as plt
    # 全局使用可渲染中文的字体，避免 CJK 变成方框
    font = _pick_font()
    plt.rcParams["font.family"] = font
    return plt


def _save(fig, out_path: str, scale: int) -> None:
    dpi = 100 * max(1, int(scale or 2))
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt = sys.modules.get("matplotlib.pyplot")
    if plt is not None:
        plt.close(fig)


def _node_parse(node_def: str):
    """解析 mermaid 节点定义 → (id, 显示文本, 形状 rect/diamond/circle/rounded)。"""
    node_def = node_def.strip()
    if not node_def:
        return None
    m = re.match(r"^([A-Za-z0-9_\u4e00-\u9fff]+)\[([^\]]*)\]$", node_def)
    if m:
        return (m.group(1), m.group(2) or m.group(1), "rect")
    m = re.match(r"^([A-Za-z0-9_\u4e00-\u9fff]+)\{([^}]*)\}$", node_def)
    if m:
        return (m.group(1), m.group(2) or m.group(1), "diamond")
    m = re.match(r"^([A-Za-z0-9_\u4e00-\u9fff]+)\(\(([^)]*)\)\)$", node_def)
    if m:
        return (m.group(1), m.group(2) or m.group(1), "circle")
    m = re.match(r"^([A-Za-z0-9_\u4e00-\u9fff]+)\(([^)]*)\)$", node_def)
    if m:
        return (m.group(1), m.group(2) or m.group(1), "rounded")
    m = re.match(r"^([A-Za-z0-9_\u4e00-\u9fff]+)$", node_def)
    if m:
        return (m.group(1), m.group(1), "rect")
    return None


def _fb_flowchart(lines, out_path: str, scale: int) -> str:
    from matplotlib.patches import Circle, FancyBboxPatch, Polygon

    plt = _setup_plt()
    font = _pick_font()

    direction = "LR"
    for ln in lines:
        mm = re.match(r"^(?:flowchart|graph)\s+(LR|RL|TB|BT|TD)\b", ln, re.I)
        if mm:
            direction = mm.group(1).upper()
            if direction == "TD":
                direction = "TB"
            break

    nodes: dict = {}
    edges: list = []
    for ln in lines:
        if re.match(r"^(?:flowchart|graph)\b", ln, re.I):
            continue
        m = re.match(r"^[A-Za-z0-9_\u4e00-\u9fff]+(\[[^\]]*\]|\{[^}]*\}|\(\([^)]*\)\)|\([^)]*\))?$", ln)
        if m:
            parsed = _node_parse(ln)
            if parsed:
                nodes.setdefault(parsed[0], (parsed[1], parsed[2]))
            continue
        m = re.match(
            r"^\s*([A-Za-z0-9_\u4e00-\u9fff]+)(?:\[[^\]]*\]|\{[^}]*\}|"
            r"\(\([^)]*\)\)|\([^)]*\))?\s*(-->|---|-\.->|==>|--|->)\s*"
            r"(?:\|([^|]*)\|)?\s*([A-Za-z0-9_\u4e00-\u9fff]+)"
            r"(?:\[[^\]]*\]|\{[^}]*\}|\(\([^)]*\)\)|\([^)]*\))?", ln)
        if m:
            frm, style, label, to = m.group(1), m.group(2), (m.group(3) or ""), m.group(4)
            edges.append((frm, to, label, style))
            for nid in (frm, to):
                if nid not in nodes:
                    nodes[nid] = (nid, "rect")

    if not nodes:
        return _fb_text("\n".join(lines), "flowchart", out_path, scale)

    order = list(nodes.keys())
    n = len(order)
    cols = min(n, 4 if direction == "TB" else n)
    node_w, node_h, gap = 1.7, 0.8, 1.2
    fig_w = max(4.0, cols * (node_w + gap) + 0.5)
    fig_h = max(2.2, ((n + cols - 1) // cols) * (node_h + gap) + 0.5)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.set_xlim(0, fig_w)
    ax.set_ylim(0, fig_h)
    ax.axis("off")

    pos = {}
    for idx, nid in enumerate(order):
        if direction == "TB":
            r, c = divmod(idx, cols)
            x = 0.25 + c * (node_w + gap)
            y = fig_h - 0.25 - (r + 1) * (node_h + gap) + gap * 0.5
        else:
            x = 0.25 + idx * (node_w + gap)
            y = fig_h / 2
        pos[nid] = (x, y)

    for nid, (label, shape) in nodes.items():
        x, y = pos[nid]
        cx, cy = x, y - node_h / 2
        if shape == "diamond":
            pts = [(x, cy), (x + node_w / 2, y), (x, cy + node_h), (x - node_w / 2, y)]
            ax.add_patch(Polygon(pts, closed=True, fill=False, edgecolor="black"))
        elif shape == "circle":
            ax.add_patch(Circle((x, y), node_h / 2, fill=False, edgecolor="black"))
        elif shape == "rounded":
            ax.add_patch(FancyBboxPatch(
                (x - node_w / 2, cy), node_w, node_h,
                boxstyle="round,pad=0.03", fill=False, edgecolor="black"))
        else:
            ax.add_patch(FancyBboxPatch(
                (x - node_w / 2, cy), node_w, node_h,
                boxstyle="square,pad=0.03", fill=False, edgecolor="black"))
        ax.text(x, y, label, ha="center", va="center",
                fontsize=9, fontname=font)

    for frm, to, label, style in edges:
        if frm not in pos or to not in pos:
            continue
        x1, y1 = pos[frm]
        x2, y2 = pos[to]
        arrowstyle = "-|>" if style in ("-->", "->", "==>") else "-"
        linestyle = "--" if style in ("-.->", "--") else "-"
        ax.annotate("", xy=(x2, y2), xytext=(x1, y1),
                    arrowprops=dict(arrowstyle=arrowstyle,
                                    linestyle=linestyle, color="black"))
        if label:
            ax.text((x1 + x2) / 2, (y1 + y2) / 2 + 0.12, label,
                    ha="center", fontsize=8, fontname=font)

    _save(fig, out_path, scale)
    return "built-in matplotlib 兜底渲染（flowchart 简化版）"


def _fb_sequence(lines, out_path: str, scale: int) -> str:
    plt = _setup_plt()
    font = _pick_font()

    participants: list = []
    msgs: list = []
    for ln in lines:
        m = re.match(r"^(?:participant|actor)\s+([A-Za-z0-9_\u4e00-\u9fff]+)"
                     r"(?:\s+as\s+(.+))?$", ln, re.I)
        if m:
            pid, label = m.group(1), m.group(2) or m.group(1)
            if pid not in [p[0] for p in participants]:
                participants.append((pid, label))
            continue
        m = re.match(
            r"^\s*([A-Za-z0-9_\u4e00-\u9fff]+)\s*(->>|-->>|->|-->)\s*"
            r"([A-Za-z0-9_\u4e00-\u9fff]+)\s*:\s*(.*)$", ln)
        if m:
            frm, to, text = m.group(1), m.group(3), m.group(4)
            for pid in (frm, to):
                if pid not in [p[0] for p in participants]:
                    participants.append((pid, pid))
            msgs.append((frm, to, text))
            continue

    if not participants:
        return _fb_text("\n".join(lines), "sequence", out_path, scale)

    n = len(participants)
    fig_w = max(5.0, n * 2.4)
    fig_h = max(3.0, len(msgs) * 0.8 + 1.6)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.set_xlim(0, fig_w)
    ax.set_ylim(0, fig_h)
    ax.axis("off")

    xpos = {}
    for i, (pid, label) in enumerate(participants):
        x = (1.0 + i * (fig_w - 2.0) / max(1, n - 1)) if n > 1 else fig_w / 2
        xpos[pid] = x
        ax.text(x, fig_h - 0.2, label, ha="center", fontsize=10, fontname=font)
        ax.plot([x, x], [fig_h - 0.55, 0.2], "--", color="gray", lw=0.8)

    for idx, (frm, to, text) in enumerate(msgs):
        y = fig_h - 1.0 - idx * 0.8
        if frm in xpos and to in xpos:
            x1, x2 = xpos[frm], xpos[to]
            ax.annotate("", xy=(x2, y), xytext=(x1, y),
                        arrowprops=dict(arrowstyle="-|>", color="black"))
            ax.text((x1 + x2) / 2, y + 0.08, text,
                    ha="center", fontsize=8, fontname=font)

    _save(fig, out_path, scale)
    return "built-in matplotlib 兜底渲染（时序图简化版）"


def _fb_text(source: str, chart_type: str, out_path: str, scale: int) -> str:
    plt = _setup_plt()
    lines = source.splitlines()
    fig_w, fig_h = 10.0, max(2.0, len(lines) * 0.32 + 1.2)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.text(0.5, 0.97,
            f"[内置兜底渲染] {chart_type} 源码文本图（安装 plantuml/mmdc 可获得完整图表）",
            ha="center", va="top", fontsize=9, color="gray")
    body = "\n".join(lines)
    ax.text(0.03, 0.90, body, va="top", fontsize=7, color="black")
    _save(fig, out_path, scale)
    return "built-in matplotlib 兜底渲染（源码文本图）"


def render_fallback(source: str, chart_type: str, out_path: str, scale: int) -> str:
    """内置兜底渲染入口。失败抛 RuntimeError。"""
    try:
        import matplotlib  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "内置兜底渲染需要 matplotlib（项目主依赖已包含；"
            "或安装 plantuml/mmdc 外部引擎）") from exc

    lines = [ln.strip() for ln in source.splitlines()
             if ln.strip() and not ln.strip().startswith(("'", "//"))]
    first = lines[0].lower() if lines else ""
    if chart_type == "mermaid" and first.startswith(("flowchart", "graph")):
        return _fb_flowchart(lines, out_path, scale)
    if chart_type in ("mermaid", "plantuml") and (
            first.startswith("sequenceDiagram") or "participant" in source or "actor" in source):
        return _fb_sequence(lines, out_path, scale)
    if chart_type == "plantuml" and "->" in source and "@startuml" in source:
        return _fb_sequence(lines, out_path, scale)
    return _fb_text(source, chart_type, out_path, scale)


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("用法: python3 fallback_render.py <源码文件> <plantuml|mermaid> <输出.png> [scale]",
              file=sys.stderr)
        sys.exit(2)
    src = open(sys.argv[1], encoding="utf-8").read()
    engine = render_fallback(src, sys.argv[2], sys.argv[3],
                             int(sys.argv[4]) if len(sys.argv) > 4 else 2)
    print(engine)
