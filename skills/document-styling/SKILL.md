---
name: document-styling
description: 文档美化：为 Markdown 或已有 docx 套用专业排版主题，生成美观的 Word/PDF（封面、页眉页脚页码、标题配色、表格底纹、中文断行）
triggers: 美化,排版,样式,主题,风格,好看,专业,太丑,文档美化,换风格
version: "1.0.0"
---

# 文档美化技能

当用户希望文档**更好看/换风格/要专业排版**，或需要把 Markdown 变成可直接交付的
Word/PDF 时，使用本技能。核心是「**主题包（themes）↔ 渲染器（scripts）**」解耦：
换风格 = 换一个主题文件，不改代码。

## 目录约定

本技能目录（下称 `${SKILL_DIR}`）：

```text
document-styling/
├── SKILL.md
├── requirements.txt          # 技能运行依赖（系统 Python 安装）
├── themes/                   # 主题包（可增删改）
│   ├── _schema.md            # 主题字段说明
│   ├── business-blue.md      # 商务蓝（默认）
│   ├── academic-gray.md      # 学术灰
│   ├── gov-red.md            # 政务红
│   ├── modern-minimal.md     # 现代极简
│   └── tech-dark.md          # 科技青
├── scripts/
│   ├── themes.py             # 主题加载/列举/预览（含社区 theme-factory 适配）
│   ├── _md.py                # Markdown 解析（共用）
│   ├── docx_theme.py         # python-docx 主题应用（样式/边距/页脚/表格）
│   ├── md2docx.py            # Markdown → 美化 docx
│   ├── apply_docx_theme.py   # 给已有 docx 套主题（美化 office-plugin 产物）
│   └── md2pdf.py             # Markdown → 美化 PDF
└── assets/html/print.css       # 可选：Chrome 引擎的打印样式
```

## 工作流程

1. **列主题**：
   ```bash
   python3 "${SKILL_DIR}/scripts/themes.py" list
   ```
2. **让用户选风格**：用 ask_user 给出主题选项（label + description），未指定时用
   `business-blue`。
3. **渲染**：
   - Markdown → Word：`python3 "${SKILL_DIR}/scripts/md2docx.py" <输入.md> --theme <name> --out "产出物/xxx.docx"`
   - Markdown → PDF：`python3 "${SKILL_DIR}/scripts/md2pdf.py" <输入.md> --theme <name> --out "产出物/xxx.pdf"`
   - **已有 docx 美化**（最常见：先 `docx_create` 产出，再美化）：
     `python3 "${SKILL_DIR}/scripts/apply_docx_theme.py" "产出物/xxx.docx" --theme <name>`
4. **回复**：文件路径 + 用了哪套主题 + 可换的主题清单。

> `md2docx.py` / `md2pdf.py` 的输入也可以是 `-`（从 stdin 读 Markdown），
> 便于与其它工具串联。

## PDF 引擎选择

| 引擎 | 页脚页码 | 中文可复制/检索 | 观感 | 依赖 |
| --- | --- | --- | --- | --- |
| `reportlab`（默认） | ✅ | ✅ 准确 | 中上 | 无（随主程序捆绑） |
| `chrome` | ❌ | ⚠️ 可能退化为兼容字形（如 `文`→`⽂`） | 更好 | 系统 Chrome |
| `auto` | 视是否检测到 Chrome | 同 chrome | — | — |

> 需要页码、或要求 PDF 里中文可正常复制/检索 → 用 `reportlab`（默认）。
> 只追求排版观感、可接受无页码与检索退化 → `--engine chrome`。

## 主题来源（三种，优先级从低到高）

1. **内置预设**：本技能 `themes/*.md`（版权自持，可随仓库分发）。
2. **用户自定义**：`~/.lite-work/themes/*.md`（同格式，放进去即出现）。
3. **社区主题即插即用**：若用户已自行安装 `anthropics/skills` 的 `theme-factory`
   （`~/.agents/skills/theme-factory/themes/*.md`），`themes.py` 会**只读**适配其
   配色与字体，作为额外可选风格（我们不再分发其文件，仅本机读取）。

## 注意

- 中文排版已内置：docx 设置 `eastAsia` 中文字体；PDF 嵌入系统中文字体
  （PingFang/STHeiti/Songti/Noto，找不到时回退内置 CID 字体）。
- 主题只改「样式」，**不改内容**：用户提供的文字、数字、条款一律原样保留。
- `apply_docx_theme.py` 会重排**已有文档的样式与表格底纹**，不删改正文文字。
- Word 的目录域（TOC）需在 Word 中按 F9 更新；本技能默认不插入目录，除非
  用户明确要求章节导航。
- 涉及法律/财务/合规的表述，提醒用户由专业人士复核。
