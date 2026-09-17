#!/usr/bin/env python3
"""运行时自检：报告本技能的依赖是否就绪，并给出分平台安装指引。

本技能分三层能力，对环境的依赖不同：

  ① Markdown / LaTeX 输入 → 生成「可编译的 LaTeX 工程」
     只需 Python（3.9+）与 PyYAML。
  ②a DOCX / PDF / PPTX / XLSX 输入 → 解析成中间表示
     纯 Python：python-docx / pymupdf / openpyxl / python-pptx，**不需要 LibreOffice**。
  ②b 旧格式转换 / xlsx 公式重算 / pptx 缩略图（可选增强）
     需要 LibreOffice（命令行 soffice）；缺失时可用 textutil / pandoc 部分替代。
  ③ LaTeX 工程 → 编译出 PDF
     额外需要 TeX 发行版（pdflatex / xelatex / lualatex + bibtex/biber）。
     没有 TeX 也能用：把工程上传到 Overleaf 编译即可。

用法：
    python3 scripts/selfcheck.py

退出码：0 = ① 可用；1 = 连 ① 都不可用（缺核心 Python 包）
"""

import platform
import shutil
import sys

PY_PACKAGES = [
    # (import 名, 发行包名, 用途, 是否核心)
    ("yaml", "PyYAML", "读取模板/阈值配置", True),
    ("docx", "python-docx", "解析 DOCX 输入", False),
    ("pymupdf", "pymupdf", "解析 PDF 输入", False),
    ("openpyxl", "openpyxl", "解析 XLSX 输入", False),
    ("pptx", "python-pptx", "解析 PPTX 输入", False),
]

EXTERNAL_TOOLS = [
    ("pdflatex", "TeX 发行版", "编译 PDF", False),
    ("xelatex", "TeX 发行版", "编译 PDF（含中文/特殊字体）", False),
    ("lualatex", "TeX 发行版", "编译 PDF", False),
    ("bibtex", "TeX 发行版", "参考文献编译", False),
    ("biber", "TeX 发行版", "参考文献编译（biblatex）", False),
    ("soffice", "LibreOffice", "旧格式转换 / xlsx 重算 / pptx 缩略图（可选）", False),
    ("textutil", "macOS 自带", ".doc/.rtf → docx（soffice 的替代）", False),
    ("pandoc", "Pandoc", "docx→PDF、接受修订（soffice 的替代）", False),
]

TEX_GUIDE = """      安装 TeX 发行版：
        macOS   : brew install --cask mactex-no-gui   （或 mactex，约 5GB）
                  # 没有 Homebrew 时用 MacPorts：
                  sudo port install texlive-latex-extra texlive-xetex \\
                       texlive-lang-chinese texlive-publishers latexmk dvisvgm
        Windows : MiKTeX  https://miktex.org/download  （或 TeX Live）
        Linux   : sudo apt install texlive-full   /   sudo dnf install texlive-scheme-full
      中文排版需 xelatex + ctex/fandol 字体（MacPorts 的 texlive-lang-chinese 已含）。
      不想安装？把生成的 LaTeX 工程打包上传到 Overleaf  https://www.overleaf.com  直接编译。"""

OFFICE_GUIDE = """      安装 LibreOffice（只影响「可选增强」，不影响四类输入解析）：
        macOS   : 官方 dmg  https://www.libreoffice.org/download/  （Apple Silicon 选 aarch64）
                  装完把命令行入口放进 PATH：
                    sudo ln -sf /Applications/LibreOffice.app/Contents/MacOS/soffice /usr/local/bin/soffice
                  MacPorts: sudo port install libreoffice
                  Homebrew: brew install --cask libreoffice
        Windows : https://www.libreoffice.org/download/
        Linux   : sudo apt install libreoffice   /   sudo dnf install libreoffice

      没有 LibreOffice 时的降级路径：
        · legacy .doc / .rtf → docx  ：macOS 系统自带 textutil -convert docx file.doc
        · docx → PDF（视觉检查）     ：pandoc -o out.pdf in.docx（需已装 TeX）
        · 接受 Word 修订             ：pandoc --track-changes=accept -o out.docx in.docx（近似）
        · xlsx 公式重算 / pptx 缩略图：无替代，需 LibreOffice"""


