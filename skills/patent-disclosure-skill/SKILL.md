---
name: patent-disclosure-skill
description: "中国专利技能：挖掘专利点与编写交底书（发明/实用/外观），把已有交底改写成申请文件四件套，也可按材料交底申请一起做，按著录字段检索公布公告，通俗解读专利，基于已读库打开专利地图，对照审查口径出政策简报，辅助审查答复。| China patents skill: mine patent points and draft disclosures, rewrite an existing disclosure into application documents, or chain disclosure-then-application from inventor materials in one pass (ask when facts are missing; at most three issue-list rounds), search CNIPA bibliographic records, explain patents, open a local patent map from interpreted notes, brief examination-policy changes, and assist office-action responses."
version: "4.8.0"
triggers: 专利,交底书,专利交底,申请文件,权利要求,说明书,专利检索,著录检索,专利解读,专利地图,审查答复,审查意见,政策简报,实用新型,外观设计,专利申请,专利查新,知识产权,CNIPA,patent,patent disclosure,patent search,patent map,office action
user-invocable: true
argument-hint: "[可选：项目路径 / 交底书 / 申请底稿 / 交底申请一起做 / 专利检索 / 专利号或 PDF / 专利地图 / 政策简报 / 审查答复]"
allowed-tools: Read, Write, Edit, Grep, Glob, WebSearch, Bash
---

# 中国专利技能

> **技能目录**：本文件所在目录。加载技能时 host 会注入它的绝对路径；本文档中的
> 命令均**相对本技能根**（与上游约定一致）。**不要依赖厂商环境变量**。
> 工作区之外的文件不能用 read_file 读写，需要看随技能附带的资料时用 `cat`。
>
> **产出位置**：一律写入**工作区** `outputs/`（解读/检索/政策/审查答复/申请文件/案卷
> 分子目录），不要写到技能安装目录或 `tmp/`。
>
> **前置条件与降级**（缺失不中断交付，但会少能力）：
>
> | 能力 | 依赖 | 缺失时 |
> | --- | --- | --- |
> | 核心：交底书/申请文件/Word(.docx)/PPT(.pptx) | `python-docx`、`PyYAML`、`python-pptx`（主程序已内置）+ `latex2mathml`、`mammoth`（按 `requirements.txt` 自动安装） | — |
> | 公式 → Word 可编辑公式 | `latex2mathml` | 降级保留 LaTeX 源文本 |
> | mermaid/附图渲染、CNIPA 公布公告检索 | `playwright` + **系统 Chrome/Edge**（无则 `python -m playwright install chromium`） | 降级：保留 mermaid 源码 / 改用 WebSearch |
> | 公式 PNG 兜底 | `matplotlib`（主程序已内置，可选） | 降级保留源文本 |
> | 通俗解读 | **Obsidian** 库（外部应用） | 该能力受限，其余正常 |
> | 专利地图 | **Obsidian** + `fastembed`（列在 `skills/patent-map/tools/requirements.txt`，**未内置，需手动装**） | 语义地图生成受限 |
> | 审查答复 | **Obsidian** + `sentence-transformers`、`sqlite-vec`（列在 `skills/patent-oa/tools/requirements-oa.txt`，**未内置**） | RAG 检索受限 |
> | STEP 三维视图 | `cadquery`、`cairosvg`（`skills/patent-disclosure/tools/requirements-step.txt`，装在独立 venv，Python 3.10–3.12） | 跳过该功能 |
>
> 说明：lite-work 只会自动安装**顶层** `requirements.txt`；上面标「未内置」的是**可选功能的子清单**，
> 用到对应能力时需按清单手动 `pip install`。`skills/patent-disclosure/tools/package.json`
> 是**遗留的可选 Node 依赖（不需要）** —— mermaid 出图已改用 Playwright + `tools/vendor/mermaid.min.js`。
>
> 建议动手前先自检：`python3 skills/patent-disclosure/tools/browser.py --probe`（浏览器）、
> `python3 skills/patent-reader/tools/vault/check_obsidian_env.py`（Obsidian）。

