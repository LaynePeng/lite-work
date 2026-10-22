# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""PPT 图形化编辑工具（ppt_edit）：把 ppt-master 的 roundtrip 工作区包装成 Agent 工具。

数据流：pptx --(pptx_to_svg.py --roundtrip)--> 可编辑 SVG 工作区
       可编辑 SVG --(svg_to_pptx.py --roundtrip)--> pptx（未改页字节级 passthrough）

供「PPT 编辑器」页签使用，六个 op：
- open       导入 pptx，建 roundtrip 工作区（中间产物/ppt-edit/<slug>/）
- outline    读每页元素结构（左栏缩略图 + 中区画布 + 右栏面板的数据源）
- slide_svg  取某页 SVG 文本，交给前端内联渲染（href 已重写为工作区相对路径）
- edit       写元素属性 / 文本 / 表格单元格 / 图表数值（确定性改动，不经模型）
- annotate   写自然语言标注（需 Agent 判断的改动），与 ppt-master 的
             data-edit-annotation 约定一致
- export     回写为 pptx

为什么单独成文件而不并入 office.py：office.py 是社区 office-plugin 的同源
副本（AGENTS.md §6），夹带主仓库改动会被用户升级社区插件时覆盖丢失。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple, cast

from litework.core.types import ToolDefinition

logger = logging.getLogger("litework.tools.ppt_edit")

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)

# roundtrip 工作区根目录（相对工作区）；AGENTS.md：中间产物统一进 中间产物/
_WORK_ROOT = os.path.join("中间产物", "ppt-edit")

# ppt-master 用 96 px/inch 把幻灯片英寸尺寸映射到 SVG 画布，
# 故 1pt = 96/72 px = 4/3 px。导出时由脚本反算，面板按 pt 显示。
_PX_PER_PT = 4.0 / 3.0

_SCRIPT_TIMEOUT = 600.0


class PptEditError(RuntimeError):
    """工具层可读错误：直接作为结果文本返回给 Agent。"""


# ---------------------------------------------------------------- skill 定位


def _skill_dir() -> str:
    """定位内置 ppt-master 技能目录（开发态与打包态）。"""
    from .skills import _builtin_skills_dir

    base = _builtin_skills_dir()
    if base:
        cand = os.path.join(str(base), "ppt-master")
        if os.path.isfile(os.path.join(cand, "scripts", "pptx_to_svg.py")):
            return cand
    meipass = getattr(sys, "_MEIPASS", None)
    for root in (meipass, os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else None):
        if not root:
            continue
        for sub in ("skills", os.path.join("_internal", "skills")):
            cand = os.path.join(root, sub, "ppt-master")
            if os.path.isfile(os.path.join(cand, "scripts", "pptx_to_svg.py")):
                return cand
    raise PptEditError(
        "找不到内置 ppt-master 技能（skills/ppt-master）。"
        "请确认安装包完整，或重新安装 lite-work。"
    )


def _skill_python() -> str:
    """选择跑技能脚本的解释器。

    打包态**绝不能**用 sys.executable：frozen 后它指向 lite-work 后端主程序，
    拼上脚本路径等于把 .py 当成子命令传给它，直接报
    `invalid choice: '.../pptx_to_svg.py'`。优先用随包分发的技能解释器
    （skills.py 的既有约定），开发态再退回当前解释器。
    """
    from .skills import _builtin_skill_python

    exe = _builtin_skill_python()
    if exe is not None:
        return str(exe)
    if getattr(sys, "frozen", False):
        raise PptEditError(
            "安装包缺少内置技能解释器（skill-python），无法运行 PPT 转换脚本。"
            "请重新安装完整版本的 lite-work。"
        )
    return sys.executable


def _run_script(script: str, args: List[str], cwd: Optional[str] = None) -> Tuple[int, str]:
    """跑 ppt-master 脚本，返回 (returncode, 输出)。超时杀整棵进程树。"""
    from .proc_utils import kill_process_tree, posix_spawn_kwargs

    skill = _skill_dir()
    path = os.path.join(skill, "scripts", script)
    if not os.path.isfile(path):
        raise PptEditError(f"技能脚本缺失: {script}")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.Popen(
        [_skill_python(), path] + args,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        cwd=cwd or skill, env=env, **posix_spawn_kwargs(),
    )
    try:
        out, _ = proc.communicate(timeout=_SCRIPT_TIMEOUT)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc.pid)
        proc.wait(timeout=10)
        raise PptEditError(f"{script} 超时（{int(_SCRIPT_TIMEOUT)}s），已终止")
    text = (out or b"").decode("utf-8", errors="replace")
    return proc.returncode, text


def _tail(text: str, limit: int = 2500) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


# ---------------------------------------------------------------- SVG 解析


def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _frame_of(elem: ET.Element) -> Optional[List[float]]:
    raw = (elem.get("data-pptx-frame") or "").strip()
    if not raw:
        return None
    try:
        parts = [float(x) for x in re.split(r"[\s,]+", raw)]
    except ValueError:
        return None
    return parts if len(parts) == 4 else None


