#!/usr/bin/env python3
"""图表渲染脚本：把 PlantUML / Mermaid 源码渲染为 PNG/SVG 图片。

用法示例：
    python3 render_diagram.py 架构.puml -o .outputs/架构图.png
    python3 render_diagram.py 流程.mmd -o .outputs/流程图.png --scale 3
    python3 render_diagram.py "@startuml ... @enduml" -o 图.png --type plantuml
    python3 render_diagram.py doc.md -o 图.svg            # 自动识别类型
    python3 render_diagram.py --check                     # 环境诊断（离线可用性）
    python3 render_diagram.py --install                   # 联网预装引擎（装一次后离线可用）

【离线保证】默认【离线优先】：
- 只使用本地已安装的引擎（plantuml CLI / java -jar plantuml.jar / mmdc），
  绝不联网拉取；
- docker 仅在本地已缓存镜像时使用（自动检查 docker image inspect）；
- npx 仅在本地已缓存 mermaid-cli 时使用（--allow-network 才允许联网安装）；
- 内置纯 Python 兜底渲染器（matplotlib，项目已依赖）：没有任何外部引擎时
  也能把常见 flowchart / sequence 图表渲染成图片，保证离线必有输出。

退出码：0 成功；1 失败（诊断信息输出到 stderr）。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# 常见 plantuml.jar 查找位置（--plantuml-jar 可显式指定）
PLANTUML_JAR_CANDIDATES = [
    "plantuml.jar",
    "/usr/local/share/plantuml/plantuml.jar",
    "/usr/share/plantuml/plantuml.jar",
    os.path.expanduser("~/plantuml.jar"),
    os.path.expanduser("~/.local/share/plantuml.jar"),
]


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def detect_type(source: str, input_path: str, explicit: str) -> str:
    """自动识别图表类型：plantuml / mermaid。"""
    if explicit and explicit != "auto":
        return explicit
    text = source.lower()
    suffix = Path(input_path).suffix.lower()
    if "@startuml" in text or suffix in (".puml", ".plantuml", ".iuml"):
        return "plantuml"
    if "```mermaid" in text or "```mmd" in text or suffix in (".mmd", ".mermaid"):
        return "mermaid"
    # 常见 mermaid 关键字兜底
    if any(k in text for k in (
        "flowchart", "sequencediagram", "classdiagram", "statediagram",
        "statediagram-v2", "erdiagram", "gantt", "journey", "pie", "mindmap",
        "gitgraph", "timeline", "quadrantchart", "requirementdiagram",
    )):
        return "mermaid"
    # PlantUML 常见关键字兜底
    if any(k in text for k in (
        "actor", "participant", "usecase", "class ", "interface ",
        "package ", "component", "state ", "deployment", "rectangle",
        "startuml", "skinparam",
    )):
        return "plantuml"
    raise ValueError("无法自动识别图表类型，请用 --type plantuml|mermaid 指定")


def extract_source(raw: str, chart_type: str) -> str:
    """从输入文本中提取真正的图表 DSL 源码。

    - PlantUML：取第一个 @startuml ... @enduml 块（若存在）；
    - Mermaid：取第一个 ```mermaid ... ``` 块（若存在），否则原样返回。
    """
    if chart_type == "plantuml":
        m = re.search(r"@startuml.*?@enduml", raw, re.S)
        return m.group(0) if m else raw
    m = re.search(r"```(?:mermaid|mmd)\s*\n(.*?)```", raw, re.S)
    if m:
        return m.group(1).strip()
    return raw


def lint_plantuml(source: str) -> list:
    """PlantUML 渲染前预校验（写完必校验规则的自动化）。

    返回 (errors, warnings)：
    - errors：@startuml/@enduml 配对错误 → 阻断渲染（这类错误引擎常报在
      不直观的后续行，提前拦截）；
    - warnings：括号不平衡、疑似箭头缺目标、() 嵌套在 {} 块内、块内出现
      () 声明、非标准元素（system/folder/file）等 → 告警不阻断
      （标签文本中合法括号会误报，故仅提示）。
    """
    errors: list = []
    warnings: list = []

    text = source.strip()

    # ---- 1) @startuml / @enduml 配对（硬错误）----
    n_start = len(re.findall(r"^\s*@startuml", text, re.M))
    n_end = len(re.findall(r"^\s*@enduml", text, re.M))
    if n_start != n_end:
        errors.append(
            f"@startuml/@enduml 不配对：找到 {n_start} 个 @startuml、{n_end} 个 @enduml"
        )
    elif n_start == 0:
        warnings.append("未找到 @startuml/@enduml 包裹（PlantUML 通常需要成对包裹）")

    # ---- 2) 括号平衡（警告：标签中的括号会误报，仅提示）----
    for opener, closer in (("{", "}"), ("(", ")"), ("[", "]")):
        # 去掉引号内的内容再计数，减少字符串字面量误报
        stripped = re.sub(r'"[^"\n]*"', '""', text)
        stripped = re.sub(r"'[^'\n]*'", "''", stripped)
        if stripped.count(opener) != stripped.count(closer):
            warnings.append(
                f"括号不平衡：{opener}{closer} 计数 {stripped.count(opener)}:{stripped.count(closer)}"
                "（若在标签文本中属正常，请确认块结构）"
            )

    lines = text.splitlines()
    # ---- 3) 疑似箭头缺目标：A --> : label（警告）----
    for i, ln in enumerate(lines, 1):
        if re.search(r"-->\s*:", ln) or re.search(r"->\s*:", ln):
            warnings.append(
                f"第 {i} 行疑似箭头缺少目标：`{ln.strip()[:60]}`"
                "（应为 `A --> B : label`）"
            )

    # ---- 4) () 嵌套在 {} 块内 / package/rectangle 块内出现 () 声明（警告）----
    in_block_depth = 0
    for i, ln in enumerate(lines, 1):
        stripped_ln = re.sub(r'"[^"]*"', '""', ln)
        if in_block_depth > 0 and re.match(r"^\s*\(\w", stripped_ln):
            warnings.append(
                f"第 {i} 行：() 声明出现在 {{}} 块内（package/rectangle 块内"
                "应用 component \"name\"，禁止 () 嵌块）"
            )
        in_block_depth += stripped_ln.count("{") - stripped_ln.count("}")

    # ---- 5) 非标准元素（警告）----
    for i, ln in enumerate(lines, 1):
        if re.match(r"^\s*system\b", ln, re.I):
            warnings.append(f"第 {i} 行：`system` 是 ArchiMate 专用元素，标准 UML 用 rectangle/component")
        if re.match(r"^\s*(folder|file)\b", ln, re.I):
            warnings.append(f"第 {i} 行：`{ln.strip().split()[0]}` 慎用，组件/部署图建议用 package/node")

    return errors, warnings


# 渲染失败时的常见错误速查（供 Agent 快速定位问题）
PLANTUML_ERROR_HINTS = """\
常见 PlantUML 错误速查（详见技能「PlantUML 安全写法」章节）：
  · 箭头必须成对有目标：A --> B : label（❌ A --> : label）
  · () 只能声明 interface/usecase；package/rectangle 块内禁止嵌 ()
    （块内用 component "name" as alias）
  · 组件/部署图只用标准元素：rectangle/component/actor/database/node/package/usecase
    （❌ system 是 ArchiMate 专用；folder/file 慎用）
  · note 用 note "text" 单行或块外 note right of Alias ... end note；
    不要在 {} 块内嵌套 note right
  · 非必要不写 !pragma layout smetana（与 note/box 混用易崩）
  · 元素 > 15 个 → 拆图；时序图与结构图分开画"""


class DiagramSyntaxError(RuntimeError):
    """源码语法错误（引擎已执行但渲染失败）——不应走内置兜底，需修源码。

    与「引擎缺失」的 RuntimeError 区分：语法错误时兜底渲染只会输出源码
    文本图，掩盖真实问题，故直接报错让调用方（Agent）修正源码。
    """


def _run(cmd: list, cwd: str, timeout: int = 180) -> subprocess.CompletedProcess:
    """执行命令，返回 CompletedProcess；失败时附带 stderr 诊断。"""
    try:
        return subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"未找到命令: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"命令超时（>{timeout}s）: {' '.join(cmd)}") from exc


def _find_plantuml_jar() -> str | None:
    for cand in PLANTUML_JAR_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    return None


def _which(name: str) -> bool:
    return shutil.which(name) is not None


def _docker_image_cached(name: str) -> bool:
    """检查本地是否已缓存 docker 镜像（不联网）。失败/无 docker 返回 False。"""
    if not _which("docker"):
        return False
    try:
        r = subprocess.run(
            ["docker", "image", "inspect", name],
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode == 0
    except Exception:
        return False


def _npx_pkg_cached(pkg: str) -> bool:
    """检查 npm 本地缓存中是否已有包（--no-install 不联网）。"""
    if not _which("npx"):
        return False
    try:
        r = subprocess.run(
            ["npx", "--no-install", pkg, "--version"],
            capture_output=True, text=True, timeout=60,
        )
        return r.returncode == 0
    except Exception:
        return False


def _plantuml_core_available() -> bool:
    """检查 @plantuml/core（纯 JS PlantUML 引擎）是否可用：node + 全局/本地包。

    不需要 Java，离线可用。安装：npm install -g @plantuml/core
    """
    if not _which("node"):
        return False
    # 0) 环境变量显式指定（与 plantuml_js_render.mjs 一致）
    env_dir = os.environ.get("PLANTUML_CORE_DIR")
    if env_dir and os.path.isfile(os.path.join(env_dir, "plantuml.js")):
        return True
    # 全局 npm root 下是否有 @plantuml/core
    try:
        root = subprocess.run(
            ["npm", "root", "-g"], capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        if root and os.path.isfile(os.path.join(root, "@plantuml", "core", "plantuml.js")):
            return True
    except Exception:
        pass
    # 本地 node_modules（脚本同目录或上级）
    here = os.path.dirname(os.path.abspath(__file__))
    d = here
    while True:
        if os.path.isfile(os.path.join(d, "node_modules", "@plantuml", "core", "plantuml.js")):
            return True
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return False


def _svg_to_png(svg_path: str, png_path: str, scale: int = 2) -> str | None:
    """把 SVG 转为 PNG，返回使用的转换器名；全部不可用返回 None。

    依次尝试：resvg（Node，跨平台）→ cairosvg（Python）→ rsvg-convert →
    inkscape → magick/convert → qlmanage（macOS 自带）。
    """
    # 1) @resvg/resvg-js（Node，跨平台 native，npm 装一次离线可用）
    if _which("node") and _resvg_available():
        r = _run(["node", _SVG2PNG_JS, svg_path, png_path, "--scale", str(scale)],
                 os.path.dirname(svg_path), timeout=120)
        if r.returncode == 0 and os.path.isfile(png_path) and os.path.getsize(png_path) > 0:
            return "resvg"
    # 2) cairosvg（Python 库，pip install cairosvg，离线可用）
    try:
        import cairosvg  # type: ignore
        cairosvg.svg2png(url=svg_path, write_to=png_path,
                         scale=max(1, int(scale)))
        if os.path.isfile(png_path) and os.path.getsize(png_path) > 0:
            return "cairosvg"
    except Exception:
        pass
    # 3) rsvg-convert（librsvg）
    if _which("rsvg-convert"):
        r = _run(["rsvg-convert", "-o", png_path, svg_path], os.path.dirname(svg_path), timeout=120)
        if r.returncode == 0 and os.path.isfile(png_path) and os.path.getsize(png_path) > 0:
            return "rsvg-convert"
    # 4) inkscape
    if _which("inkscape"):
        r = _run(["inkscape", svg_path, "--export-type=png",
                  f"--export-filename={png_path}"],
                 os.path.dirname(svg_path), timeout=120)
        if r.returncode == 0 and os.path.isfile(png_path) and os.path.getsize(png_path) > 0:
            return "inkscape"
    # 5) ImageMagick
    conv = "magick" if _which("magick") else ("convert" if _which("convert") else None)
    if conv:
        r = _run([conv, "-density", "150", svg_path, png_path],
                 os.path.dirname(svg_path), timeout=120)
        if r.returncode == 0 and os.path.isfile(png_path) and os.path.getsize(png_path) > 0:
            return conv
    # 6) qlmanage（macOS 自带，零安装）
    if _which("qlmanage") and sys.platform == "darwin":
        out_dir = os.path.dirname(os.path.abspath(png_path))
        r = _run(["qlmanage", "-t", "-s", str(96 * max(1, scale)), "-o", out_dir, svg_path],
                 os.path.dirname(svg_path), timeout=120)
        thumb = os.path.join(out_dir, os.path.splitext(os.path.basename(svg_path))[0] + ".svg.png")
        if r.returncode == 0 and os.path.isfile(thumb):
            shutil.copyfile(thumb, png_path)
            return "qlmanage"
    return None


def _resvg_available() -> bool:
    """检查 @resvg/resvg-js 是否可用（node + 全局/本地包）。"""
    if not _which("node"):
        return False
    if os.environ.get("RESVG_DIR"):
        return os.path.isfile(os.path.join(os.environ["RESVG_DIR"], "index.js"))
    try:
        root = subprocess.run(
            ["npm", "root", "-g"], capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        if root and os.path.isfile(os.path.join(root, "@resvg", "resvg-js", "index.js")):
            return True
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    d = here
    while True:
        if os.path.isfile(os.path.join(d, "node_modules", "@resvg", "resvg-js", "index.js")):
            return True
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return False


_SVG2PNG_JS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "svg2png.mjs")


_PLANTUML_JS_RENDER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "plantuml_js_render.mjs")


def render_plantuml(source: str, out_path: str, scale: int, tmpdir: str,
                    plantuml_jar: str | None, allow_network: bool = False) -> str:
    """渲染 PlantUML 源码，返回渲染引擎说明。离线优先，仅本地引擎。"""
    in_file = os.path.join(tmpdir, "diagram.puml")
    with open(in_file, "w", encoding="utf-8") as f:
        f.write(source)

    fmt = "-tsvg" if out_path.lower().endswith(".svg") else "-tpng"
    extra = []
    if scale > 1 and fmt == "-tpng":
        extra = [f"-scale", str(scale)]
    if out_path.lower().endswith(".svg"):
        out_name = "diagram.svg"
    else:
        out_name = "diagram.png"
    out_full = os.path.join(tmpdir, out_name)

    def check_out() -> bool:
        return os.path.isfile(out_full) and os.path.getsize(out_full) > 0

    # 记录「引擎已执行但渲染失败」的错误：全部引擎失败且至少一个执行过
    # → 大概率是源码语法错误（抛 DiagramSyntaxError，不走兜底）
    engine_errors: list = []

    # 1) plantuml CLI（本地，离线可用）
    if _which("plantuml"):
        cmd = ["plantuml", fmt, "-charset", "UTF-8", *extra, in_file]
        log("[render] 使用 plantuml CLI")
        r = _run(cmd, tmpdir)
        if r.returncode == 0 and check_out():
            shutil.copyfile(out_full, out_path)
            return "plantuml CLI"
        engine_errors.append("plantuml CLI: " + (r.stderr or r.stdout).strip()[:400])
        log("  plantuml CLI 失败: " + (r.stderr or r.stdout).strip()[:500])

    # 2) @plantuml/core 纯 JS 引擎（node，无需 Java，离线可用）
    if _which("node") and _plantuml_core_available():
        log("[render] 使用 @plantuml/core（纯 JS）")
        svg_tmp = os.path.join(tmpdir, "diagram.svg")
        r = _run(["node", _PLANTUML_JS_RENDER, in_file, svg_tmp], tmpdir, timeout=300)
        if r.returncode == 0 and os.path.isfile(svg_tmp) and os.path.getsize(svg_tmp) > 0:
            if out_path.lower().endswith(".svg"):
                shutil.copyfile(svg_tmp, out_path)
            else:
                conv = _svg_to_png(svg_tmp, out_path, scale)
                if conv:
                    return f"@plantuml/core + {conv}（纯 JS，离线）"
                log("  SVG 已生成但缺 PNG 转换器（建议 pip install cairosvg）")
                shutil.copyfile(svg_tmp, out_path + ".svg")
                raise RuntimeError(
                    "@plantuml/core 已生成 SVG，但缺少 SVG→PNG 转换器。\n"
                    "请安装其一：pip install cairosvg / brew install librsvg / inkscape / imagemagick\n"
                    f"SVG 文件已保存: {out_path}.svg"
                )
            return "@plantuml/core（纯 JS，离线）"
        engine_errors.append("@plantuml/core: " + (r.stderr or r.stdout).strip()[:400])
        log("  @plantuml/core 失败: " + (r.stderr or r.stdout).strip()[:500])

    # 3) java -jar plantuml.jar（本地，离线可用）
    jar = plantuml_jar or _find_plantuml_jar()
    if jar and _which("java"):
        log(f"[render] 使用 java -jar {jar}")
        cmd = ["java", "-jar", jar, fmt, "-charset", "UTF-8", *extra, in_file]
        r = _run(cmd, tmpdir, timeout=240)
        if r.returncode == 0 and check_out():
            shutil.copyfile(out_full, out_path)
            return f"java -jar {os.path.basename(jar)}"
        engine_errors.append(f"java -jar: " + (r.stderr or r.stdout).strip()[:400])
        log("  java -jar 失败: " + (r.stderr or r.stdout).strip()[:500])

    # 4) docker plantuml/plantuml（仅镜像已缓存；allow_network 才允许拉取）
    if _which("docker") and (allow_network or _docker_image_cached("plantuml/plantuml")):
        log("[render] 使用 docker plantuml/plantuml")
        cmd = [
            "docker", "run", "--rm",
            "-v", f"{tmpdir}:/work",
            "plantuml/plantuml",
            fmt, "-charset", "UTF-8", *extra, "/work/diagram.puml",
        ]
        r = _run(cmd, tmpdir, timeout=300)
        if r.returncode == 0 and check_out():
            shutil.copyfile(out_full, out_path)
            return "docker plantuml/plantuml"
        engine_errors.append("docker: " + (r.stderr or r.stdout).strip()[:400])
        log("  docker plantuml 失败: " + (r.stderr or r.stdout).strip()[:500])

    # 引擎执行过但全部失败 → 大概率源码语法错误（不走兜底，让调用方修源码）
    if engine_errors:
        raise DiagramSyntaxError(
            "PlantUML 引擎已执行但渲染失败（大概率源码语法错误）：\n  "
            + "\n  ".join(engine_errors)
        )

    hints = [
        "未找到可用的 PlantUML 渲染引擎（离线优先，未联网拉取）。请安装其一：",
        "  npm install -g @plantuml/core     （推荐，纯 JS 离线，无需 Java）",
        "  macOS:  brew install plantuml",
        "  Debian/Ubuntu:  sudo apt install plantuml",
        "  或下载 jar 后用 --plantuml-jar 指定: https://github.com/plantuml/plantuml/releases",
        "  （在线时可用 --allow-network 允许 docker 自动拉取镜像）",
    ]
    if _plantuml_core_available() and not _which("java"):
        hints.append("（已检测到 @plantuml/core，但缺 SVG→PNG 转换器：pip install cairosvg）")
    elif _which("java") and _find_plantuml_jar() is None:
        hints.append("（检测到 java，安装 plantuml.jar 后即可使用）")
    if _which("java") is None and not _plantuml_core_available():
        hints.append("（未检测到 java，推荐用 @plantuml/core 纯 JS 方案）")
    raise RuntimeError("\n".join(hints))


def cmd_check() -> int:
    """环境诊断：列出可用渲染引擎与离线能力。"""
    def _yn(b: bool) -> str:
        return "✓ 可用" if b else "✗ 未安装"

    lines = [
        "lite-work diagram 渲染环境诊断（离线优先）",
        "",
        f"  Python        : {sys.version.split()[0]}",
        f"  matplotlib    : {_yn(_has_matplotlib())}（内置兜底渲染依赖）",
        "",
        "  -- PlantUML --",
        f"  @plantuml/core: {_yn(_plantuml_core_available())}（纯 JS，推荐）",
        f"  plantuml CLI  : {_yn(_which('plantuml'))}",
        f"  java          : {_yn(_which('java'))}",
        f"  plantuml.jar  : {os.path.basename(_find_plantuml_jar()) if _find_plantuml_jar() else '✗ 未找到'}",
        f"  docker 镜像   : {'✓ 已缓存' if _docker_image_cached('plantuml/plantuml') else '✗ 未缓存'}",
        "",
        "  -- Mermaid --",
        f"  mmdc          : {_yn(_which('mmdc'))}",
        f"  npx           : {_yn(_which('npx'))}",
        f"  npx 包缓存    : {'✓ 已缓存' if _npx_pkg_cached('@mermaid-js/mermaid-cli') else '✗ 未缓存'}",
        f"  docker 镜像   : {'✓ 已缓存' if _docker_image_cached('minlag/mermaid-cli') else '✗ 未缓存'}",
        "",
        "  -- SVG→PNG 转换器（@plantuml/core 输出 SVG 后转 PNG 用） --",
        f"  @resvg/resvg-js: {_yn(_resvg_available())}（npm install -g @resvg/resvg-js）",
        f"  cairosvg      : {_yn(_has_cairosvg())}（pip install cairosvg）",
        f"  rsvg-convert  : {_yn(_which('rsvg-convert'))}（brew install librsvg）",
        f"  inkscape      : {_yn(_which('inkscape'))}",
        f"  magick/convert: {_yn(_which('magick') or _which('convert'))}（ImageMagick）",
        f"  qlmanage      : {_yn(_which('qlmanage'))}（macOS 自带）",
        "",
        "离线策略：优先本地引擎（@plantuml/core / plantuml CLI / mmdc）；",
        "无任何引擎时自动使用内置 matplotlib 兜底渲染，保证离线必出图。",
        "",
        "提示：联网时运行 `render_diagram.py --install` 可一次预装缺失引擎",
        "（npm 安装 @plantuml/core + @resvg/resvg-js + mmdc），装后完全离线可用。",
    ]
    for ln in lines:
        print(ln)
    return 0


def _has_matplotlib() -> bool:
    try:
        import matplotlib  # noqa: F401
        return True
    except ImportError:
        return False


def _has_cairosvg() -> bool:
    try:
        import cairosvg  # noqa: F401
        return True
    except Exception:
        # cairosvg 依赖系统 libcairo，缺失时 import 抛 OSError/ImportError
        return False


PLANTUML_JAR_URL = (
    "https://github.com/plantuml/plantuml/releases/download/"
    "v1.2024.8/plantuml-1.2024.8.jar"
)


def _download(url: str, dest: str, timeout: int = 300) -> bool:
    """用标准库下载文件（不依赖 curl/wget），成功返回 True。"""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "lite-work-diagram"})
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)
        return os.path.isfile(dest) and os.path.getsize(dest) > 0
    except Exception as exc:
        log(f"  下载失败: {exc}")
        return False


def cmd_install() -> int:
    """联网预装缺失引擎（下载 plantuml.jar + 全局安装 mmdc），之后完全离线可用。

    需要网络；无网络时应跳过此命令（脚本仍可离线用内置兜底渲染）。
    """
    print("lite-work diagram 引擎预装（需要网络，装一次后离线可用）")
    ok = True

    # ---- PlantUML（纯 JS 引擎 @plantuml/core，无需 Java）----
    print("\n[1/3] PlantUML 引擎")
    if _which("plantuml"):
        print("  ✓ 已安装 plantuml CLI，跳过")
    elif _plantuml_core_available():
        print("  ✓ @plantuml/core 已可用，跳过")
    else:
        if not _which("node"):
            print("  ✗ 未检测到 node，无法安装 @plantuml/core（需要 Node.js）")
            print("    备选：brew install plantuml 或下载 plantuml.jar 放到 ~/plantuml.jar")
            ok = False
        elif not _which("npm"):
            print("  ✗ 未检测到 npm，无法全局安装 @plantuml/core")
            ok = False
        else:
            print("  · npm install -g @plantuml/core（纯 JS PlantUML 引擎，无需 Java）...")
            try:
                r = subprocess.run(
                    ["npm", "install", "-g", "@plantuml/core"],
                    capture_output=True, text=True, timeout=900,
                )
                if r.returncode == 0 and _plantuml_core_available():
                    print("  ✓ @plantuml/core 安装完成")
                else:
                    print("  ✗ @plantuml/core 安装失败: " + (r.stderr or r.stdout).strip()[:400])
                    ok = False
            except subprocess.TimeoutExpired:
                print("  ✗ @plantuml/core 安装超时（>900s）")
                ok = False

    # ---- SVG→PNG 转换器（resvg 优先，供 @plantuml/core 输出 PNG 用）----
    print("\n[2/3] SVG→PNG 转换器")
    if _resvg_available():
        print("  ✓ @resvg/resvg-js 已可用，跳过")
    else:
        if not _which("npm"):
            print("  ✗ 未检测到 npm，无法安装 @resvg/resvg-js")
            ok = False
        else:
            print("  · npm install -g @resvg/resvg-js（跨平台 SVG→PNG）...")
            try:
                r = subprocess.run(
                    ["npm", "install", "-g", "@resvg/resvg-js"],
                    capture_output=True, text=True, timeout=600,
                )
                if r.returncode == 0 and _resvg_available():
                    print("  ✓ @resvg/resvg-js 安装完成")
                else:
                    print("  ✗ @resvg/resvg-js 安装失败: " + (r.stderr or r.stdout).strip()[:400])
                    print("    备选：pip install cairosvg / brew install librsvg / inkscape / imagemagick")
                    ok = False
            except subprocess.TimeoutExpired:
                print("  ✗ @resvg/resvg-js 安装超时（>600s）")
                ok = False

    # ---- Mermaid ----
    print("\n[3/3] Mermaid")
    if _which("mmdc"):
        print("  ✓ 已安装 mmdc（@mermaid-js/mermaid-cli），跳过")
    else:
        if not _which("node"):
            print("  ✗ 未检测到 node，Mermaid 需要 Node.js >= 18")
            ok = False
        elif not _which("npm"):
            print("  ✗ 未检测到 npm，无法全局安装 mermaid-cli")
            ok = False
        else:
            print("  · npm install -g @mermaid-js/mermaid-cli ...")
            try:
                r = subprocess.run(
                    ["npm", "install", "-g", "@mermaid-js/mermaid-cli"],
                    capture_output=True, text=True, timeout=900,
                )
                if r.returncode == 0 and _which("mmdc"):
                    print("  ✓ mmdc 安装完成")
                else:
                    print("  ✗ mmdc 安装失败: " + (r.stderr or r.stdout).strip()[:400])
                    ok = False
            except subprocess.TimeoutExpired:
                print("  ✗ mmdc 安装超时（>900s）")
                ok = False

    print("\n完成。运行 `render_diagram.py --check` 确认各项为 ✓。")
    return 0 if ok else 1


def _fallback_render(source: str, chart_type: str, out_path: str, scale: int) -> str:
    """调用内置 matplotlib 兜底渲染器（离线保证）。"""
    try:
        import importlib.util
        here = os.path.dirname(os.path.abspath(__file__))
        fb_path = os.path.join(here, "fallback_render.py")
        spec = importlib.util.spec_from_file_location("litework_fallback_render", fb_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.render_fallback(source, chart_type, out_path, scale)
    except Exception as exc:
        raise RuntimeError(f"内置兜底渲染失败: {exc}") from exc


def render_mermaid(source: str, out_path: str, scale: int, tmpdir: str,
                   allow_network: bool = False) -> str:
    """渲染 Mermaid 源码，返回渲染引擎说明。离线优先，仅本地引擎。"""
    in_file = os.path.join(tmpdir, "diagram.mmd")
    with open(in_file, "w", encoding="utf-8") as f:
        f.write(source)

    # mmdc 的 -o 直接控制输出路径；SVG 时忽略 scale
    mmdc_out = out_path if os.path.isabs(out_path) else os.path.abspath(out_path)
    scale_arg = ["-s", str(scale)] if not out_path.lower().endswith(".svg") else []

    # 1) mmdc CLI（本地全局安装，离线可用）
    if _which("mmdc"):
        log("[render] 使用 mmdc")
        cmd = ["mmdc", "-i", in_file, "-o", mmdc_out, *scale_arg]
        r = _run(cmd, tmpdir, timeout=300)
        if r.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            return "mmdc"
        log("  mmdc 失败: " + (r.stderr or r.stdout).strip()[:500])

    # 2) npx @mermaid-js/mermaid-cli（仅本地已缓存包；allow_network 才允许联网安装）
    if _which("npx") and (allow_network or _npx_pkg_cached("@mermaid-js/mermaid-cli")):
        log("[render] 使用 npx @mermaid-js/mermaid-cli")
        npx_opt = [] if allow_network else ["--no-install"]
        cmd = [
            "npx", *npx_opt, "--yes", "@mermaid-js/mermaid-cli",
            "-i", in_file, "-o", mmdc_out, *scale_arg,
        ]
        r = _run(cmd, tmpdir, timeout=600)
        if r.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            return "npx @mermaid-js/mermaid-cli"
        log("  npx 失败: " + (r.stderr or r.stdout).strip()[:500])

    # 3) docker minlag/mermaid-cli（仅镜像已缓存；allow_network 才允许拉取）
    if _which("docker") and (allow_network or _docker_image_cached("minlag/mermaid-cli")):
        log("[render] 使用 docker minlag/mermaid-cli")
        out_container = "/data/" + os.path.basename(mmdc_out)
        cmd = [
            "docker", "run", "--rm",
            "-v", f"{os.path.dirname(mmdc_out)}:/data",
            "minlag/mermaid-cli",
            "-i", "/data/diagram.mmd", "-o", out_container, *scale_arg,
        ]
        r = _run(cmd, tmpdir, timeout=300)
        if r.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            return "docker minlag/mermaid-cli"
        log("  docker mermaid 失败: " + (r.stderr or r.stdout).strip()[:500])

    hints = [
        "未找到可用的 Mermaid 渲染引擎（离线优先，未联网拉取）。请安装其一：",
        "  npm install -g @mermaid-js/mermaid-cli   （需要 Node.js >= 18）",
        "  （在线时可用 --allow-network 允许 npx/docker 自动拉取）",
        "mmdc 依赖本机 Chrome（puppeteer）。若缺浏览器，执行：",
        "  npx puppeteer browsers install chrome",
    ]
    raise RuntimeError("\n".join(hints))


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把 PlantUML / Mermaid 源码渲染为 PNG/SVG 图片（离线优先）",
    )
    parser.add_argument("input", nargs="?",
                        help="源码文本、或 .puml/.mmd/.md 文件路径（--check 时省略）")
    parser.add_argument("-o", "--output",
                        help="输出图片路径（.png 或 .svg；--check 时省略）")
    parser.add_argument("--type", default="auto",
                        choices=["auto", "plantuml", "mermaid"],
                        help="图表类型（默认 auto 自动识别）")
    parser.add_argument("--scale", type=int, default=2,
                        help="PNG 放大倍数（默认 2，SVG 忽略）")
    parser.add_argument("--plantuml-jar", default=None,
                        help="显式指定 plantuml.jar 路径")
    parser.add_argument("--check", action="store_true",
                        help="环境诊断：列出可用引擎与离线能力，不渲染")
    parser.add_argument("--install", action="store_true",
                        help="联网预装缺失引擎（plantuml.jar / mmdc），装一次后离线可用")
    parser.add_argument("--allow-network", action="store_true",
                        help="允许联网（npx 安装包 / docker 拉取镜像）")
    parser.add_argument("--no-fallback", action="store_true",
                        help="禁用内置 matplotlib 兜底渲染（无引擎时直接报错）")
    args = parser.parse_args(argv)

    if args.check:
        return cmd_check()

    if args.install:
        return cmd_install()

    if not args.input or not args.output:
        parser.error("需要 input 与 -o/--output（或使用 --check / --install）")

    # 读取输入
    raw = args.input
    input_path = args.input
    if os.path.isfile(args.input):
        input_path = args.input
        try:
            raw = Path(args.input).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raw = Path(args.input).read_text(encoding="utf-8", errors="replace")

    # 识别类型并提取源码
    try:
        chart_type = detect_type(raw, input_path, args.type)
    except ValueError as exc:
        log(f"[error] {exc}")
        return 1
    source = extract_source(raw, chart_type)
    if not source.strip():
        log("[error] 输入中未找到图表源码（空的 @startuml/```mermaid 块？）")
        return 1

    # 渲染前预校验（写完必校验）：配对错误阻断，其余告警
    if chart_type == "plantuml":
        lint_errors, lint_warnings = lint_plantuml(source)
        for w in lint_warnings:
            log(f"[lint] ⚠ {w}")
        if lint_errors:
            for e in lint_errors:
                log(f"[lint] ✗ {e}")
            log("[error] PlantUML 源码预校验未通过，请修正后重试")
            return 1

    # 输出目录
    out_path = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    engine = None
    try:
        with tempfile.TemporaryDirectory(prefix="litework-diagram-") as tmpdir:
            if chart_type == "plantuml":
                engine = render_plantuml(
                    source, out_path, args.scale, tmpdir, args.plantuml_jar,
                    allow_network=args.allow_network,
                )
            else:
                engine = render_mermaid(
                    source, out_path, args.scale, tmpdir,
                    allow_network=args.allow_network,
                )
    except DiagramSyntaxError as exc:
        # 源码语法错误：不走兜底（兜底只会输出源码文本图，掩盖真实问题）
        log(f"[error] 图表源码渲染失败（语法错误，请修正源码后重试）：\n{exc}")
        if chart_type == "plantuml":
            log("\n" + PLANTUML_ERROR_HINTS)
        return 1
    except RuntimeError as exc:
        if args.no_fallback:
            log(f"[error] 渲染失败：\n{exc}")
            return 1
        # 离线兜底：无外部引擎时用内置 matplotlib 渲染，保证必出图
        log(f"[warn] 外部引擎不可用，改用内置 matplotlib 兜底渲染：\n{exc}")
        try:
            engine = _fallback_render(source, chart_type, out_path, args.scale)
        except RuntimeError as fb_exc:
            log(f"[error] {fb_exc}")
            return 1

    if not os.path.isfile(out_path) or os.path.getsize(out_path) <= 0:
        log("[error] 渲染结果为空文件，请检查源码或使用 --check 诊断环境")
        return 1

    size = os.path.getsize(out_path)
    log(f"[ok] 已生成图片（{engine}）→ {out_path} ({size} bytes)")
    print(out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