按用户意图 **`Read`** 对应入口的 `SKILL.md`，再按该流程执行。

| 能力 | 做什么 | 何时进入 | 入口 |
|------|--------|----------|------|
| **交底** | 挖专利点 → 查新 → 成稿 → 迭代 | 专利挖掘、交底书、查新、实用新型、外观设计；`/patent-disclosure`、`/交底书` | `skills/patent-disclosure/SKILL.md` |
| **申请文件** | 已有交底 → 权要 / 说明书 / 摘要 / 附图 | **须显式**且**指定交底目录**：申请文件、申请底稿、申报材料、`/申请底稿`、`/patent-apply`。仅缺材料则终止；内容争议写入问题清单；交付末尾须摘要清单；改已有产出则另存 | `skills/patent-application/SKILL.md` |
| **案卷** | 按发明人/工程师给的材料一趟写出交底书和申请文件；清单缺口最多来回三轮，缺事实问人、不编 | **须显式**：交底申请一起做、从零出交底和申请、一条龙、帮写交底再出申请、按清单改、会稿、案卷、`/patent-docket`。只写交底或已有交底只出四件套 → 不要进本案。状态 `outputs/docket/` | `skills/patent-docket/SKILL.md` |
| **检索** | 公布站高级查询（发明人/申请人/分类号/名称/摘要等）；单图或权要可先抽关键字再查 | 按著录字段查公布公告、个人公开清单、以图/权要生成检索式；`/patent-search`。普通多条件**不要**默认翻完全部分页 | `skills/patent-search/SKILL.md` |
| **解读** | 公开号 / PDF / 全文 → 通俗笔记 + 图谱 | 读专利、公开号或 PDF 且目标为理解；`/patent-read`、`/读专利` | `skills/patent-reader/SKILL.md` |
| **专利地图** | 已解读入库的案例摊成五种图（语义地形） | **须显式**：专利地图、案例地图、`/专利地图`、`/patent-map`。不因读专利自动进入 | `skills/patent-map/SKILL.md` |
| **审查答复** | 审查意见问答与草稿；库薄时引导入库/蒸馏 | **须显式**：审查意见、OA、案例入库、实务书、`/oa` | `skills/patent-oa/SKILL.md` |
| **政策简报** | 对照国知局口径，说明对交底写法/本稿的影响；改技能仅为旁路 | **须显式**：政策简报、政策雷达、`/政策简报`、`/patent-brief`、`/patent-exam-policy`。「技能进化 / `/patent-evolve`」同一入口，仍先出简报 | `skills/patent-exam-policy/SKILL.md` |

## 路由

- 填表、线稿、CAD、公式、Word 出图在交底包 `prompts/` 与 `tools/`；解读填表用解读包 `prompts/fill_*`；过 WAF 的 `browser.py`、Markdown 转 Word 的 `md_to_docx.py` **各包自带副本**。
- **禁止跨包调用**其他子技能的 `tools/`。需要同一能力就用本包副本。
- 专利号或 PDF 且意图为「读懂」→ **优先解读**，不跑交底 Step 1–8。
- **禁止**因写交底或读专利自动进入政策简报、审查答复、申请文件、案卷或专利地图。申请文件必须用户点名并给出交底目录；缺 schema / 线稿 / 交底书则停，引导先补交底。内容争议不阻塞主文件。
- **案卷**必须用户点名（交底申请一起做 / 从零出交底和申请 / 一条龙 / 帮写交底再出申请 / 按清单改 / 会稿 / `/patent-docket`）。点名后 `Read` `skills/patent-docket/SKILL.md`；由案卷再 `Read` 交底或申请入口。调度申请视为已点名申请文件，仍须有交底目录。只写交底、或已有交底只出四件套 → 不要进案卷。
- 交底 Step 5 查新只用交底包轻量检索（一词一页）；按发明人/申请人等做著录检索时只用检索包，两者不要混用。

