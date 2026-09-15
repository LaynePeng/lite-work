---
name: project-init
description: 项目组织与产物管理：初始化 AGENTS.md + 素材/产出物 结构；产出物按品类存放、版本演进归档（禁止补充文件）
triggers: 初始化项目,项目结构,新建项目,项目组织,归档,版本管理,产出物整理,整理产出物
version: "1.0.0"
---

# 项目组织技能（project-init）

维护本项目的标准结构：`AGENTS.md` + `素材/`（只读输入）+ `产出物/`（按品类、
版本演进、带归档）。完整约定见项目根 `AGENTS.md` 与主仓库
`docs/project-structure.md`。

> 本技能目录下的脚本以**技能目录**为基准调用（下文命令均相对技能目录）。

## 何时使用

- 用户要求「初始化项目 / 建项目结构 / 整理产出物」→ 运行 scaffold；
- 任何交付物产出或修订 → 用 new_artifact / archive_artifact 维护版本与 INDEX；
- 怀疑出现补充文件 → 运行 lint_artifacts 自检。

## 硬性规则（优先级最高）

1. **只用版本更新，禁止补充文件**：修订/补充信息必须合并进
   `<主题>_v(N+1).<ext>`，旧版进归档；**绝不**新建 `*补充*`、`*追加*`、
   `*addendum*`、`*supplement*` 之类文件。
2. `素材/` 只读；交付物只写 `产出物/<品类>/`；归档只增不删。
3. 一个交付物任何时刻只有一个当前版本，`INDEX.md` 指向它。

## 标准工作流

### 1) 初始化（幂等，不覆盖已有文件）

```bash
python scripts/scaffold.py <项目根>            # 默认品类
python scripts/scaffold.py <项目根> --categories 报告,专利   # 自定义品类
```

### 2) 新交付物 / 新版本

```bash
# 分配下一版本路径（打印 产出物/<品类>/<主题>_vN.<ext>，并登记 INDEX）
python scripts/new_artifact.py --category 报告 --name 季度方案 --ext docx \
    --project-root <项目根> --note "初稿"
# ↑ 把生成工具的输出路径设为上面打印的路径（docx_create 的 path 参数等）
```

### 3) 修订（用户不满意 / 补充信息）

```bash
# ① 读取当前版本（read_file / docx_read）
# ② 合并修改，写入 new_artifact 分配的 _v(N+1) 路径
# ③ 归档旧版（自动移入 归档/YYYY-MM-DD_<原名>，并更新 INDEX）
python scripts/archive_artifact.py --path 产出物/报告/季度方案_v1.docx \
    --project-root <项目根>
```

### 4) 自检（补充文件违例）

```bash
python scripts/lint_artifacts.py --project-root <项目根>   # 违例时非零退出
```

## INDEX.md 约定

- 主表：`| 文件 | 版本 | 日期 | 状态 | 说明 |`（状态：草稿/当前/已归档）
- 归档表：`| 文件 | 归档日期 |`
- 脚本会自动维护；手工补充时保持行格式一致。

## 注意

- 品类可扩展：`new_artifact --category 新品类` 会自动建目录与 INDEX；
  新增品类属于「Ask first」，先征求用户同意。
- office 工具的 `chart_make` 默认写 `产出物/diagrams/`（工具内置子目录），
  视为图表品类的工作目录，交付图表建议显式写入 `产出物/图表/`。
