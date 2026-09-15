# Academic Paper Strategist（英文论文选题规划）

[lite-work](https://github.com/LaynePeng/lite-work) 社区技能：**英文哲学 / 交叉学科学术论文的系统化选题与大纲规划**。
输入一个研究想法，输出「可直接进入写作阶段的详细大纲 + 支撑材料」。

> 移植自 [lishix520/academic-paper-skills](https://github.com/lishix520/academic-paper-skills)
> 的 `strategist`，MIT License，Copyright (c) 2025 Li Shixiong（见 `LICENSE.txt`）。
> 配套技能：**academic-paper-composer**（按大纲写作）。

## 三个阶段（各带质量门槛）

| 阶段 | 产出 |
| --- | --- |
| 1. Platform Analysis | 目标平台（PhilArchive / arXiv / PhilSci-Archive …）+ 风格指南（分析 8–10 篇样本文） |
| 2. Theoretical Framework | 文献检索 + 研究缺口论证（每个缺口 3–5 条证据）+ 原创性评估 |
| 3. Outline Optimization | 以审稿人视角（7 维度 / 35 分）自评并优化的大纲 |

**门槛**：大纲自评 ≥ 28/35 才建议进入写作阶段。

## 用法（Agent 视角）

```
> Plan a paper on how mortality generates consciousness
```

技能会用 `ask_user` 与你确认平台、样本文、研究缺口等关键选择，并调用脚本产出报告。

## 脚本（非交互 CLI）

```bash
python3 "${SKILL_DIR}/scripts/evaluate_samples.py" --template samples.json --count 8   # 生成样本文评估模板
python3 "${SKILL_DIR}/scripts/evaluate_samples.py" --json samples.json --out 产出物/sample_report.md
python3 "${SKILL_DIR}/scripts/gap_analysis.py" --template gaps.json --count 3          # 生成缺口模板
python3 "${SKILL_DIR}/scripts/gap_analysis.py" --validate gaps.json                    # 校验单个缺口
python3 "${SKILL_DIR}/scripts/gap_analysis.py" --json gaps.json --out 产出物/gap_report.md
python3 "${SKILL_DIR}/scripts/gap_analysis.py" --gap gaps.json --out 产出物/evidence.md  # 证据包
```

不带参数时脚本会尝试进入**交互式菜单**（需 TTY）；在 Agent / CI 等无 TTY 环境下会打印用法并以 `2` 退出。

## 目录结构

```text
academic-paper-strategist/
├── SKILL.md                    # Agent 加载的指令（工作流与质量门槛）
├── references/
│   ├── quality_standards.md    # 7 维度评审标准
│   └── search_strategy.md      # 文献检索策略
├── scripts/
│   ├── evaluate_samples.py     # 样本文评估
│   └── gap_analysis.py         # 研究缺口校验与分析
├── examples/                   # 样本文示例（哲学 / AI 伦理）
└── LICENSE.txt
```

## 本仓库相对上游的适配

1. **脚本改为非交互 CLI**（argparse）：上游只有 `input()` 菜单，在无 TTY 的 Agent 环境会挂起或 `EOFError`；
   分析函数逻辑未改动，`--interactive` 仍可打开原菜单。
2. **修复 `gap_analysis.py` 语法**：上游使用了嵌套同种引号的 f-string（仅 Python 3.12+ 合法），
   在 3.9/3.10/3.11 下直接 `SyntaxError`；现可在 Python 3.9+ 运行。
3. 路径改为 `${SKILL_DIR}` 相对路径，`python` → `python3`。
4. frontmatter 补 `triggers` 与顶层 `version`，并把含 ASCII 冒号的 description 加引号（严格 YAML 可解析）。
5. 报告输出由 `--out` 指定（建议写入工作区 `产出物/`），不再固定写在输入 JSON 旁边。

## 依赖

仅 Python 标准库（`json` / `argparse` / `pathlib`），无需额外安装。
