# Academic Paper Composer（英文论文写作）

[lite-work](https://github.com/LaynePeng/lite-work) 社区技能：**按详细大纲逐章撰写英文学术论文**，
并在章节级与全文级做迭代质量控制，产出可投稿稿件。

> 移植自 [lishix520/academic-paper-skills](https://github.com/lishix520/academic-paper-skills)
> 的 `composer`，MIT License，Copyright (c) 2025 Li Shixiong（见 `LICENSE.txt`）。
> 配套技能：**academic-paper-strategist**（先生成优化大纲）。

## 两个阶段

| 阶段 | 内容 | 产出 |
| --- | --- | --- |
| 4. Systematic Writing | 逐章写作 + 每章质量门槛 | 各章草稿 + 章节评估报告 |
| 5. Quality Control | 全文 7 维度评估 + 投稿前清单 | 可投稿稿件 + 评估报告 |

**输入**：详细大纲（来自 strategist，或用户自备等价大纲）。

## 用法（Agent 视角）

```
> Write the paper from this outline: [大纲]
```

## 脚本（非交互 CLI）

```bash
# 章节质量检查（评分维度 1–4 分）
python3 "${SKILL_DIR}/scripts/chapter_quality_check.py" --template ch2.json \
    --chapter-id chapter_2 --chapter-title "Method" --word-count 1500
python3 "${SKILL_DIR}/scripts/chapter_quality_check.py" --json ch2.json --out 产出物/ch2_report.md
python3 "${SKILL_DIR}/scripts/chapter_quality_check.py" --compare chapters.json --out 产出物/compare.md

# 全文最终评估（评分维度 1–10 分 + completeness_checklist）
python3 "${SKILL_DIR}/scripts/final_evaluation.py" --template final.json \
    --title "Paper Title" --word-count 8000 --platform PhilArchive
python3 "${SKILL_DIR}/scripts/final_evaluation.py" --json final.json --out 产出物/final_report.md
```

不带参数时脚本会尝试进入**交互式菜单**（需 TTY）；在 Agent / CI 等无 TTY 环境下会打印用法并以 `2` 退出。

> JSON 里只填「人/模型给出的判断与评分」，脚本负责校验与生成报告——
> 校验不通过时退出码为 `1`，并打印具体 issue，便于回归修正。

## 目录结构

```text
academic-paper-composer/
├── SKILL.md                    # Agent 加载的指令（写作流程与质量门槛）
├── references/
│   ├── section_guides.md       # 各章节写作指南
│   └── writing_standards.md    # 学术写作规范
├── scripts/
│   ├── chapter_quality_check.py  # 章节质量检查
│   └── final_evaluation.py       # 全文最终评估
└── LICENSE.txt
```

## 本仓库相对上游的适配

1. **脚本改为非交互 CLI**（argparse）：上游只有 `input()` 菜单，在无 TTY 的 Agent 环境会挂起或 `EOFError`；
   校验 / 评分 / 报告函数逻辑未改动，`--interactive` 仍可打开原菜单。
2. 路径改为 `${SKILL_DIR}` 相对路径，`python` → `python3`。
3. frontmatter 补 `triggers` 与顶层 `version`，并把含 ASCII 冒号的 description 加引号（严格 YAML 可解析）。
4. 报告输出由 `--out` 指定（建议写入工作区 `产出物/`），不再固定写在输入 JSON 旁边。

## 依赖

仅 Python 标准库（`json` / `argparse` / `pathlib`），无需额外安装。
