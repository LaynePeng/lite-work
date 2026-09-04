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

## 二、UML 选型：优先 PlantUML（而非 Mermaid）

**生成 UML 类图表（时序图、类图、组件图、部署图、用例图、状态图等）时，
一律用 PlantUML，不要用 Mermaid。** 原因：

1. **渲染可靠性**：PlantUML 有纯 JS 离线引擎（`@plantuml/core`），不依赖
   Chrome/puppeteer；Mermaid 的 mmdc 依赖本机 Chrome，环境缺浏览器时易失败；
2. **语法表达力**：PlantUML 对 UML 语义（参与者激活、note、分包、嵌套）
   支持更完整，Office 文档场景下出图更稳定；
3. **离线一致**：PlantUML 链路（`@plantuml/core` + `@resvg/resvg-js`）
   全程纯 JS 离线，与本项目"离线优先"策略一致。

Mermaid 仅在用户**明确指定**或源码已是 mermaid 语法时使用（如
`flowchart`、`gantt`、`mindmap` 等 Mermaid 特有类型）。

## 三、PlantUML 安全写法（避免规则，生成时必须遵守）

实测发现某些写法有一定概率触发解析/渲染错误，生成 PlantUML 源码时
**必须遵守以下规则**：

1. **只用标准元素类型**
   - 组件图/部署图用：`rectangle`、`component`、`actor`、`database`、
     `node`、`package`、`usecase`；
   - ❌ 不要用 `system`（ArchiMate 专用）、`folder`/`file` 慎用。

2. **`()` 语法的使用边界**
   - `()` 只能用于声明 `interface` / `usecase`；
   - `package` / `rectangle` 块内一律用 `component "name"`，**禁止把
     `()` 嵌在块内**；
   - 块内表达元素用 `rectangle "name" as alias` 即可，不需要 `()`。

3. **箭头必须成对有目标**
   - 正确：`A --> B : label`；
   - ❌ `A --> : label`（目标缺失 → 解析器在后续行报错，错误位置不直观）。

4. **note 用最稳妥形式**
   - 单行：`note "text"`；
   - 多行附元素：在块**外**写 `note right of Alias ... end note`；
   - ❌ 不要在 `{}` 块内嵌套 `note right`（与布局引擎冲突）；
   - 最保守：不依赖 note，用 `title` / `legend` / 文档正文表格承载描述。

5. **慎用自定义 layout 指令**
   - `!pragma layout smetana` 等与 note/box 混用易崩 → 默认布局足够，
     非必要不写。

6. **单图控制信息量**
   - 元素 > 15 个或多层次并发 → 拆图；
   - 表达层次结构用 `rectangle` 嵌套（配合 `componentStyle`），别用
     `box` + 消息混排画架构图；
   - **序列图（时序）与结构图（层次）是两种图，分开画**。

7. **写完必校验（由渲染脚本自动执行）**
   - 每张图检查 `@startuml`/`@enduml` 配对、`{}`/`()`/`[]` 括号平衡
     （渲染前脚本会预检，配对错误直接报错、括号不平衡告警）；
   - 渲染确认以本地出图为准（脚本成功生成非空图片 = 校验通过），
     不要只靠文本自测。

## 四、渲染成图片

优先使用技能自带脚本（**默认离线优先，绝不联网拉取**）：

```bash
python3 skills/diagram-to-office/render_diagram.py <input> -o <output.png> [--type plantuml|mermaid|auto]
python3 skills/diagram-to-office/render_diagram.py --check   # 环境诊断（离线可用性）
```

