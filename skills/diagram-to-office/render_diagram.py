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

    # 1) plantuml CLI（本地，离线可用）
    if _which("plantuml"):
        cmd = ["plantuml", fmt, "-charset", "UTF-8", *extra, in_file]
        log("[render] 使用 plantuml CLI")
        r = _run(cmd, tmpdir)
        if r.returncode == 0 and check_out():
            shutil.copyfile(out_full, out_path)
            return "plantuml CLI"
        log("  plantuml CLI 失败: " + (r.stderr or r.stdout).strip()[:500])

    # 2) java -jar plantuml.jar（本地，离线可用）
    jar = plantuml_jar or _find_plantuml_jar()
    if jar and _which("java"):
        log(f"[render] 使用 java -jar {jar}")
        cmd = ["java", "-jar", jar, fmt, "-charset", "UTF-8", *extra, in_file]
        r = _run(cmd, tmpdir, timeout=240)
        if r.returncode == 0 and check_out():
            shutil.copyfile(out_full, out_path)
            return f"java -jar {os.path.basename(jar)}"

    # 3) docker plantuml/plantuml（仅镜像已缓存；allow_network 才允许拉取）
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
        log("  docker plantuml 失败: " + (r.stderr or r.stdout).strip()[:500])

    hints = [
        "未找到可用的 PlantUML 渲染引擎（离线优先，未联网拉取）。请安装其一：",
        "  macOS:  brew install plantuml",
        "  Debian/Ubuntu:  sudo apt install plantuml",
        "  或下载 jar 后用 --plantuml-jar 指定: https://github.com/plantuml/plantuml/releases",
        "  （在线时可用 --allow-network 允许 docker 自动拉取镜像）",
    ]
    if _which("java") and _find_plantuml_jar() is None:
        hints.append("（检测到 java，安装 plantuml.jar 后即可使用）")
    if _which("java") is None:
        hints.append("（未检测到 java，PlantUML 需要 Java 运行时）")
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
        "离线策略：优先本地引擎（plantuml CLI / java+jar / mmdc）；",
        "无任何引擎时自动使用内置 matplotlib 兜底渲染，保证离线必出图。",
        "",
        "提示：联网时运行 `render_diagram.py --install` 可一次预装缺失引擎",
        "（下载 plantuml.jar 到 ~/plantuml.jar + 全局安装 mmdc），装后完全离线可用。",
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

    # ---- PlantUML ----
    print("\n[1/2] PlantUML")
    if _which("plantuml"):
        print("  ✓ 已安装 plantuml CLI，跳过")
    else:
        jar = _find_plantuml_jar()
        if jar:
            print(f"  ✓ 已找到 plantuml.jar: {jar}")
        else:
            dest = os.path.expanduser("~/plantuml.jar")
            print(f"  · 下载 plantuml.jar → {dest} ...")
            if _download(PLANTUML_JAR_URL, dest):
                print(f"  ✓ 下载完成（{os.path.getsize(dest)} bytes），脚本会自动发现")
            else:
                print("  ✗ plantuml.jar 下载失败，可手动下载放到 ~/plantuml.jar")
                ok = False

    # ---- Mermaid ----
    print("\n[2/2] Mermaid")
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