def _num(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    m = re.match(r"\s*(-?[\d.]+)", value)
    try:
        return float(m.group(1)) if m else None
    except ValueError:
        return None


def _text_elements(group: ET.Element) -> List[ET.Element]:
    return [e for e in group.iter() if _local(e.tag) == "text"]


def _para_nodes(text_el: ET.Element) -> List[ET.Element]:
    """取 <text> 下的「段落」节点。

    ppt-master 的段落模型：外层 <tspan> = 段落，内层 <tspan> = run（含项目符号）。
    没有外层 tspan 时 <text> 自身即唯一段落。
    """
    outer = [c for c in list(text_el) if _local(c.tag) == "tspan"]
    return outer or [text_el]


def _para_text(node: ET.Element) -> str:
    return "".join(node.itertext())


def _paragraphs(group: ET.Element) -> List[str]:
    out: List[str] = []
    for t in _text_elements(group):
        for node in _para_nodes(t):
            out.append(_para_text(node))
    return out


def _first_text(group: ET.Element) -> Optional[ET.Element]:
    texts = _text_elements(group)
    return texts[0] if texts else None


def _element_info(elem: ET.Element, index: int) -> Optional[Dict[str, Any]]:
    """把一个 SVG 顶层对象描述成前端可用的结构。"""
    eid = elem.get("id")
    obj = elem.get("data-pptx-object")
    if eid is None and obj is None:
        return None
    frame = _frame_of(elem)
    info: Dict[str, Any] = {
        "id": eid or f"_auto_{index}",
        "kind": obj or "shape",
        "semantic": elem.get("data-pptx-semantic-object") or "",
        "frame": frame,
        "placeholder": elem.get("data-ph-type") or elem.get("data-pptx-placeholder-index") or "",
        "annotation": elem.get("data-edit-annotation") or "",
    }
    # 图片在 ppt-master 里是 <g data-pptx-object="picture"> 包一个 <image>：
    # 元素 id 在外层 g 上，href 在内层（两者都要认，否则面板拿不到文件、改不到图）。
    # 判定必须带 data-pptx-object="picture"：表格/图表的 graphic-frame 里也有
    # 预览用 <image>，只按「含 image 子节点」判断会把它们误认成图片元素。
    is_picture = (_local(elem.tag) == "image"
                  or elem.get("data-pptx-object") == "picture")
    img = elem if _local(elem.tag) == "image" else (
        next((c for c in elem.iter() if _local(c.tag) == "image"), None)
        if is_picture else None)
    if img is not None:
        info.update({"text": [], "font_size": None, "fill": None, "align": None,
                     "image": img.get("href") or img.get("{%s}href" % XLINK_NS) or ""})
        return info
    info["text"] = _paragraphs(elem)
    t = _first_text(elem)
    info["font_size"] = _num(t.get("font-size")) if t is not None else None
    info["fill"] = (t.get("fill") or "") if t is not None else ""
    info["align"] = (t.get("text-anchor") or "") if t is not None else ""

    meta = None
    for child in elem.iter():
        if _local(child.tag) == "metadata" and (child.text or "").strip():
            try:
                meta = json.loads(child.text or "")
            except json.JSONDecodeError:
                meta = None
            break
    if isinstance(meta, dict):
        if elem.get("data-pptx-replace-with") == "table" or "rows" in meta:
            info["table"] = meta
        elif "series" in meta:
            info["chart"] = meta
    return info


def _load_slide(path: str) -> Tuple[Any, ET.Element]:
    """解析一页 SVG。ElementTree 的泛型参数在 typeshed 里是可选类型，
    这里统一收敛为 Any，避免调用点被 str|None 污染（运行时无差异）。"""
    tree: Any = ET.parse(path)
    root = cast(ET.Element, tree.getroot())
    return tree, root


def _find_by_id(root: ET.Element, eid: str) -> Optional[ET.Element]:
    for elem in root.iter():
        if elem.get("id") == eid:
            return elem
    return None


# ---------------------------------------------------------------- 工具类


class PptEditTools:
    """ppt_edit 工具实现。"""

    def __init__(self, workspace: Optional[str]) -> None:
        self.workspace = os.path.abspath(workspace) if workspace else os.path.expanduser("~")

    # ------------------------------------------------------------ 工具定义

    def get_tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="ppt_edit",
                description=(
                    "图形化编辑已有 PowerPoint：导入 .pptx 建可编辑工作区，"
                    "按元素改文字/字号/颜色/位置、改表格单元格与图表数值，"
                    "或给元素写自然语言标注由你判断执行，最后回写为新 pptx。"
                    "未改动的页在导出时字节级保留（设计不变形）。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "op": {
                            "type": "string",
                            "enum": ["open", "outline", "slide_svg", "thumbs", "edit",
                                     "annotate", "annotations", "export",
                                     "templates", "create_from_template", "extract",
                                     "add_slide", "remove_slide", "change_layout",
                                     "get_notes", "set_note"],
                            "description": "操作类型",
                        },
                        "path": {
                            "type": "string",
                            "description": "op=open：源 pptx 路径（相对工作区）",
                        },
                        "project": {
                            "type": "string",
                            "description": "工作区名（op=open 的返回）；其余 op 必填",
                        },
                        "slide": {
                            "type": "integer",
                            "description": "页码（从 1 开始）",
                        },
                        "element_id": {
                            "type": "string",
                            "description": "元素 id（来自 outline）",
                        },
                        "changes": {
                            "type": "object",
                            "description": (
                                "op=edit 的改动：text=段落数组；font_size=磅值(pt)；"
                                "fill=颜色；x/y/width/height=画布像素；"
                                "table_cell={row,col,text}；"
                                "chart_value={series,index,value}；"
                                "image=新图片路径(相对工作区)"
                            ),
                        },
                        "note": {
                            "type": "string",
                            "description": "op=annotate：对该元素的自然语言修改要求",
                        },
                        "output": {
                            "type": "string",
                            "description": "op=export：输出 pptx 路径（默认源文件同名 _edited）",
                        },
                        "reimport": {
                            "type": "boolean",
                            "description": "op=open：已有工作区时是否丢弃改动重新导入（默认 false=沿用）",
                        },
                        "template": {
                            "type": "object",
                            "description": (
                                "op=create_from_template："
                                "{suite, brand, style, pages:[{page, values:{占位符:值}}]}；"
                                "先用 op=templates 查可用套件与占位符"
                            ),
                        },
                        "filename": {
                            "type": "string",
                            "description": "op=create_from_template：输出 pptx 文件名（默认按套件命名）",
                        },
                        "restyled_from": {
                            "type": "string",
                            "description": "op=create_from_template：由哪份 pptx 重排而来（仅用于结果标注）",
                        },
                    },
                    "required": ["op"],
                },
            ),
        ]

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        import asyncio as _asyncio

        if name != "ppt_edit":
            raise ValueError(f"Unknown tool: {name}")
        return await _asyncio.to_thread(self._dispatch, args)

    def _dispatch(self, args: Dict[str, Any]) -> str:
        op = str(args.get("op") or "").strip()
        try:
            handler = {
                "open": self._op_open,
                "outline": self._op_outline,
                "slide_svg": self._op_slide_svg,
                "thumbs": self._op_thumbs,
                "edit": self._op_edit,
                "annotate": self._op_annotate,
                "annotations": self._op_annotations,
                "export": self._op_export,
                "templates": self._op_templates,
                "create_from_template": self._op_create_from_template,
                "extract": self._op_extract,
                "add_slide": self._op_add_slide,
                "remove_slide": self._op_remove_slide,
                "change_layout": self._op_change_layout,
                "get_notes": self._op_get_notes,
                "set_note": self._op_set_note,
            }.get(op)
            if handler is None:
                return f"[PPT Error]: 未知 op：{op}"
            return handler(args)
        except PptEditError as exc:
            return f"[PPT Error]: {exc}"
        except Exception as exc:  # noqa: BLE001 - 工具边界：错误文本回给模型
            logger.exception("ppt_edit op=%s 失败", op)
            return f"[PPT Error]: {op} 执行失败: {exc}"

    # ------------------------------------------------------------ 路径辅助

    def _resolve(self, rel: str) -> str:
        resolved = os.path.abspath(
            rel if os.path.isabs(rel) else os.path.join(self.workspace, rel)
        )
        if not (resolved == self.workspace or resolved.startswith(self.workspace + os.sep)):
            raise PptEditError(f"路径越界：仅支持工作区内文件: {rel}")
        return resolved

    def _project_dir(self, project: str) -> str:
        name = os.path.basename((project or "").strip().rstrip("/\\"))
        if not name:
            raise PptEditError("缺少 project 参数（op=open 的返回值）")
        d = os.path.join(self.workspace, _WORK_ROOT, name)
        if not os.path.isdir(os.path.join(d, "authoring-svg-flat")):
            raise PptEditError(f"工作区不存在或未导入完成: {name}")
        return d

    def _slide_path(self, project_dir: str, slide: int) -> str:
        p = os.path.join(project_dir, "authoring-svg-flat", f"slide_{slide:02d}.svg")
        if not os.path.isfile(p):
            raise PptEditError(f"第 {slide} 页 SVG 不存在")
        return p

    def _rel(self, abs_path: str) -> str:
        return os.path.relpath(abs_path, self.workspace).replace(os.sep, "/")

    # ------------------------------------------------------------ op: open

    def _op_open(self, args: Dict[str, Any]) -> str:
        rel = str(args.get("path") or "").strip()
        if not rel:
            raise PptEditError("op=open 缺少 path")
        src = self._resolve(rel)
        if not os.path.isfile(src):
            raise PptEditError(f"文件不存在: {rel}")
        if not src.lower().endswith(".pptx"):
            raise PptEditError("仅支持 .pptx（.ppt 请先用 office_convert 转成 .pptx）")

        slug = re.sub(r"[^\w.-]+", "_", os.path.splitext(os.path.basename(src))[0])[:60]
        out = os.path.join(self.workspace, _WORK_ROOT, slug)
        if os.path.isdir(out):
            # 已有工作区不静默重建：里面可能躺着用户还没导出的编辑。
            # 只有显式 reimport=true 才丢弃重来（前端「重新导入」用）。
            if not args.get("reimport"):
                return json.dumps({
                    "ok": True,
                    "project": slug,
                    "source": rel,
                    "slides": self._slide_count(out),
                    "reused": True,
                    "message": "已存在该演示文稿的工作区，沿用其中的编辑"
                               "（如需丢弃改动重新导入，传 reimport=true）",
                }, ensure_ascii=False, indent=2)
            shutil.rmtree(out, ignore_errors=True)
        os.makedirs(os.path.dirname(out), exist_ok=True)

        code, log = _run_script("pptx_to_svg.py", [
            src, "-o", out, "--inheritance-mode", "both", "--roundtrip",
        ])
        if code != 0 or not os.path.isdir(os.path.join(out, "authoring-svg-flat")):
            raise PptEditError(f"导入失败（exit={code}）:\n{_tail(log)}")

        return json.dumps({
            "ok": True,
            "project": slug,
            "source": rel,
            "slides": self._slide_count(out),
            "log": _tail(log, 600),
        }, ensure_ascii=False, indent=2)

    def _slide_count(self, project_dir: str) -> int:
        d = os.path.join(project_dir, "authoring-svg-flat")
        return len([f for f in os.listdir(d)
                    if re.fullmatch(r"slide_\d+\.svg", f)])

    # ------------------------------------------------------------ op: outline

    def _op_outline(self, args: Dict[str, Any]) -> str:
        project_dir = self._project_dir(str(args.get("project") or ""))
        total = self._slide_count(project_dir)
        want = args.get("slide")
        slides: List[Dict[str, Any]] = []
        for n in range(1, total + 1):
            if want is not None and int(want) != n:
                continue
            _, root = _load_slide(self._slide_path(project_dir, n))
            vb = (root.get("viewBox") or "").split()
            elements: List[Dict[str, Any]] = []
            for i, child in enumerate(list(root)):
                if _local(child.tag) == "rect" and child.get("id") is None:
                    continue  # 背景
                info = _element_info(child, i)
                if info:
                    elements.append(info)
            slides.append({
                "slide": n,
                "viewBox": [float(x) for x in vb] if len(vb) == 4 else None,
                "elements": elements,
            })
        return json.dumps({
            "ok": True,
            "project": os.path.basename(project_dir),
            "total": total,
            "slides": slides,
        }, ensure_ascii=False)

    # ------------------------------------------------------------ op: slide_svg

    def _op_slide_svg(self, args: Dict[str, Any]) -> str:
        project_dir = self._project_dir(str(args.get("project") or ""))
        slide = int(args.get("slide") or 0)
        if slide < 1:
            raise PptEditError("op=slide_svg 需要 slide（从 1 开始）")
        path = self._slide_path(project_dir, slide)
        with open(path, "r", encoding="utf-8") as fh:
            svg = fh.read()
        # 相对 href（../images/x.png）在浏览器里会按页面 origin 解析而断链，
        # 后端知道文件系统落点，统一改写成工作区相对路径；
        # 前端再走既有的 rewriteLocalUrls 转成 /api/files/raw。
        svg = self._rewrite_hrefs(svg, path)
        return json.dumps({
            "ok": True, "slide": slide, "svg": svg,
            "path": self._rel(path),
        }, ensure_ascii=False)

    def _rewrite_hrefs(self, svg: str, svg_path: str) -> str:
        def repl(m: re.Match[str]) -> str:
            attr, url = m.group(1), m.group(2)
            if re.match(r"^(https?:|data:|/api/|#)", url):
                return m.group(0)
            target = os.path.normpath(os.path.join(os.path.dirname(svg_path), url))
            if not os.path.isfile(target):
                return m.group(0)
            return f'{attr}="{self._rel(target)}"'

        return re.sub(
            r'(href|xlink:href)\s*=\s*"([^"]*)"', repl, svg,
        )

    # ------------------------------------------------------------ op: edit

    def _op_edit(self, args: Dict[str, Any]) -> str:
        project_dir = self._project_dir(str(args.get("project") or ""))
        slide = int(args.get("slide") or 0)
        eid = str(args.get("element_id") or "").strip()
        changes = args.get("changes") or {}
        if slide < 1 or not eid:
            raise PptEditError("op=edit 需要 slide 与 element_id")
        if not isinstance(changes, dict) or not changes:
            raise PptEditError("op=edit 需要非空 changes")

        path = self._slide_path(project_dir, slide)
        tree, root = _load_slide(path)
        elem = _find_by_id(root, eid)
        if elem is None:
            raise PptEditError(f"第 {slide} 页找不到元素 {eid}")

        applied: List[str] = []
        self._apply_text(elem, changes, applied)
        self._apply_text_style(elem, changes, applied)
        self._apply_frame(elem, changes, applied)
        self._apply_table(elem, changes, applied)
        self._apply_chart(elem, changes, applied)
        self._apply_image(elem, changes, applied, project_dir)

        if not applied:
            raise PptEditError(
                "没有可应用的改动。可用键：text / font_size / fill / align / "
                "x / y / width / height / table_cell / chart_value / image"
            )
        self._flatten_picture_group(root, elem)
        tree.write(path, encoding="utf-8", xml_declaration=False)
        self._sync_previews(project_dir)
        # 扁平化后元素本身可能已被替换（g → image），回读当前页拿最新结构
        _, fresh_root = _load_slide(path)
        cur = _find_by_id(fresh_root, eid)
        info = _element_info(cur, 0) if cur is not None else None
        return json.dumps({
            "ok": True, "slide": slide, "element_id": eid,
            "applied": applied, "element": info,
        }, ensure_ascii=False, indent=2)

    @staticmethod
    def _set_para(node: ET.Element, value: str) -> None:
        """把段落节点的可见文字替换为 value，保留项目符号等前缀 run。"""
        inner = [c for c in list(node) if _local(c.tag) == "tspan"]
        if inner:
            target = inner[-1]
            # 清掉目标 run 的所有子节点，写入纯文本
            for sub in list(target):
                target.remove(sub)
            target.text = value
            for extra in inner[:-1]:
                pass  # 前缀 run（项目符号）原样保留
        else:
            node.text = value

    def _apply_text(self, elem: ET.Element, changes: Dict[str, Any],
                    applied: List[str]) -> None:
        new_text = changes.get("text")
        if new_text is None:
            return
        if isinstance(new_text, str):
            new_text = [new_text]
        if not isinstance(new_text, list):
            raise PptEditError("text 需为字符串或字符串数组")
        texts = _text_elements(elem)
        if not texts:
            raise PptEditError("该元素没有文字内容")
        # 全部段落按顺序写入（数组长度可少于段数：只改前 N 段）
        all_paras: List[ET.Element] = []
        for t in texts:
            all_paras.extend(_para_nodes(t))
        for node, value in zip(all_paras, new_text):
            self._set_para(node, str(value))
        applied.append(f"text({len(new_text)} 段)")

    def _apply_text_style(self, elem: ET.Element, changes: Dict[str, Any],
                          applied: List[str]) -> None:
        texts = _text_elements(elem)
        pt = changes.get("font_size")
        if pt is not None and texts:
            px = float(pt) * _PX_PER_PT
            for t in texts:
                t.set("font-size", f"{px:.2f}")
            applied.append(f"font_size({float(pt):g}pt)")

        fill = changes.get("fill")
        if fill is not None and texts:
            for t in texts:
                t.set("fill", str(fill))
            applied.append(f"fill({fill})")

        align = changes.get("align")
        if align in ("left", "center", "right") and texts:
            anchor = {"left": "start", "center": "middle", "right": "end"}[align]
            frame = _frame_of(elem)
            for t in texts:
                t.set("text-anchor", anchor)
                # x 必须跟着 anchor 走：SVG 的 anchor 决定「哪个点对齐 x」，
                # 只改 anchor 不改 x，左对齐的文字会从原中线起排、右对齐会跑出框外。
                # 文本框内边距沿用 ppt-master 的首行缩进（约 0.1 英寸 = 9.6px）。
                if frame is not None:
                    fx, _fy, fw, _fh = frame
                    pad = 9.6
                    if align == "left":
                        t.set("x", f"{fx + pad:.15g}")
                    elif align == "right":
                        t.set("x", f"{fx + fw - pad:.15g}")
                    else:
                        t.set("x", f"{fx + fw / 2:.15g}")
                elif align == "left":
                    t.set("x", "0")
            applied.append(f"align({align})")

    def _apply_frame(self, elem: ET.Element, changes: Dict[str, Any],
                     applied: List[str]) -> None:
        frame = _frame_of(elem)
        if frame is None:
            return
        keys = ["x", "y", "width", "height"]
        if not any(k in changes for k in keys):
            return
        new: List[float] = list(frame)
        for i, k in enumerate(keys):
            if k in changes and changes[k] is not None:
                try:
                    new[i] = float(changes[k])
                except (TypeError, ValueError):
                    raise PptEditError(f"{k} 需为数字")
        if new[2] <= 0 or new[3] <= 0:
            raise PptEditError("width / height 必须大于 0")
        elem.set("data-pptx-frame", " ".join(f"{v:.15g}" for v in new))
        # 文字定位点依赖文本框几何：挪框/改宽度后必须按当前 anchor 重算 x，
        # 否则文字会留在旧位置（改宽时表现为「框变宽了但字没跟着走」）。
        texts = _text_elements(elem)
        if texts:
            anchor = texts[0].get("text-anchor") or "start"
            pad = 9.6
            if anchor == "middle":
                tx = new[0] + new[2] / 2
            elif anchor == "end":
                tx = new[0] + new[2] - pad
            else:
                tx = new[0] + pad
            for t in texts:
                t.set("x", f"{tx:.15g}")
        # 图片几何由 <image> 自身的 x/y/width/height 驱动（data-pptx-frame 只是
        # 元信息），不同步就出现「面板显示改了、导出的位置没动」。
        img = elem if _local(elem.tag) == "image" else next(
            (c for c in elem.iter() if _local(c.tag) == "image"), None)
        if img is not None:
            img.set("x", f"{new[0]:.15g}")
            img.set("y", f"{new[1]:.15g}")
            img.set("width", f"{new[2]:.15g}")
            img.set("height", f"{new[3]:.15g}")
        dirs = [k for k in keys if k in changes]
        applied.append("frame(" + ",".join(dirs) + ")")

    def _flatten_picture_group(self, root: ET.Element, elem: ET.Element) -> None:
        """把 <g data-pptx-object="picture"> 的属性提升到内层 <image> 并去掉该 g。

        为什么必须做：导出器按元素标签决定产出——`<g>` 会变成 `<p:grpSp>`，
        内层图片只是组内一个 `<p:pic>`，PowerPoint 里图片外面多一层组合框，
        选中/裁剪/替换等操作都变别扭。属性提到 `<image>` 上（无 g 包裹）时
        还原为原生 `<p:pic>`，保真度回到与源文件一致。
        """
        if _local(elem.tag) != "g":
            return
        if elem.get("data-pptx-object") != "picture":
            return
        img = next((c for c in elem.iter() if _local(c.tag) == "image"), None)
        if img is None:
            return
        for key, value in elem.attrib.items():
            if key not in img.attrib:
                img.set(key, value)
        # 用提升后的 image 替换 g 在父节点中的位置
        for parent in root.iter():
            for i, child in enumerate(list(parent)):
                if child is elem:
                    parent.remove(elem)
                    parent.insert(i, img)
                    return

    def _find_metadata(self, elem: ET.Element) -> Optional[ET.Element]:
        for child in elem.iter():
            if _local(child.tag) == "metadata":
                return child
        return None

    def _apply_table(self, elem: ET.Element, changes: Dict[str, Any],
                     applied: List[str]) -> None:
        cell = changes.get("table_cell")
        if not isinstance(cell, dict):
            return
        meta_el = self._find_metadata(elem)
        if meta_el is None:
            raise PptEditError("该元素不是表格")
        try:
            meta = json.loads(meta_el.text or "{}")
            row, col = int(cell["row"]), int(cell["col"])
            rows = meta["rows"]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise PptEditError(f"table_cell 参数无效: {exc}")
        if not (0 <= row < len(rows)) or not (0 <= col < len(rows[row])):
            raise PptEditError(f"单元格越界：表格为 {len(rows)} 行")
        rows[row][col]["text"] = str(cell.get("text", ""))
        meta_el.text = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
        applied.append(f"table_cell({row},{col})")

    def _apply_chart(self, elem: ET.Element, changes: Dict[str, Any],
                     applied: List[str]) -> None:
        cv = changes.get("chart_value")
        if not isinstance(cv, dict):
            return
        meta_el = self._find_metadata(elem)
        if meta_el is None:
            raise PptEditError("该元素不是图表")
        try:
            meta = json.loads(meta_el.text or "{}")
            si, idx = int(cv["series"]), int(cv["index"])
            value = float(cv["value"])
            series = meta["series"]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise PptEditError(f"chart_value 参数无效: {exc}")
        if not (0 <= si < len(series)):
            raise PptEditError(f"系列越界：共 {len(series)} 个系列")
        values = series[si]["values"]
        if not (0 <= idx < len(values)):
            raise PptEditError(f"数据点越界：该系列有 {len(values)} 个值")
        values[idx] = value
        meta_el.text = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
        applied.append(f"chart_value(s{si},#{idx})")

    def _apply_image(self, elem: ET.Element, changes: Dict[str, Any],
                     applied: List[str], project_dir: str) -> None:
        rel = changes.get("image")
        if not rel:
            return
        # 图片元素可能是外层 <g>（id 在它身上），href 在内层 <image>
        img = elem if _local(elem.tag) == "image" else next(
            (c for c in elem.iter() if _local(c.tag) == "image"), None)
        if img is None:
            raise PptEditError("该元素不是图片")
        target = self._resolve(str(rel))
        if not os.path.isfile(target):
            raise PptEditError(f"图片不存在: {rel}")
        # 复制进工作区 images/，导出时素材自洽；href 保持相对 images/ 的写法
        dest_dir = os.path.join(project_dir, "images")
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, os.path.basename(target))
        if os.path.abspath(target) != os.path.abspath(dest):
            shutil.copyfile(target, dest)
        img.set("href", f"../images/{os.path.basename(dest)}")
        applied.append(f"image({os.path.basename(str(rel))})")

    def _sync_previews(self, project_dir: str) -> None:
        """改完刷新 authoring summary；失败不阻断（导出会自行重算）。"""
        code, log = _run_script("svg_authoring_view.py",
                                [os.path.join(project_dir, "authoring-svg-flat"),
                                 "--refresh-summary"])
        if code != 0:
            logger.debug("刷新 authoring summary 失败（忽略）: %s", _tail(log, 300))

    # ------------------------------------------------------------ op: thumbs

    def _op_thumbs(self, args: Dict[str, Any]) -> str:
        """批量返回每页的缩略 SVG（左栏预览用）。

        与 slide_svg 的区别：这里刻意做**瘦身**——去掉大体积图片，只留文字
        与色块。左栏缩略图只有一百多像素宽，原尺寸图片既浪费带宽也没意义；
        用户真正需要看清画面时点进去用 slide_svg 拿完整版。
        """
        project_dir = self._project_dir(str(args.get("project") or ""))
        slides: List[Dict[str, Any]] = []
        for n in range(1, self._slide_count(project_dir) + 1):
            path = self._slide_path(project_dir, n)
            with open(path, "r", encoding="utf-8") as fh:
                svg = fh.read()
            # 图片替换为同尺寸浅色占位块，保留版面结构
            svg = re.sub(
                r"<image\b[^>]*?\bx=\"([-\d.]+)\"[^>]*?\by=\"([-\d.]+)\""
                r"[^>]*?\bwidth=\"([\d.]+)\"[^>]*?\bheight=\"([\d.]+)\"[^>]*/>",
                lambda m: (f'<rect x="{m.group(1)}" y="{m.group(2)}" '
                           f'width="{m.group(3)}" height="{m.group(4)}" '
                           f'fill="#E2E8F0" stroke="#CBD5E1"/>'),
                svg,
            )
            svg = re.sub(r"<image\b[^>]*/>", "", svg)
            vb = re.search(r'viewBox="([^"]+)"', svg)
            slides.append({
                "slide": n,
                "viewBox": vb.group(1) if vb else "0 0 1280 720",
                "svg": svg,
            })
        return json.dumps({"ok": True, "slides": slides}, ensure_ascii=False)

    # ------------------------------------------------------------ op: annotate

    def _op_annotate(self, args: Dict[str, Any]) -> str:
        project_dir = self._project_dir(str(args.get("project") or ""))
        slide = int(args.get("slide") or 0)
        eid = str(args.get("element_id") or "").strip()
        note = str(args.get("note") or "").strip()
        if slide < 1 or not eid or not note:
            raise PptEditError("op=annotate 需要 slide / element_id / note")
        path = self._slide_path(project_dir, slide)
        tree, root = _load_slide(path)
        elem = _find_by_id(root, eid)
        if elem is None:
            raise PptEditError(f"第 {slide} 页找不到元素 {eid}")
        if note == "__clear__":
            elem.attrib.pop("data-edit-target", None)
            elem.attrib.pop("data-edit-annotation", None)
            msg = "已清除标注"
        else:
            elem.set("data-edit-target", "true")
            elem.set("data-edit-annotation", note)
            msg = "已添加标注"
        tree.write(path, encoding="utf-8", xml_declaration=False)
        return json.dumps({"ok": True, "slide": slide, "element_id": eid,
                           "message": msg}, ensure_ascii=False)

    def _op_annotations(self, args: Dict[str, Any]) -> str:
        project_dir = self._project_dir(str(args.get("project") or ""))
        items: List[Dict[str, Any]] = []
        for n in range(1, self._slide_count(project_dir) + 1):
            _, root = _load_slide(self._slide_path(project_dir, n))
            for elem in root.iter():
                if elem.get("data-edit-target") == "true":
                    items.append({
                        "slide": n,
                        "element_id": elem.get("id") or "",
                        "note": elem.get("data-edit-annotation") or "",
                        "text": _paragraphs(elem)[:3],
                    })
        return json.dumps({"ok": True, "count": len(items), "items": items},
                          ensure_ascii=False, indent=2)

    # ------------------------------------------------------------ op: export

    def _op_export(self, args: Dict[str, Any]) -> str:
        project_dir = self._project_dir(str(args.get("project") or ""))
        default = os.path.splitext(os.path.basename(project_dir))[0] + "_edited.pptx"
        out_rel = str(args.get("output") or default).strip()
        out = out_rel if os.path.isabs(out_rel) else os.path.join(self.workspace, out_rel)
        if not (out == self.workspace or out.startswith(self.workspace + os.sep)):
            raise PptEditError(f"输出路径越界: {out_rel}")
        fmt = str(args.get("format") or "pptx").strip().lower()
        if fmt not in ("pptx", "pdf"):
            raise PptEditError(f"不支持的导出格式：{fmt}（可选 pptx / pdf）")
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)

        # 增删页/换版式后 roundtrip 三层校验必然失配（页结构变了）。
        # 处理：结构操作打标记 → 这里看到标记就改走 flat 导出，并在结果里
        # 说明「字节级 passthrough 已失效」。内容不丢，只是不再承诺
        # 未改页逐字节一致。
        restructured = os.path.isfile(
            os.path.join(project_dir, ".litework-restructured"))
        export_args = [project_dir, "-o", out]
        if restructured:
            export_args.append("--native-charts-and-tables")
        else:
            export_args += ["--roundtrip", "--native-charts-and-tables"]
        code, log = _run_script("svg_to_pptx.py", export_args)
        if code != 0 or not os.path.isfile(out):
            raise PptEditError(f"导出失败（exit={code}）:\n{_tail(log)}")
        extra = {}
        if fmt == "pdf":
            extra = self._export_pdf(out)
            if extra.get("pdf"):
                out = extra["pdf"]
        m = re.search(r"Round-trip export summary:\s*(.+)", log)
        result: Dict[str, Any] = {
            "ok": True,
            "output": self._rel(out),
            "format": fmt,
            "summary": m.group(1).strip() if m else "",
            # 结构变更过的 deck 导出后，原工作区的 roundtrip 身份已失效
            "restructured": restructured,
            "log": _tail(log, 800),
        }
        # pdf 失败：pptx 已落盘，错误带回不抛（用户还有 pptx 可用）
        result.update({k: v for k, v in extra.items() if k.startswith("pdf")})
        return json.dumps(result, ensure_ascii=False, indent=2)

    def _export_pdf(self, pptx_path: str) -> Dict[str, str]:
        """pptx → pdf（LibreOffice，与 office_convert 同一套封装）。

        返回 {"pdf": 路径} 或 {"pdf_error": 原因}——pdf 失败不让 pptx 导出
        作废（pptx 已经落盘），错误原样带回让用户决定要不要重试。
        """
        from .office import _SofficeMissing, _run_soffice

        try:
            with tempfile.TemporaryDirectory(prefix="litework-ppt-pdf-") as tmpdir:
                rc, out_b, err_b = _run_soffice(
                    ["--convert-to", "pdf", "--outdir", tmpdir, pptx_path],
                    timeout=180)
                if rc != 0:
                    detail = (err_b or b"").decode("utf-8", "replace").strip()[:300]
                    return {"pdf_error": f"PDF 转换失败: {detail or '无错误输出'}"}
                stem = os.path.splitext(os.path.basename(pptx_path))[0]
                produced = os.path.join(tmpdir, f"{stem}.pdf")
                if not os.path.isfile(produced):
                    cands = [f for f in os.listdir(tmpdir) if f.endswith(".pdf")]
                    if not cands:
                        return {"pdf_error": "PDF 转换没有产出文件"}
                    produced = os.path.join(tmpdir, cands[0])
                final = os.path.splitext(pptx_path)[0] + ".pdf"
                shutil.move(produced, final)
                return {"pdf": final}
        except _SofficeMissing:
            return {"pdf_error": "本机未安装 LibreOffice，无法转 PDF"
                                 "（office_convert 的安装指引适用）"}
        except RuntimeError as exc:
            return {"pdf_error": str(exc)}

    # ------------------------------------------------------------ op: templates

    def _op_templates(self, args: Dict[str, Any]) -> str:
        """列出可用版式套件 / 品牌 / 风格。

        三者的用途不同（见 ppt_templates 模块头）：layouts 可直接填充出稿，
        brands 提供主色，styles 是给 Agent 的设计规范上下文。
        """
        from . import ppt_templates as tpl

        avail = tpl.availability()
        return json.dumps({
            "ok": True,
            "layouts": tpl.list_layouts() if avail["ready"] else [],
            "brands": tpl.list_brands() if avail["ready"] else [],
            "styles": tpl.list_styles() if avail["ready"] else [],
            # 前端据此区分「模板库为空」与「技能没装好」——前者是数据问题，
            # 后者要提示用户修安装，两者的界面反馈完全不同。
            "available": avail,
        }, ensure_ascii=False)

    # ------------------------------------------------------------ op: create_from_template

    def _op_create_from_template(self, args: Dict[str, Any]) -> str:
        """按模板填充并生成 pptx，再以 roundtrip 工作区打开供后续微调。

        参数形如：
            {"suite": "presentation_core",
             "brand": "mckinsey", "style": "mbb-consulting",
             "pages": [{"page": "01_title_slide.svg",
                        "values": {"TITLE": "...", "SUBTITLE": "..."}}]}

        `page` 可写带序号的文件名，也可只写 slug（如 "title_slide"），
        后者按后缀匹配模板文件——用户更常说「要一页封面」而不是文件名。
        """
        from . import ppt_templates as tpl

        spec = args.get("template") or {}
        if not isinstance(spec, dict):
            raise PptEditError("template 需为对象")
        suite = str(spec.get("suite") or "").strip()
        pages = spec.get("pages")
        if not suite:
            raise PptEditError("template.suite 必填（先用 op=templates 查可用套件）")
        if not isinstance(pages, list) or not pages:
            raise PptEditError("template.pages 需为非空数组")

        # 先确认技能可用：否则下面的 resolve 会回「未知版式套件」，
        # 把「没装好」误报成「你写错了套件名」，Agent 会照着去查 templates 白绕一圈。
        avail = tpl.availability()
        if not avail["ready"]:
            raise PptEditError(
                f"{avail['reason']}\n{avail['hint']}"
                "\n（ppt-master 是随安装包分发的内置技能，无法通过安装插件补齐）")

        # 同一套件重复生成时换目录名，避免覆盖用户已有的编辑
        base_slug = re.sub(r"[^\w.-]+", "_", suite)[:50]
        out = os.path.join(self.workspace, _WORK_ROOT, base_slug)
        n = 2
        while os.path.isdir(out):
            out = os.path.join(self.workspace, _WORK_ROOT, f"{base_slug}_{n}")
            n += 1
        os.makedirs(out, exist_ok=True)

        svg_dir = os.path.join(out, "svg_output")
        os.makedirs(svg_dir, exist_ok=True)

        warnings: List[str] = []
        # 容量预警：生成前就能知道哪页文案放不下（上游 slot_capacity 报告）。
        # 与排版引擎的自动缩排互补——缩排兜底，预警让用户提前知道该精简。
        try:
            warnings.extend(tpl.capacity_warnings(suite, pages))
        except Exception:            # 预警是增强项：失败不能影响生成
            logger.debug("容量预警失败（忽略）", exc_info=True)
        viewbox = "0 0 1280 720"
        canvas_format = "ppt169"
        seen_sizes: List[float] = []
        for idx, item in enumerate(pages, 1):
            if not isinstance(item, dict):
                warnings.append(f"第 {idx} 页配置非对象，已跳过")
                continue
            want = str(item.get("page") or "").strip()
            values = item.get("values") or {}
            if not isinstance(values, dict):
                values = {}
            try:
                raw, placeholders = tpl.prepare_page_svg(suite, self._resolve_page(suite, want))
            except (FileNotFoundError, ValueError) as exc:
                raise PptEditError(str(exc))
            if idx == 1:
                m = re.search(r'viewBox="([^"]+)"', raw)
                if m:
                    viewbox = m.group(1).strip()
            filled, overflow = tpl.fill_page_svg_checked(
                raw, {k: str(v) for k, v in values.items()},
                workspace=self.workspace)
            if overflow:
                warnings.append(
                    f"第 {idx} 页 {', '.join(overflow)} 文案过长（已缩到最小字号仍超出），建议精简或拆页")
            # 字号收集放在排版**之后**：缩排会引入非模板字号（22→16.5 等），
            # 不收集进 spec_lock 会被质检判「未声明的字号」→ 导出失败（踩过）。
            seen_sizes.extend(tpl.collect_font_sizes(filled))
            # 深色主题（C3）：色板等值替换，布局不动
            if str(spec.get("theme") or "").strip().lower() == "dark":
                filled = tpl.apply_dark_theme(filled)
            # 表格 / 图表：写进模板的语义载荷，导出时变成 PowerPoint 原生对象。
            # 不写就会永远显示模板自带的示例数据（Adoption 68% 之类）——踩过。
            if item.get("table") is not None:
                filled = tpl.inject_table(filled, item["table"])
            if item.get("chart") is not None:
                filled = tpl.inject_chart(filled, item["chart"])
            elif item.get("table") is None:
                # 该页既没有表格也没有图表数据：模板示例数据与可见回退配色
                # 不一致（模板上游问题），--native-charts-and-tables 会拒绝导出。
                # 摘掉替换标记 → 该页保持视觉渲染（原样，不报错）。
                # 注意：只在没有**任何**语义数据时摘；给了 table 的页不能走这。
                filled = tpl.strip_native_markers(filled)
            missing = [p for p in placeholders if p not in values]
            if missing:
                warnings.append(f"第 {idx} 页未提供 {', '.join(missing)}（按空值导出）")
            with open(os.path.join(svg_dir, f"slide_{idx:02d}.svg"), "w", encoding="utf-8") as fh:
                # 尾注记录填充值：change_layout 换版式时靠它把文案迁到同名
                # 占位符（不迁图片/表格，形状不同对不上）。普通导入的页没有
                # 这行注释，change_layout 按空值处理并在返回里说明。
                fh.write(filled + "\n<!-- litework-filled: "
                         + json.dumps({k: v for k, v in values.items()
                                       if not k.startswith("IMAGE_")},
                                      ensure_ascii=False) + " -->\n")

        brand_color = ""
        brand = str(spec.get("brand") or "").strip()
        if brand:
            for b in tpl.list_brands():
                if b["key"] == brand:
                    brand_color = b.get("primary_color", "")
                    break
            else:
                warnings.append(f"未知品牌 {brand}（已忽略配色）")

        tpl.write_spec_lock(
            out, viewbox=viewbox, canvas_format=canvas_format,
            brand_color=brand_color, style=str(spec.get("style") or ""),
            extra_sizes=sorted(set(seen_sizes)),
        )

        # 质检是导出的硬前置；失败不阻断生成，但要在结果里说清楚
        code, qlog = _run_script("svg_quality_checker.py",
                                 [out, "--canonical-authoring", "--stage", "final", "--json"])
        if code not in (0, 2):   # 2 = 有 warning 但可用
            warnings.append(f"质检有阻塞项，导出可能失败：{_tail(qlog, 200)}")

        filename = str(args.get("filename") or f"{base_slug}.pptx").strip()
        if not filename.lower().endswith(".pptx"):
            filename += ".pptx"
        target = os.path.join(self.workspace, filename)

        # --native-charts-and-tables：把表格/图表槽导出成 PowerPoint **原生**
        # 对象（可继续编辑）。不加就只是视觉渲染的矩形+文本框——用户拿到的
        # 表格改不了、图表动不了（这是最初的真实缺陷）。
        code, log = _run_script("svg_to_pptx.py",
                                [out, "-o", target, "--native-charts-and-tables"])
        if code != 0 or not os.path.isfile(target):
            raise PptEditError(f"按模板生成失败（exit={code}）:\n{_tail(log)}")

        # 立刻转成 roundtrip 工作区：生成完就能在同一编辑器里微调
        project = os.path.splitext(os.path.basename(target))[0]
        ws = os.path.join(self.workspace, _WORK_ROOT, project)
        if os.path.isdir(ws):
            shutil.rmtree(ws, ignore_errors=True)
        code2, log2 = _run_script("pptx_to_svg.py", [
            target, "-o", ws, "--inheritance-mode", "both", "--roundtrip",
        ])
        if code2 != 0:
            raise PptEditError(
                f"已生成 {filename}，但转入编辑器失败（exit={code2}）:\n{_tail(log2)}")

        return json.dumps({
            "ok": True,
            "output": self._rel(target),
            "project": project,
            "slides": self._slide_count(ws),
            "warnings": warnings,
        }, ensure_ascii=False, indent=2)

    def _resolve_page(self, suite: str, want: str) -> str:
        """把用户给的页标识解析成模板文件名（实现见 ppt_templates）。"""
        from . import ppt_templates as tpl

        if not want:
            raise PptEditError("pages[].page 必填（如 title_slide / 01_title_slide.svg）")
        try:
            return tpl.resolve_page_file(suite, want)
        except FileNotFoundError as exc:
            raise PptEditError(str(exc))

    # ------------------------------------------------------------ op: 增删页 / 换版式

    def _authoring_dir(self, project: str) -> str:
        """roundtrip 工作区的页面目录（authoring-svg-flat）。"""
        d = os.path.join(self._project_dir(project), "authoring-svg-flat")
        if not os.path.isdir(d):
            raise PptEditError(f"工作区没有页面目录: {project}")
        return d

    def _sync_authoring_manifest(self, project_dir: str, count: int) -> None:
        """增删页后同步 authoring manifest 的文件清单。

        manifest 记录每页的 source/authoring 对应关系（哈希由导出器重算），
        页数不一致时导出会按 manifest 走——必须重建。
        """
        import hashlib

        def _sha(path: str) -> str:
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
            return h.hexdigest()

        auth_dir = os.path.join(project_dir, "authoring-svg-flat")
        files = [f"slide_{i:02d}.svg" for i in range(1, count + 1)]

        # roundtrip 导出校验三层文件一致：authoring / analysis flat / layered。
        # 增删页破坏了对应关系——把 authoring 页同步为新的"源"，并重算哈希，
        # 这样每页都变成"内容页"（不走字节级 passthrough，但导出成功）。
        for sub in ("analysis/roundtrip-svg/flat", "analysis/roundtrip-svg/layered"):
            src_dir = os.path.join(project_dir, sub)
            if not os.path.isdir(src_dir):
                continue
            for old in os.listdir(src_dir):
                if re.fullmatch(r"slide_\d+\.svg", old):
                    os.remove(os.path.join(src_dir, old))
            for f in files:
                shutil.copyfile(os.path.join(auth_dir, f),
                                os.path.join(src_dir, f))

        mpath = os.path.join(auth_dir, "authoring_manifest.json")
        try:
            with open(mpath, encoding="utf-8") as fh:
                manifest = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(manifest.get("documents"), list):
            return
        docs = []
        for f in files:
            digest = _sha(os.path.join(auth_dir, f))
            docs.append({"source": f, "authoring": f, "source_refs": {},
                         "source_sha256": digest,
                         "initial_authoring_sha256": digest})
        manifest["documents"] = docs
        manifest["file_count"] = count
        manifest["source_ref_count"] = 0
        try:
            with open(mpath, "w", encoding="utf-8") as fh:
                json.dump(manifest, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _slide_files(self, project: str) -> List[str]:
        d = self._authoring_dir(project)
        return sorted(f for f in os.listdir(d)
                      if re.fullmatch(r"slide_\d+\.svg", f))

    def _mark_restructured(self, project_dir: str) -> None:
        """结构操作（增删页/换版式）后：打标记 + 清理 roundtrip 残留。

        - `.litework-restructured` 标记：export 看到它改走 flat 导出
          （roundtrip 三层哈希校验在页结构变化后必然失配）；
        - 清掉页上的 `data-pptx-source-ref`：那是「无损导入」的投影锚点，
          新结构页带它会在再投影时冲突（实测报 "Input already contains
          reserved data-pptx-source-ref"）。
        """
        auth_dir = os.path.join(project_dir, "authoring-svg-flat")
        marker = os.path.join(project_dir, ".litework-restructured")
        try:
            open(marker, "w").close()
        except OSError:
            pass
        if not os.path.isdir(auth_dir):
            return
        for name in os.listdir(auth_dir):
            if not re.fullmatch(r"slide_\d+\.svg", name):
                continue
            path = os.path.join(auth_dir, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    svg = fh.read()
            except OSError:
                continue
            cleaned = re.sub(r'\s+data-pptx-source-ref="[^"]*"', "", svg)
            if cleaned != svg:
                try:
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(cleaned)
                except OSError:
                    pass
        auth_dir = os.path.join(project_dir, "authoring-svg-flat")
        marker = os.path.join(project_dir, ".litework-restructured")
        try:
            open(marker, "w").close()
        except OSError:
            pass
        if not os.path.isdir(auth_dir):
            return
        for name in os.listdir(auth_dir):
            if not re.fullmatch(r"slide_\d+\.svg", name):
                continue
            path = os.path.join(auth_dir, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    svg = fh.read()
            except OSError:
                continue
            cleaned = re.sub(r'\s+data-pptx-source-ref="[^"]*"', "", svg)
            if cleaned != svg:
                try:
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(cleaned)
                except OSError:
                    pass
        # flat 导出需要 spec_lock.md；roundtrip 工作区没有（只在模板生成时写）。
        # 结构变更后必然走 flat 导出，这里按页面实际字号补一份。
        if not os.path.isfile(os.path.join(project_dir, "spec_lock.md")):
            sizes: List[float] = []
            for name in os.listdir(auth_dir):
                if re.fullmatch(r"slide_\d+\.svg", name):
                    try:
                        with open(os.path.join(auth_dir, name), encoding="utf-8") as fh:
                            sizes.extend(float(x) for x in
                                         re.findall(r'font-size="([\d.]+)"', fh.read()))
                    except OSError:
                        pass
            from . import ppt_templates as _tpl
            _tpl.write_spec_lock(
                project_dir, viewbox="0 0 1280 720", canvas_format="ppt169",
                extra_sizes=sorted(set(sizes)))
        # flat 导出读的是 svg_output/；roundtrip 工作区没有它——把 authoring
        # 页镜像过去（结构变更后的导出走这条目录）。
        out_dir = os.path.join(project_dir, "svg_output")
        os.makedirs(out_dir, exist_ok=True)
        for name in os.listdir(out_dir):
            if re.fullmatch(r"slide_\d+\.svg", name):
                os.remove(os.path.join(out_dir, name))
        for name in os.listdir(auth_dir):
            if re.fullmatch(r"slide_\d+\.svg", name):
                shutil.copyfile(os.path.join(auth_dir, name),
                                os.path.join(out_dir, name))
        # flat 导出的硬前置：质检报告。roundtrip 页清掉投影属性后应能过；
        # 失败也不吞——让导出错误可见（带质检 tail）。
        _run_script("svg_quality_checker.py",
                    [project_dir, "--canonical-authoring", "--stage", "final", "--json"])

    def _renumber_slides(self, project: str) -> int:
        """增删页后把 slide_*.svg 重编为连续编号（导出器要求连续）。"""
        d = self._authoring_dir(project)
        files = self._slide_files(project)
        # 先全部挪到临时名，避免 slide_02→slide_01 时目标已存在
        for i, f in enumerate(files, 1):
            os.replace(os.path.join(d, f), os.path.join(d, f".tmp{i:02d}.svg"))
        for i in range(1, len(files) + 1):
            os.replace(os.path.join(d, f".tmp{i:02d}.svg"),
                       os.path.join(d, f"slide_{i:02d}.svg"))
        self._sync_authoring_manifest(
            os.path.dirname(d), len(files))
        self._mark_restructured(os.path.dirname(d))
        return len(files)

    def _op_add_slide(self, args: Dict[str, Any]) -> str:
        """插入一页：模板版式（layout）或复制某页（copy_from）。"""
        project = str(args.get("project") or "")
        index = int(args.get("index") or 0)          # 0 = 追加到末尾
        layout = str(args.get("layout") or "").strip()
        base = int(args.get("copy_from") or 0)
        d = self._authoring_dir(project)
        files = self._slide_files(project)
        n = len(files)

        if layout:
            from . import ppt_templates as tpl
            suite = str(args.get("suite") or "presentation_core").strip()
            raw, placeholders = tpl.prepare_page_svg(
                suite, tpl.resolve_page_file(suite, layout))
            # 模板页必须当场填充（flat 导出不认 {{KEY}}，会原样导出）。
            # 未提供的占位符按空值出（和 create_from_template 的语义一致）。
            vals = args.get("values") or {}
            if not isinstance(vals, dict):
                vals = {}
            vals = {k: str(v) for k, v in vals.items()}
            filled, _ov = tpl.fill_page_svg_checked(
                raw, vals, workspace=self.workspace)
            new_svg = filled + "\n<!-- litework-filled: " + json.dumps(
                vals, ensure_ascii=False) + " -->\n"
        elif 1 <= base <= n:
            with open(os.path.join(d, files[base - 1]), encoding="utf-8") as fh:
                new_svg = fh.read()
            # 复制页清掉旧标注：「应用改动」不能把幽灵标注带给 Agent
            new_svg = re.sub(r'\s+data-edit-annotation="[^"]*"', "", new_svg)
        else:
            raise PptEditError("需要 layout（模板版式）或 copy_from（复制哪一页）")

        pos = max(1, min(index if index else n + 1, n + 1))
        for i in range(n, pos - 1, -1):
            src = os.path.join(d, f"slide_{i:02d}.svg")
            if os.path.isfile(src):
                os.replace(src, os.path.join(d, f"slide_{i + 1:02d}.svg"))
        with open(os.path.join(d, f"slide_{pos:02d}.svg"), "w", encoding="utf-8") as fh:
            fh.write(new_svg)
        total = self._renumber_slides(project)
        return json.dumps({"ok": True, "slide": pos, "slides": total},
                          ensure_ascii=False, indent=2)

    def _op_remove_slide(self, args: Dict[str, Any]) -> str:
        """删除一页（至少保留一页，空 deck 导出会崩）。"""
        project = str(args.get("project") or "")
        slide = int(args.get("slide") or 0)
        files = self._slide_files(project)
        if not (1 <= slide <= len(files)):
            raise PptEditError(f"页码越界：{slide}（共 {len(files)} 页）")
        if len(files) <= 1:
            raise PptEditError("至少保留一页；要清空请重新导入")
        d = self._authoring_dir(project)
        os.remove(os.path.join(d, files[slide - 1]))
        total = self._renumber_slides(project)
        return json.dumps({"ok": True, "slides": total}, ensure_ascii=False, indent=2)

    def _extract_slot_values(self, svg: str, keys: List[str]) -> Dict[str, str]:
        """按目标模板占位符顺序，从旧页文本对位提取现值。

        两个世界的数据形态不同：
        - 模板页（create_from_template 直出）：文本在 `*-carrier` id 的
          <text> 里，顺序 = 占位符顺序；
        - roundtrip 导入页：文本散在 `<g id="shape-N">` 里、**没有 carrier
          id**（实测），只能按「带文本的顶层组出现顺序」对位——它与
          outline（编辑器看到的元素序）一致。

        顺序对位在换不同版式时可能语义错位（三点页换双栏页），但错位
        的内容用户在编辑器里能看到再调，好过整页清空。
        """
        if not keys:
            return {}
        # 先试 carrier（模板直出页），不齐再退回顶层文本组（导入页）
        texts: List[str] = []
        for pattern in (
                r'<text\b[^>]*id="[^"]*-carrier"[^>]*>(.*?)</text>',
                r'<text\b[^>]*>(.*?)</text>',
        ):
            texts = []
            for m in re.finditer(pattern, svg, re.S):
                block = m.group(1)
                if "<tspan" in block:
                    lines = [re.sub(r"<[^>]+>", "", tm.group(1))
                             for tm in re.finditer(
                                 r"<tspan\b[^>]*>(.*?)</tspan>", block, re.S)]
                    texts.append("\n".join(lines))
                else:
                    texts.append(re.sub(r"<[^>]+>", "", block))
            if texts:
                break
        return {k: v for k, v in zip(keys, texts) if v}

    def _op_change_layout(self, args: Dict[str, Any]) -> str:
        """把某页换成模板版式；同名占位符文案自动迁移。

        依赖 create_from_template 在 SVG 尾部埋的 `litework-filled` 注释——
        普通导入（pptx_to_svg）的页面没有它，按空值换版式并在返回值里说明，
        前端据此提示「文案会丢」。
        """
        from . import ppt_templates as tpl

        project = str(args.get("project") or "")
        slide = int(args.get("slide") or 0)
        suite = str(args.get("suite") or "presentation_core").strip()
        layout = str(args.get("layout") or "").strip()
        if not layout:
            raise PptEditError("op=change_layout 需要 layout（目标版式 slug）")
        files = self._slide_files(project)
        if not (1 <= slide <= len(files)):
            raise PptEditError(f"页码越界：{slide}（共 {len(files)} 页）")

        path = os.path.join(self._authoring_dir(project), files[slide - 1])
        with open(path, encoding="utf-8") as fh:
            old_svg = fh.read()
        old_values: Dict[str, str] = {}
        from_template = False
        m = re.search(r"<!-- litework-filled: (.*?) -->", old_svg, re.S)
        if m:
            from_template = True
            try:
                old_values = json.loads(m.group(1))
            except json.JSONDecodeError:
                old_values = {}
        # 兜底：roundtrip 导入会重建 SVG、丢掉填充注释——按新模板占位符
        # 顺序与旧页 carrier 文本对位（模板里两者出现顺序严格一致）。
        if not old_values:
            raw, placeholders = tpl.prepare_page_svg(
                suite, tpl.resolve_page_file(suite, layout))
            old_values = self._extract_slot_values(old_svg, placeholders)
            from_template = bool(old_values)
        raw, placeholders = tpl.prepare_page_svg(
            suite, tpl.resolve_page_file(suite, layout))
        filled = tpl.fill_page_svg(raw, old_values, workspace=self.workspace)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(filled + "\n<!-- litework-filled: "
                     + json.dumps(old_values, ensure_ascii=False) + " -->\n")
        self._mark_restructured(self._project_dir(project))
        missing = [p for p in placeholders if p not in old_values]
        return json.dumps({
            "ok": True, "slide": slide, "layout": layout,
            "carried": sorted(k for k in old_values),
            "missing": missing,
            "warning": None if from_template
            else "源页不是模板生成的，文案无法迁移（已按空值换版式）",
        }, ensure_ascii=False, indent=2)

    def _op_get_notes(self, args: Dict[str, Any]) -> str:
        """读全部页的演讲者备注（notes/slide_NN.md，ppt-master 的约定位置）。"""
        project_dir = self._project_dir(str(args.get("project") or ""))
        notes_dir = os.path.join(project_dir, "notes")
        out: List[Dict[str, Any]] = []
        total = self._slide_count(project_dir)
        for n in range(1, total + 1):
            path = os.path.join(notes_dir, f"slide_{n:02d}.md")
            text = ""
            if os.path.isfile(path):
                try:
                    with open(path, encoding="utf-8") as fh:
                        text = fh.read().strip()
                except OSError:
                    text = ""
            out.append({"slide": n, "note": text})
        return json.dumps({"ok": True, "notes": out}, ensure_ascii=False, indent=2)

    def _op_set_note(self, args: Dict[str, Any]) -> str:
        """写单页演讲者备注；note 为空串 = 删除该页备注。"""
        project_dir = self._project_dir(str(args.get("project") or ""))
        slide = int(args.get("slide") or 0)
        note = str(args.get("note") or "").strip()
        total = self._slide_count(project_dir)
        if not (1 <= slide <= total):
            raise PptEditError(f"页码越界：{slide}（共 {total} 页）")
        notes_dir = os.path.join(project_dir, "notes")
        os.makedirs(notes_dir, exist_ok=True)
        path = os.path.join(notes_dir, f"slide_{slide:02d}.md")
        if note:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(note + "\n")
        elif os.path.isfile(path):
            os.remove(path)      # 空备注不留空文件（导出器按文件存在与否判断）
        return json.dumps({"ok": True, "slide": slide,
                           "cleared": not note}, ensure_ascii=False, indent=2)

    def _op_extract(self, args: Dict[str, Any]) -> str:
        """把已有 pptx 逐页读成「结构化内容」，供重排到模板使用。

        为什么不用 outline：outline 面向编辑器（带 frame/id/热区），重排关心的是
        「这页讲了什么、有几段、有没有表格图表图片」，两者粒度不同。
        这里返回每页的语义摘要 + 完整文本，让 Agent 据此决定映射到哪个版式。

        参数 path 直接指向源 pptx（不需要先 open 成工作区）——重排是「读旧写新」，
        不必为源文件建 roundtrip 工作区。
        """
        rel = str(args.get("path") or "").strip()
        if not rel:
            raise PptEditError("op=extract 缺少 path")
        src = self._resolve(rel)
        if not os.path.isfile(src):
            raise PptEditError(f"文件不存在: {rel}")

        try:
            from pptx import Presentation
        except ImportError:
            raise PptEditError("需要安装 python-pptx 才能读取演示文稿")

        try:
            prs = Presentation(src)
        except Exception as exc:
            raise PptEditError(f"无法打开演示文稿: {exc}")

        slides: List[Dict[str, Any]] = []
        for idx, slide in enumerate(prs.slides, 1):
            title = ""
            paragraphs: List[str] = []
            has_table = has_chart = has_picture = False
            table_data: List[List[str]] = []
            chart_series: List[Dict[str, Any]] = []

            for shape in slide.shapes:
                st = getattr(shape, "shape_type", None)
                if st is not None and "PICTURE" in str(st):
                    has_picture = True
                if getattr(shape, "has_table", False):
                    has_table = True
                    tb = shape.table
                    table_data = [
                        [tb.cell(r, c).text for c in range(len(tb.columns))]
                        for r in range(len(tb.rows))
                    ]
                if getattr(shape, "has_chart", False):
                    has_chart = True
                    try:
                        plot = shape.chart.plots[0]
                        chart_series.append({
                            "categories": [str(c) for c in plot.categories],
                            "series": [
                                {"name": s.name, "values": [float(v) for v in s.values]}
                                for s in plot.series
                            ],
                        })
                    except Exception:
                        pass
                if not getattr(shape, "has_text_frame", False):
                    continue
                is_title = shape == getattr(slide.shapes, "title", None)
                for para in shape.text_frame.paragraphs:
                    text = "".join(run.text for run in para.runs).strip()
                    if not text:
                        continue
                    if is_title and not title:
                        title = text
                    else:
                        paragraphs.append(text)

            # 版式类型初判：给 Agent 一个起点，它仍可覆盖
            layout_guess = "content"
            if idx == 1 and title and len(paragraphs) <= 2:
                layout_guess = "title_slide"
            elif has_table:
                layout_guess = "table_summary"
            elif has_chart:
                layout_guess = "chart_insight"
            elif not paragraphs and title:
                layout_guess = "section_header"
            elif len(paragraphs) >= 6:
                layout_guess = "title_content"

            slides.append({
                "slide": idx,
                "title": title,
                "paragraphs": paragraphs,
                "layout_guess": layout_guess,
                "has_table": has_table,
                "has_chart": has_chart,
                "has_picture": has_picture,
                "table": table_data,
                "chart": chart_series,
            })

        return json.dumps({
            "ok": True,
            "source": rel,
            "total": len(slides),
            "slides": slides,
        }, ensure_ascii=False, indent=2)