- `<input>`：PlantUML/Mermaid 源码文本文件（含 `@startuml...@enduml` 或 ` ```mermaid ` 代码块），
  或已存在的 `.puml` / `.mmd` 文件；
- `-o`：输出图片路径，推荐放在工作区 `.outputs/` 下（如 `.outputs/架构图.png`）；
- `--type`：指定类型；缺省 `auto` 自动识别（按内容含 `@startuml` 或文件名后缀判断）；
- `--scale`：放大倍数（默认 2，用于高 DPI 清晰嵌入；SVG 输出时忽略）；
- `--check`：环境诊断，列出本地可用引擎与离线能力；
- `--allow-network`：显式允许联网（仅在线环境需要；默认关闭）；
- `--no-fallback`：禁用内置兜底渲染（无引擎时直接报错）。

### 离线保证（重要）

| 场景 | 行为 |
|---|---|
| 有本地引擎 | 用 `@plantuml/core`（纯 JS）/ `plantuml` CLI / `java -jar plantuml.jar` / `mmdc` 渲染，全离线 |
| 无本地引擎 | **自动改用内置 matplotlib 兜底渲染**（零外部依赖，离线必出图）：flowchart / 时序图渲染简化版，其余类型输出源码文本图 |
| docker / npx | 仅当本地已缓存镜像/包时使用（自动检查），**默认不联网拉取** |
| 在线环境 | 加 `--allow-network` 才允许 npx 安装包 / docker 拉镜像 |

引擎选择顺序（离线优先，按可用性依次尝试）：

| 引擎 | 适用 | 离线 | 检查命令 |
|---|---|---|---|
| `@plantuml/core`（纯 JS，推荐） | PlantUML | ✅（无需 Java） | `npm root -g` 下存在 |
| `plantuml` CLI | PlantUML | ✅ | `which plantuml` |
| `java -jar plantuml.jar` | PlantUML | ✅ | 本地存在 plantuml.jar |
| Docker `plantuml/plantuml` | PlantUML | 仅镜像已缓存 | `docker image inspect` |
| `mmdc`（@mermaid-js/mermaid-cli） | Mermaid | ✅（全局安装） | `which mmdc` |
| `npx @mermaid-js/mermaid-cli` | Mermaid | 仅包已缓存 | `npx --no-install` |
| Docker `minlag/mermaid-cli` | Mermaid | 仅镜像已缓存 | `docker image inspect` |
| 内置 matplotlib 兜底 | 两者 | ✅（项目已依赖） | `--check` 查看 |

> PlantUML 渲染采用 `@plantuml/core`（TeaVM 编译的纯 JS 引擎，方案来自
> [markdown-viewer](https://github.com/LaynePeng/markdown-viewer)）：先渲染为
> SVG，再用 `@resvg/resvg-js`（Node 跨平台）转 PNG，**全程无需 Java、无需系统库**。

**离线部署建议**（一次性准备，之后完全离线）：
1. **一键预装**（推荐，需要联网一次）：
   ```bash
   python3 skills/diagram-to-office/render_diagram.py --install
   ```
   脚本会自动执行：
   - `npm install -g @plantuml/core`（PlantUML 纯 JS 引擎，无需 Java）
   - `npm install -g @resvg/resvg-js`（SVG→PNG，跨平台）
   - `npm install -g @mermaid-js/mermaid-cli`（mmdc，Mermaid）
2. 手动方式：
   - PlantUML：`npm install -g @plantuml/core`（推荐，无需 Java）或
     `brew install plantuml`，或下载 `plantuml.jar` 放到 `~/plantuml.jar`；
   - SVG→PNG：`npm install -g @resvg/resvg-js`（推荐）或
     `pip install cairosvg` / `brew install librsvg` / ImageMagick；
   - Mermaid：`npm install -g @mermaid-js/mermaid-cli`，并确保本机有 Chrome
     （`npx puppeteer browsers install chrome`）；
3. 准备完成后运行 `render_diagram.py --check` 确认各项为 ✓；
4. 之后完全离线使用：`render_diagram.py` 直接走本地引擎渲染，不联网。

**特殊注意**：
- Mermaid 渲染依赖本机 Chrome（puppeteer）。若 `mmdc` 因缺浏览器失败，
  可尝试 `npx puppeteer browsers install chrome` 后重试；
- 内置兜底渲染仅覆盖 flowchart / 时序图简化版，其余类型为源码文本图；
  安装引擎后效果更佳，但**离线时也保证有图片可嵌入**；
- 中文渲染需本机有中文字体（macOS 自带 PingFang，无需额外配置）。

## 五、嵌入 Office 文档

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

## 六、交付与验证

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
