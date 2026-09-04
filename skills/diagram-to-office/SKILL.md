---
name: diagram-to-office
description: 图表转 Office：把 PlantUML / Mermaid 源码渲染成 PNG/SVG 图片，再嵌入 Word/PPT 文档
triggers: plantuml,PlantUML,mermaid,Mermaid,uml,UML,图表转图片,图转word,图转ppt,图转office,时序图,流程图,类图
---

# 图表转 Office 技能（diagram-to-office）

用户文档中大量使用 PlantUML / Mermaid 图表；Office 文档（Word/PPT）无法直接
渲染这些 DSL，必须先把图表**渲染成图片**（PNG 或 SVG），再**嵌入** docx/pptx。
本技能定义完整流程。

## 一、识别图表源码

从用户输入或既有文档中找出图表 DSL 代码块：

- PlantUML：以 `@startuml` 开头、`@enduml` 结尾；常见类型：时序图、用例图、
  类图、活动图、组件图、状态图、部署图、ER 图、流程图（`|pseudo|` / `activity`）。
- Mermaid：以 ` ```mermaid ` 代码块包裹；常见类型：`flowchart`、`sequenceDiagram`、
  `classDiagram`、`stateDiagram-v2`、`erDiagram`、`gantt`、`pie`、`mindmap`。

源码可能来自：
1. 用户消息/文档中的代码块（提取文本即可）；
2. 仓库中的 `.puml` / `.plantuml` / `.mmd` 文件（直接读取文件内容）；
3. 已有 Markdown 文档中的 ` ```plantuml ` / ` ```mermaid ` 代码块。

## 二、渲染成图片

优先使用技能自带脚本：

```bash
python3 skills/diagram-to-office/render_diagram.py <input> -o <output.png> [--type plantuml|mermaid|auto]
```

- `<input>`：PlantUML/Mermaid 源码文本文件（含 `@startuml...@enduml` 或 ` ```mermaid ` 代码块），
  或已存在的 `.puml` / `.mmd` 文件；
- `-o`：输出图片路径，推荐放在工作区 `.outputs/` 下（如 `.outputs/架构图.png`）；
- `--type`：指定类型；缺省 `auto` 自动识别（按内容含 `@startuml` 或文件名后缀判断）；
- `--scale`：放大倍数（默认 2，用于高 DPI 清晰嵌入；SVG 输出时忽略）。

脚本会自动选择渲染引擎（按可用性依次尝试）：

| 引擎 | 适用 | 检查命令 |
|---|---|---|
| `plantuml` CLI | PlantUML | `which plantuml` |
| `java -jar plantuml.jar` | PlantUML | 本地存在 plantuml.jar |
| Docker `plantuml/plantuml` | PlantUML | `which docker` |
| `mmdc`（@mermaid-js/mermaid-cli） | Mermaid | `which mmdc` |
| `npx @mermaid-js/mermaid-cli` | Mermaid | `which npx` |
| Docker `minlag/mermaid-cli` | Mermaid | `which docker` |

**离线兜底（任何引擎都不可用时）**：直接返回诊断信息，提示用户安装，例如：

```bash
# PlantUML（需 Java，已检测到 /usr/bin/java）
brew install plantuml            # macOS
sudo apt install plantuml        # Debian/Ubuntu
# 或下载 jar：https://github.com/plantuml/plantuml/releases

# Mermaid（需 Node.js ≥ 18）
npm install -g @mermaid-js/mermaid-cli
# 若 puppeteer 下载 Chrome 失败，设置环境变量后重试：
# PUPPETEER_SKIP_DOWNLOAD=true 后需系统已装 Chrome，或用 npx puppeteer browsers install chrome
```

**特殊注意**：
- Mermaid 渲染依赖本机 Chrome（puppeteer）。若 `mmdc` 因缺浏览器失败，
  可尝试 `npx puppeteer browsers install chrome` 后重试；
- PlantUML 流程图若含 Unicode/中文，脚本已自动加 `!pragma layout smetana` 不必手动处理；
- 中文渲染需本机有中文字体（macOS 自带 PingFang，无需额外配置）。

## 三、嵌入 Office 文档

渲染得到的 PNG/SVG 图片路径，按目标格式嵌入：

### Word（docx_create）

`docx_create` 的 `content` 支持 Markdown 图片语法：

```markdown
# 系统架构说明

![系统架构图](.outputs/架构图.png)
```

- 图片路径：相对工作区的路径或绝对路径均可；脚本会自动调整图片宽度至页宽以内；
- 图片前后各留一个空行，避免与正文粘连。

### PPT（pptx_create）

`pptx_create` 的每页 slide 支持 `image` 字段（图片路径），图片会放置在
该页正文占位区域上方：

```json
{"slides": [
  {"title": "系统架构", "image": ".outputs/架构图.png", "bullets": ["采用微服务架构", "共 8 个服务"]},
  {"title": "部署拓扑", "image": ".outputs/拓扑图.png"}
], "filename": "方案.pptx", "title": "技术方案"}
```

### 备选：独立脚本嵌入

若需要在既有 docx/pptx 中追加图片，可直接用 python-docx / python-pptx：

```bash
python3 - <<'PY'
from docx import Document
d = Document("旧文档.docx")
d.add_picture(".outputs/架构图.png", width=__import__("docx.shared", fromlist=["Inches"]).Inches(5.5))
d.save("新文档.docx")
PY
```

## 四、交付与验证

1. 生成图片后，用 read_file / list_dir 确认图片文件存在且大小 > 0；
2. 嵌入后交付文件路径，并告知用户：
   - 图片对应的图表源码位置（便于后续修改重渲）；
   - 生成的图片路径（`.outputs/*.png`）；
   - 若要改图：修改源码后重新运行渲染脚本，再重新生成 Office 文档。

## 注意

- **不要**把 PlantUML/Mermaid 源码原文直接粘贴到 docx/pptx 当作正文
  （除非用户明确要保留代码），Office 文档无法渲染 DSL；
- 多图文档建议每张图单独命名（如 `架构图.png`、`时序图-登录.png`），避免覆盖；
- 图片嵌入失败的常见原因：路径不存在 / 相对路径基准不对 / 图片损坏，
  先确认渲染脚本输出的路径与嵌入时使用的路径一致。
