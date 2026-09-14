# 主题文件格式说明

主题是「配色 + 字体 + 版式度量」的轻量声明，供 `scripts/*.py` 读取。
格式刻意保持扁平（`key: value`），无需 YAML 依赖，便于用户手改。

## 字段

| 字段 | 说明 | 示例 |
| --- | --- | --- |
| `name` | 主题 id（文件名去掉 .md，须一致） | `business-blue` |
| `label` | 显示名（给用户看） | `商务蓝` |
| `description` | 一句话适用场景 | `稳重专业，适合方案/汇报` |
| `color_primary` | 主色（标题/表头） | `#1F4E79` |
| `color_accent` | 强调色 | `#2E75B6` |
| `color_text` | 正文文字色 | `#1A1A1A` |
| `color_muted` | 次要文字色（页脚/日期） | `#6B7280` |
| `color_table_head` | 表头底色 | `#1F4E79` |
| `color_table_band` | 表格隔行底色 | `#EEF3F9` |
| `color_code_bg` | 代码块底色 | `#F5F5F5` |
| `font_zh` | 正文中文字体 | `宋体` |
| `font_en` | 正文西文字体 | `Times New Roman` |
| `font_heading_zh` | 标题中文字体 | `微软雅黑` |
| `font_heading_en` | 标题西文字体 | `Calibri` |
| `font_mono` | 等宽字体（代码） | `Courier New` |
| `size_body` | 正文字号 pt | `12` |
| `size_h1` / `size_h2` / `size_h3` | 各级标题字号 pt | `22` / `16` / `14` |
| `size_small` | 小号字（页脚） | `10.5` |
| `line_spacing` | 行距倍数 | `1.5` |
| `para_space` | 段后间距 pt | `6` |
| `margin_mm` | 页边距 mm（四边） | `25.4` |

## 最小示例

```markdown
name: my-theme
label: 我的主题
description: 自定义风格
color_primary: "#225533"
color_table_head: "#225533"
color_table_band: "#EAF3EE"
font_zh: 宋体
font_en: Times New Roman
font_heading_zh: 微软雅黑
size_body: 12
size_h1: 22
size_h2: 16
size_h3: 14
line_spacing: 1.5
para_space: 6
margin_mm: 25.4
```

> 缺失字段会用内置默认值补齐，因此最小主题只需写想改的项。
> 放到 `~/.lite-work/themes/` 下即被自动发现。