## 目录

```
SKILL.md                         # 本文件：子技能路由入口
skills/patent-disclosure/        # 交底（含填表/线稿/CAD/公式/docx）
skills/patent-application/       # 申请文件四件套（须显式；须指定交底目录）
skills/patent-docket/            # 案卷：交底到申请一趟串起来（须显式）
skills/patent-search/            # 著录检索
skills/patent-reader/            # 解读
skills/patent-map/               # 专利地图（须显式；读 vault，本机页面）
skills/patent-oa/                # 审查答复
skills/patent-exam-policy/        # 政策简报（技能进化为旁路）
```

## 环境与约定

- **默认语言**：面向用户的检索清单、交底书、申请文件、案卷 TRACKER、解读和审查答复用简体中文；脚本机读前缀与 JSON 字段名保持稳定。
- **技术语言（硬性）**：交底书、申请文件等一切交付文档，技术特征**尽量使用本领域通用技术术语**（「连接」「固定」「夹持」「检测」「处理单元」「通信接口」这类常规词），**禁止随意生造词汇**（把「夹持」写成「环抱式定位握持」、把「连接器」写成「插拔耦合组件」即属造词）。确需表述新概念时，用通用词组合表达，并在首次出现处给出定义，而不是发明一个没见过的名词。与子技能里的「话题错位检测」是两条并列要求：换领域词不等于造新词，两者都要守。
- **脚本判读（尤其 Windows）**：stderr 有字 **不等于** 失败。以 **退出码 0** 和机读前缀为准：`EPUB_HITS_JSON:` / `EPUB_SEARCH_MD:` / `EPUB_SEARCH_JSON:` / `EPUB_CLASS_HINT:`、`PROBE:` / `BROWSER:`、`MERMAID:` / `DOCX:`、`APPLICATION_GATE:` / `APPLICATION_CLAIMS:` / `APPLICATION_NUMERALS:`、`DOCKET_DIR:` / `DOCKET_YAML:` / `DOCKET_OK:` / `DOCKET_ERROR:`、`MAP_URL:` / `MAP_PORT:` / `MAP_VAULT:` / `MAP_SOURCE:` / `MAP_CACHE:`。PowerShell 可能把 stderr 标成 `NativeCommandError`；**禁止**因此重跑安装或把查新降级 WebSearch。
- **专利类型**：未显式指定时交底**默认发明**。
- **脚本路径**：相对本技能仓库根（本文件所在目录）。整仓：`python skills/patent-disclosure/tools/…`。当前工作区不是本仓库时，把技能安装目录接到命令前面。单独拷走某一子包时，该包内用 `python tools/…`。不要写厂商环境变量。
- **用户产出**：写在当前工作区 `outputs/`（解读 `outputs/patent_reader/`，检索 `outputs/patent-search/`，政策 `outputs/exam-policy/`，审查答复 `outputs/oa/`，申请文件 `outputs/patent-application/`，案卷 `outputs/docket/`），不要写到技能安装目录或 `tmp/`。调用脚本时 cwd 用工作区根；`-o` / `-w` 用上述相对路径。

## 执行前核对

```
□ 已 Read 对应 skills/*/SKILL.md，未把本文件当交底/申请底稿/案卷/检索/解读/专利地图正文
□ 交底查新未调用 patent-search
□ 著录检索未用交底一词一页结果冒充清单
□ 政策简报 / 审查答复 / 申请文件 / 案卷 / 专利地图仅在显式触发时进入
□ 申请文件已指定交底目录；门禁未过未开写
□ 案卷未写交底/申请正文；派工只 Read 对方 SKILL.md
□ 未跨包调用其他子技能的 tools/
□ 未把政策简报当成改技能；无点名未改交底包以外的目录
```
