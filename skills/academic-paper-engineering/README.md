# Academic Paper Engineering（学术论文工程化排版）

[lite-work](https://github.com/LaynePeng/lite-work) 社区技能：把**异构论文材料**
（DOCX / PDF / Markdown / LaTeX / PPTX / XLSX）转换成**结构正确、可编译的 LaTeX 工程**，
内置 8+1 套期刊模板，可自动编译出 PDF，并附 12 项质量审查。

> 移植自 [Hongyuan-Lu/academic-paper-engineering](https://github.com/Hongyuan-Lu/academic-paper-engineering)，
> MIT License，Copyright (c) 2026 Hongyuan Lu（见 `LICENSE.txt`）。

## 三层能力与前置条件（重要）

本技能的能力是分层的，对**用户机器**的依赖不同 —— 装了什么用什么：

| # | 能力 | 需要 | 缺失时的表现 |
| --- | --- | --- | --- |
| ① | Markdown / LaTeX → 可编译 LaTeX 工程 | Python **3.9+**、PyYAML | 不可用（核心能力） |
| ②a | DOCX / PDF / PPTX / XLSX 输入**解析** | 纯 Python：`python-docx` / `pymupdf` / `openpyxl` / `python-pptx` | 对应格式不可用，其余正常 |
| ②b | 旧格式转换 / xlsx 公式重算 / pptx 缩略图（可选增强） | **LibreOffice**（命令行 `soffice`） | 见下方降级路径，不影响 ②a |
| ③ | 本地编译出 PDF | 额外 **TeX 发行版**（`pdflatex`/`xelatex` + `bibtex`） | **自动跳过编译，仍交付 LaTeX 工程** |

> ②a **不需要 LibreOffice** —— 四类输入都是纯 Python 解析。LibreOffice 只服务于 ②b。

**动手前先自检**（会逐项报告并给出针对性的安装指引）：

```bash
cd "<技能目录>" && python3 scripts/selfcheck.py
```

### 没有 TeX 怎么办？

这是**预期情况**，不是故障：技能会正常产出「可编译的 LaTeX 工程」，只是跳过最后一步编译。
两条出路：

- **方案 A（本地编译）**：安装 TeX 发行版后在工程目录执行 `pdflatex main.tex`（重复 2–3 次解析交叉引用）
  - macOS: `brew install --cask mactex-no-gui`（或 `mactex`，约 5GB）
  - macOS（无 Homebrew，用 MacPorts）：
    ```bash
    sudo port install texlive-latex-extra texlive-xetex texlive-lang-chinese \
         texlive-publishers latexmk dvisvgm
    ```
  - Windows: MiKTeX <https://miktex.org/download> 或 TeX Live
  - Linux: `sudo apt install texlive-full` / `sudo dnf install texlive-scheme-full`
- **方案 B（免安装）**：把整个 LaTeX 工程目录打包上传到 [Overleaf](https://www.overleaf.com) 直接编译。

中文排版走 `xelatex` + `ctex` + `fandol` 字体（MacPorts 的 `texlive-lang-chinese` 已含）。

### 没有 LibreOffice 怎么办？

②a（四类输入解析）不受影响；只有 ②b 的几个可选动作需要它。降级路径：

| ②b 动作 | 没有 LibreOffice 时 |
| --- | --- |
| legacy `.doc` / `.rtf` → docx | macOS 系统自带：`textutil -convert docx file.doc` |
| docx → PDF（视觉检查） | `pandoc -o out.pdf in.docx`（需已装 TeX） |
| 接受 Word 修订 | `pandoc --track-changes=accept -o out.docx in.docx`（近似，段落合并场景有差异） |
| xlsx 公式重算 / pptx 缩略图 | 无替代，需 LibreOffice |

安装 LibreOffice 后，把 CLI 放进 PATH（macOS）：

```bash
sudo ln -sf /Applications/LibreOffice.app/Contents/MacOS/soffice /usr/local/bin/soffice
```

## 支持的模板

Elsevier（elsarticle）、Cell Press（cas-sc/cas-dc）、Springer（sn-jnl）、MDPI、Frontiers、
Taylor & Francis、Wiley、arXiv（NeurIPS）、IEEE（规范定义）。
模板速查见 `assets/templates/CHEATSHEET.md`。

## 用法（Agent 视角）

```
> 把这篇论文（paper.md）排成 Elsevier 双栏格式
> 把这篇 Word 论文转成可编译的 LaTeX 工程
> 把这篇 IEEE 论文迁移到 Springer 模板
> 把这份中文学术稿翻成英文并按 MDPI 模板排版
```

## 本仓库相对上游的适配

1. **Python 3.9 兼容**：上游 `scripts/common/office/validate.py` 使用了 `match` 语句
   （仅 Python 3.10+ 合法），在 macOS 自带 python3（3.9）下直接 `SyntaxError`；
   已改为 `if/elif`。现在 51 个 `.py` 在 3.9 下全部可编译。
2. **跨平台路径**：`scripts/docx/accept_changes.py` 里写死了 `/tmp/libreoffice_docx_profile`，
   Windows 上不成立；改为 `tempfile.gettempdir()`。
3. **无 TeX 时优雅降级**（核心）：`src/latex/compiler.py` 增加引擎探测与回退
   （pdflatex → xelatex → lualatex），都没有时返回 `skipped=True` + 分平台安装指引，
   而不是抛错中断 —— 保证「没装 TeX 的用户也能拿到 LaTeX 工程」。
4. **新增 `scripts/selfcheck.py`**：逐项报告 Python 版本 / Python 包 / 外部工具，
   输出「三层能力可用性」评估与针对性的补齐指引。
5. **资产瘦身**：上游 `assets/templates/` 6.9MB 中，手册、样例 PDF、logo EPS、示例 JPG/DOCX
   等**编译不需要**的文件已剔除（保留全部 `.cls/.sty/.bst/.bib/.tex`），
   技能体积 8.6MB → **3.9MB**。
6. **frontmatter 扩展**：补 `triggers` 与顶层 `version`（host 索引与社区更新检测需要），
   并在正文顶部写明「技能目录约定 + 前置条件 + 无 TeX 时的行为」。
7. `requirements.txt` 去掉 `pytest`（开发依赖，运行时不需要）。

## 目录结构

```text
academic-paper-engineering/
├── SKILL.md              # Agent 加载的指令（工作流 + 前置条件 + 降级约定）
├── README.md             # 本文件
├── LICENSE.txt           # MIT（上游版权）
├── requirements.txt      # 运行时 Python 依赖
├── assets/templates/     # 8+1 套期刊 LaTeX 模板（.cls/.sty/.bst/.tex/.bib）
├── references/           # 提示词、规则、JSON Schema、配置
├── scripts/              # 各格式处理脚本 + selfcheck.py（含 office 工具集）
└── src/                  # Python 引擎：parsers / processors / latex / qa
```

## 依赖说明

`requirements.txt` 里的包（PyYAML / python-docx / pymupdf / openpyxl / python-pptx）
**在 lite-work 主程序中已内置**；技能由系统 Python 执行时若缺失，安装时会依 `requirements.txt` 自动安装。
TeX 与 LibreOffice 属于**外部工具**，无法打包分发，只能由用户按上面的指引自行安装（或走 Overleaf）。
