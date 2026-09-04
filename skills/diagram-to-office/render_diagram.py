#!/usr/bin/env python3
"""图表渲染脚本：把 PlantUML / Mermaid 源码渲染为 PNG/SVG 图片。

用法示例：
    python3 render_diagram.py 架构.puml -o .outputs/架构图.png
    python3 render_diagram.py 流程.mmd -o .outputs/流程图.png --scale 3
    python3 render_diagram.py "@startuml ... @enduml" -o 图.png --type plantuml
    python3 render_diagram.py doc.md -o 图.svg            # 自动识别类型

支持引擎（自动探测，按顺序尝试）：
  PlantUML : plantuml CLI / java -jar plantuml.jar / docker plantuml/plantuml
  Mermaid  : mmdc / npx @mermaid-js/mermaid-cli / docker minlag/mermaid-cli

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


def render_plantuml(source: str, out_path: str, scale: int, tmpdir: str,
                    plantuml_jar: str | None) -> str:
    """渲染 PlantUML 源码，返回渲染引擎说明。"""
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

    # 1) plantuml CLI
    if _which("plantuml"):
        cmd = ["plantuml", fmt, "-charset", "UTF-8", *extra, in_file]
        log("[render] 使用 plantuml CLI")
        r = _run(cmd, tmpdir)
        if r.returncode == 0 and check_out():
            shutil.copyfile(out_full, out_path)
            return "plantuml CLI"
        log("  plantuml CLI 失败: " + (r.stderr or r.stdout).strip()[:500])

    # 2) java -jar plantuml.jar
    jar = plantuml_jar or _find_plantuml_jar()
    if jar and _which("java"):
        log(f"[render] 使用 java -jar {jar}")
        cmd = ["java", "-jar", jar, fmt, "-charset", "UTF-8", *extra, in_file]
        r = _run(cmd, tmpdir, timeout=240)
        if r.returncode == 0 and check_out():
            shutil.copyfile(out_full, out_path)
            return f"java -jar {os.path.basename(jar)}"

    # 3) docker plantuml/plantuml
    if _which("docker"):
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

    hints = [
        "未找到可用的 PlantUML 渲染引擎。请安装其一：",
        "  macOS:  brew install plantuml",
        "  Debian/Ubuntu:  sudo apt install plantuml",
        "  或下载 jar 后用 --plantuml-jar 指定: https://github.com/plantuml/plantuml/releases",
        "  或安装 Docker: https://docs.docker.com/get-docker/",
    ]
    if _which("java") and _find_plantuml_jar() is None:
        hints.append("（检测到 java，安装 plantuml.jar 后即可使用）")
    if _which("java") is None:
        hints.append("（未检测到 java，PlantUML 需要 Java 运行时）")
    raise RuntimeError("\n".join(hints))


def render_mermaid(source: str, out_path: str, scale: int, tmpdir: str) -> str:
    """渲染 Mermaid 源码，返回渲染引擎说明。"""
    in_file = os.path.join(tmpdir, "diagram.mmd")
    with open(in_file, "w", encoding="utf-8") as f:
        f.write(source)

    # mmdc 的 -o 直接控制输出路径；SVG 时忽略 scale
    mmdc_out = out_path if os.path.isabs(out_path) else os.path.abspath(out_path)
    scale_arg = ["-s", str(scale)] if not out_path.lower().endswith(".svg") else []

    # 1) mmdc CLI
    if _which("mmdc"):
        log("[render] 使用 mmdc")
        cmd = ["mmdc", "-i", in_file, "-o", mmdc_out, *scale_arg]
        r = _run(cmd, tmpdir, timeout=300)
        if r.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            return "mmdc"
        log("  mmdc 失败: " + (r.stderr or r.stdout).strip()[:500])

    # 2) npx @mermaid-js/mermaid-cli
    if _which("npx"):
        log("[render] 使用 npx @mermaid-js/mermaid-cli")
        cmd = [
            "npx", "--yes", "@mermaid-js/mermaid-cli",
            "-i", in_file, "-o", mmdc_out, *scale_arg,
        ]
        r = _run(cmd, tmpdir, timeout=600)
        if r.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            return "npx @mermaid-js/mermaid-cli"
        log("  npx 失败: " + (r.stderr or r.stdout).strip()[:500])

    # 3) docker minlag/mermaid-cli
    if _which("docker"):
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

    hints = [
        "未找到可用的 Mermaid 渲染引擎。请安装其一：",
        "  npm install -g @mermaid-js/mermaid-cli   （需要 Node.js >= 18）",
        "  或安装 Docker: https://docs.docker.com/get-docker/",
        "mmdc 依赖本机 Chrome（puppeteer）。若缺浏览器，执行：",
        "  npx puppeteer browsers install chrome",
    ]
    raise RuntimeError("\n".join(hints))


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把 PlantUML / Mermaid 源码渲染为 PNG/SVG 图片",
    )
    parser.add_argument("input", help="源码文本、或 .puml/.mmd/.md 文件路径")
    parser.add_argument("-o", "--output", required=True, help="输出图片路径（.png 或 .svg）")
    parser.add_argument("--type", default="auto",
                        choices=["auto", "plantuml", "mermaid"],
                        help="图表类型（默认 auto 自动识别）")
    parser.add_argument("--scale", type=int, default=2,
                        help="PNG 放大倍数（默认 2，SVG 忽略）")
    parser.add_argument("--plantuml-jar", default=None,
                        help="显式指定 plantuml.jar 路径")
    args = parser.parse_args(argv)

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

    # 输出目录
    out_path = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="litework-diagram-") as tmpdir:
        try:
            if chart_type == "plantuml":
                engine = render_plantuml(source, out_path, args.scale, tmpdir, args.plantuml_jar)
            else:
                engine = render_mermaid(source, out_path, args.scale, tmpdir)
        except RuntimeError as exc:
            log(f"[error] 渲染失败：\n{exc}")
            return 1

    size = os.path.getsize(out_path)
    log(f"[ok] 已生成图片（{engine}）→ {out_path} ({size} bytes)")
    print(out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
