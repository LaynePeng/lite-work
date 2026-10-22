# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""内容展示偏好的按需参考手册（原 system_prompt 的 7.5k 字符大段精简而来）。

System Prompt 只保留「选型一句话」级别的速查；围栏的完整 JSON 语法细节在
Agent 第一次使用对应围栏时由此文件按需提供（tools/skills 式的惰性加载思路，
对齐 Muse 的多级工具：L0 常驻一句话，L1 按需完整手册）。
"""

MANUAL = """### 富围栏参考手册（按需使用）

你输出富围栏（chart/cards/kpi/timeline/table/compare/board/progress/
interactive/diagram/mermaid/layout）时，遵循本手册的语法。

**chart（原生图表）**：围栏内写紧凑 JSON。
- 基础：`{"type": "bar|line|pie|barh", "title": "…", "unit": "…",
  "labels": […], "series": [{"name": "…", "data": […]}]}`；单系列可省 series
  直接写 `"data": [...]`；
- 专业统计图：`scatter`（`"points": [[x,y],…]`，自动叠加趋势线与 R²，关闭传
  `"trend": false`）；`boxplot`（`"boxStats": [{"label","min","q1","median",
  "q3","max","outliers"}]` 五数概括需先算好）；`histogram`（给原始数值
  `"series":[{"data":[…]}]`，前端自动分箱，不要自己算频数）；`heatmap`
  （`"heatmap":{"values":[[…]],"xLabels":[…],"yLabels":[…],"mode":"diverging"
  |"sequential"}`，相关矩阵用 diverging）；`combo` 双轴（折线系列标
  `"axis":"right"`）；`waterfall`（`"items":[{"label","value","total":true?}]`，
  total:true 为小计柱）；`stacked` / `stacked-area`（多系列自下而上堆叠）；
- 图表/数据表右上角悬停有 ⇩ 导出菜单（PNG / CSV / 独立 HTML / 导出到项目
  `中间产物/exports/`）。用户要「把图放进 Word/PPT」时，先请用户在图上点
  「导出到项目」，拿到 PNG 路径后用 docx_create / pptx_create 的
  `![图注](路径)` 嵌入——不要用 chart_make 重画，也不要手写 base64。

**cards（实体卡片）**：`{"title": "…", "items": [{"title", "image"?, "price"?,
"desc", "tags": […]}]}`；image 用 workspace 相对路径或 https URL。

**kpi（指标卡）**：`{"title": "…", "items": [{"label", "value", "delta",
"trend": "up|down|flat", "note"?}]}`；up=绿/down=红/flat=中性。

**timeline（时间线）**：`{"items": [{"time", "title", "desc"?, "status":
"done|active|pending"}]}`。

**table（数据表）**：`{"title", "columns": […], "rows": [[…], …]}`；
适合窄栏多列数值对比。

**compare（左右对比）**：`{"left": {"title", "items": […], "tone":
"good|bad|neutral"}, "right": {…}}`。

**board（看板）**：`{"columns": [{"name", "cards": [{"title", "tag"?}]}]}`。

**progress（进度条）**：`{"title", "items": [{"label", "value": 0-100,
"note"?}]}`；自动色阶 ≥80 绿 / 40-79 黄 / <40 红。

**interactive（交互图解）**：`{"title", "summary", "numbered"?, "parts":
[{"name", "desc", "meta"?}]}`；3-6 块为宜。desc 写清「它是什么 + 起什么作用」。
纯前端本地交互，无法接收用户输入。part 可选带 shapes（diagram 同款原语），
选中该块时显示聚焦示意图。

**diagram（图形图解）**：`{"title", "canvas": {"w", "h"}, "parts": [{"name",
"desc", "shapes": […]}]}`。原语：`circle(x,y,r)` / `rect(x,y,w,h,rx?)` /
`line(x1,y1,x2,y2)` / `ellipse(x,y,rx,ry)` / `polyline(pts)` /
`path(d，仅 M L Q A Z)` / `label(x,y,text,size?,anchor?)`（单条 ≤24 字）。
排版要求：主体居中占画布 70-85%；相邻部件共享坐标；线宽统一 3-4；每个部件
都要有 label；面图形自动带低饱和填充；每部件 1-8 个原语，画完自查不重叠。

**mermaid（流程/时序/状态/架构首选）**：输出 Mermaid DSL 文本，前端自动布局。
流程 graph TD/LR、时序 sequenceDiagram、状态 stateDiagram-v2、甘特 gantt、
类图。语法错降级为代码块，写完自查分号/箭头。

**layout（语义拓扑图）**：`{"title", "dir": "TB|LR", "nodes": [{"id",
"label", "group"?, "level"?}], "edges": [{"from", "to", "label"?}]}`；
坐标由前端布局算法计算。适用关系拓扑/系统组成/依赖关系。

**三图选择**：流程/时序/状态 → mermaid；关系拓扑 → layout；有真实空间
比例的结构 → diagram；拿不准优先 mermaid。

**通用规则**：富围栏流式渐进渲染，可放在回答开头；html 围栏仅在确有独特
版式需求（海报/页面原型）时手写；引用本地图片一律写 workspace 相对路径
（不要绝对路径、不要 /api/ URL）。
"""
