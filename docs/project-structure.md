# 项目结构约定（素材 / 产出物 / 版本归档）

lite-work 项目的推荐组织方式：**明确的 AGENTS.md + 素材/产出物两大目录 +
按品类版本演进 + 归档**。配套工具是内置技能 `project-init`
（`skills/project-init/`），本文是它的约定说明。

## 目录总览

```
<项目>/
├── AGENTS.md                 # 项目最佳实践（Agent 每次任务自动读取并注入系统提示）
├── README.md                 # 人类入口（可选）
├── 素材/                      # 输入：Agent 只读
│   ├── 原始/                  # 上传原件（上传文件默认落点）
│   └── 参考/                  # 参考资料、外部引用
├── 产出物/                    # 产出：Agent 只写这里
│   ├── INDEX.md               # 总览（只列品类）
│   └── <品类>/                # 默认：报告 / 演示 / 表格数据 / 图表 / 专利 / 论文（可扩展）
│       ├── INDEX.md           # 品类清单：文件 / 版本 / 日期 / 状态 / 说明
│       ├── <主题>_vN.<ext>     # 当前交付物（文件名带版本号）
│       ├── 中间产物/           # 草稿、检索结果、渲染中间件（非交付）
│       └── 归档/               # 旧版本：YYYY-MM-DD_<原名>
└── .lite-work/                # 运行时数据（会话/计划），隐藏，不参与交付
```

## 规则

1. **命名**：日期一律 `YYYY-MM-DD`；交付物 `<主题>_vN.<ext>`；归档
   `归档/YYYY-MM-DD_<原名>`。
2. **版本演进（硬性）**：任何修订（用户不满意、补充信息、纠错）都是
   「读取当前版本 → 合并 → 写 `_v(N+1)` → 旧版进归档 → 更新 INDEX」。
   **禁止用新增文件做补充**（`X_补充.md`、`*_addendum*` 等）——一个交付物
   任何时刻只有一个当前版本。
3. **权限边界**：`素材/` 只读；交付物只写 `产出物/<品类>/`；`归档/` 只增不删。
4. **品类可扩展**：默认品类之外可按项目新增（先征得用户同意），新增后建对应
   `INDEX.md`。

## 工具（内置技能 project-init）

| 命令（相对技能目录） | 作用 |
| --- | --- |
| `python scripts/scaffold.py <项目根> [--categories a,b]` | 初始化结构（幂等，不覆盖已有文件；`AGENTS.md` 缺失时从模板生成） |
| `python scripts/new_artifact.py --category 报告 --name 主题 --ext docx [--project-root .]` | 分配下一版本路径并登记 INDEX（版本号只增不减） |
| `python scripts/archive_artifact.py --path 产出物/报告/x_v1.docx` | 旧版移入 `归档/YYYY-MM-DD_<原名>` 并更新 INDEX |
| `python scripts/lint_artifacts.py [--project-root .]` | 检查补充文件违例（非零退出） |

## 新建项目自动初始化

桌面端「新建项目」入口在创建目录时即自动完成结构初始化（`素材/`、
`产出物/`、品类目录、`INDEX.md`、`AGENTS.md`——由后端
`litework/tools/project_scaffold.py` 执行，打包态可用）；「新建代码」入口
**不**初始化结构、不生成 `AGENTS.md`，保持代码仓库干净。存量项目用上表的
`scaffold.py` 补建即可（幂等）。

## 项目类型（代码 / 项目）

项目类型与 git 解耦，按目录内容启发式判定（`classify_project_kind`）：
根目录有代码标记文件（`pyproject.toml` / `package.json` 等）→ 代码；
有 `素材/` 或 `产出物/` → 项目；文档文件（docx/pdf/pptx/xlsx）多于代码文件
→ 项目；仅 `.git` 的空仓库 → 代码兜底。最近项目列表可手动标记
（行内 ⇄ 按钮），标记后锁定、不再被启发式覆盖。

## 与现有机制的衔接

- `AGENTS.md` 会被 `SystemPromptBuilder` 自动注入系统提示（项目指令），
  无需额外配置。
- office 工具的产出目录就是 `产出物/`（`OUTPUT_DIR_NAME`），上传素材落
  `素材/`（`UPLOADS_DIR_NAME`），与本约定天然一致。
- office 的 `chart_make` 内置子目录 `产出物/diagrams/` 视为图表工作目录；
  交付图表建议显式写入 `产出物/图表/`。

## 设计参考

- AGENTS.md 开放标准（agents.md）：「agent 的 README」，保持精简、指针式。
- artifact 组织综述（zylos.ai）：项目 → 类型 → 日期的混合层级，同类扁平、
  不超过 3 层，每类一个 INDEX（Map of Content），旧内容走归档。