def rule(char="-", width=68):
    return char * width


def main():
    print(rule("="))
    print("academic-paper-engineering 运行时自检")
    print(rule("="))

    print("\n[1] Python 运行时")
    print("  版本: %s" % platform.python_version())
    print("  路径: %s" % sys.executable)
    py_ok = sys.version_info >= (3, 9)
    print("  状态: %s" % ("OK（需要 3.9+）" if py_ok else "需要 Python 3.9 及以上"))

    print("\n[2] Python 包")
    missing_core = []
    missing_extra = []
    for module, dist, use, core in PY_PACKAGES:
        try:
            __import__(module)
            print("  [有] %-14s %s" % (dist, use))
        except ImportError:
            tag = "核心" if core else "可选"
            print("  [无] %-14s %s  <%s>" % (dist, use, tag))
            (missing_core if core else missing_extra).append(dist)

    print("\n[3] 外部工具")
    found_tex = []
    has_office = False
    for cmd, belongs, use, _core in EXTERNAL_TOOLS:
        path = shutil.which(cmd)
        if path:
            print("  [有] %-10s %s" % (cmd, path))
            if belongs == "TeX 发行版":
                found_tex.append(cmd)
            elif cmd == "soffice":
                has_office = True
        else:
            print("  [无] %-10s %s  <%s>" % (cmd, use, belongs))

    # ---------------------------------------------------------------- 能力结论
    print("\n" + rule("="))
    print("能力评估")
    print(rule("="))

    python_layer = py_ok and not missing_core
    print("\n① Markdown / LaTeX → 可编译 LaTeX 工程")
    print("   状态: %s" % ("可用" if python_layer else "不可用（缺核心 Python 包）"))

    parse_layer = python_layer and not missing_extra
    print("\n②a DOCX / PDF / PPTX / XLSX 输入解析（纯 Python，不需要 LibreOffice）")
    if parse_layer:
        print("   状态: 可用")
    elif missing_extra:
        print("   状态: 不可用（缺: %s）" % " ".join(missing_extra))
    else:
        print("   状态: 不可用")

    print("\n②b 旧格式转换 / xlsx 公式重算 / pptx 缩略图（可选增强）")
    if has_office:
        print("   状态: 可用（LibreOffice soffice）")
    else:
        alts = [c for c in ("textutil", "pandoc") if shutil.which(c)]
        print("   状态: 降级（未装 LibreOffice%s）" % (
            "；可用替代: " + ", ".join(alts) if alts else "；也没有 textutil / pandoc 替代"))

    tex_layer = python_layer and bool(found_tex)
    print("\n③ 本地编译出 PDF")
    print("   状态: %s" % ("可用" if tex_layer else "不可用（未检测到 TeX 引擎）"))

    # ---------------------------------------------------------------- 补齐建议
    todo = []
    if missing_core or missing_extra:
        todo.append("  pip install %s" % " ".join(missing_core + missing_extra))
    if not found_tex:
        todo.append(TEX_GUIDE)
    if not has_office:
        todo.append(OFFICE_GUIDE)

    if todo:
        print("\n" + rule("="))
        print("要补齐的话：")
        print(rule("="))
        for item in todo:
            print(item)
    else:
        print("\n依赖完备，核心能力（①②a③）与可选增强（②b）全部可用。")

    if not tex_layer:
        print("\n提示：没有 TeX 也能正常使用本技能 —— 会交付「可编译的 LaTeX 工程」，"
              "本地装 TeX 后可自行编译，或上传 Overleaf 编译。")

    if not has_office:
        print("提示：没有 LibreOffice 不影响 ②a 解析；仅 xlsx 公式重算与 pptx 缩略图"
              "不可用，旧格式转换/PDF 预览可走 textutil 与 pandoc 降级。")

    return 0 if python_layer else 1


if __name__ == "__main__":
    sys.exit(main())
