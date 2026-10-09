---
name: project-init
description: 项目组织与产物管理：初始化 AGENTS.md + 素材/中间产物/归档 顶层结构；交付物放项目根目录，版本演进自动归档（禁止补充文件）
triggers: 初始化项目,项目结构,新建项目,项目组织,归档,版本管理,产出物整理,整理产出物
version: "2.0.0"
---

# 项目组织技能（project-init）

维护本项目的标准结构：**交付物直接放项目根目录**（参考 Muse/dots 的 Artifacts
思路——成品平铺直给）+ `素材/`（只读输入）+ `中间产物/`、`归档/`（顶层可见
目录）。完整约定见项目根 `AGENTS.md` 与主仓库 `docs/project-structure.md`。

> 本技能目录下的脚本以**技能目录**为基准调用（下文命令均相对技能目录）。
> 「新建项目」入口（桌面端）会在创建时**自动初始化**该结构——无需再跑
> scaffold；本技能主要用于**存量项目补建**结构与日常维护（版本/归档）。
> v2 结构不再有 产出物/ 品类目录与 INDEX.md；存量项目的旧结构保留兼容，
> 不迁移。

## 何时使用

- 用户要求「初始化项目 / 建项目结构 / 整理产出物」→ 运行 scaffold；
- 任何交付物产出或修订 → 用 new_artifact / archive_artifact 维护版本演进；
- 怀疑出现补充文件 → 运行 lint_artifacts 自检。

## 硬性规则（优先级最高）

1. **只用版本更新，禁止补充文件**：修订/补充信息必须合并进
   `<主题>_v(N+1).<ext>`，旧版进归档；**绝不**新建 `*补充*`、`*追加*`、
   `*addendum*`、`*supplement*` 之类文件。
2. `素材/` 只读；交付物只写项目根目录；过程文件写 `中间产物/`；
   `归档/` 只增不删。
3. 一个交付物任何时刻只有一个当前版本（根目录），旧版本都在 `归档/` 里。

## 标准工作流

### 1) 初始化（幂等，不覆盖已有文件）

```bash
python scripts/scaffold.py <项目根>
```

scaffold 同时会生成 `.litework/project.json`（运行环境清单）——按项目特征文件
（pyproject.toml / package.json 等）自动检测安装与验证命令，Agent 后续优先使用
这些命令（见主程序 W5：环境即第一类上下文）。已有的清单不会被覆盖。

### 2) 新交付物 / 新版本

```bash
# 分配下一版本文件名（打印 <主题>_vN.<ext>，目标位置为项目根目录）
python scripts/new_artifact.py --name 季度方案 --ext docx \
    --project-root <项目根>
# ↑ 把生成工具的输出路径设为项目根目录下的上面打印的文件名
#   （docx_create 的 filename 参数等）
```

### 3) 修订（用户不满意 / 补充信息）

```bash
# ① 读取当前版本（read_file / docx_read）
# ② 合并修改，写入 new_artifact 分配的 _v(N+1) 文件名（根目录）
# ③ 归档旧版（自动移入 归档/YYYY-MM-DD_<原名>）
python scripts/archive_artifact.py --path 季度方案_v1.docx \
    --project-root <项目根>
```

### 4) 自检（补充文件违例）

```bash
python scripts/lint_artifacts.py --project-root <项目根>   # 违例时非零退出
```

## 注意

- 交付物**只写项目根目录**，不建子目录（图表/报告/论文等品类信息用文件名
  表达，不用目录）；
- office 工具（docx/xlsx/pptx/pdf/图表）v1.6.0 起默认就写项目根目录，
  中间产物（文档内嵌图表、渲染预览）自动落 `中间产物/`；
- 存量项目（有 产出物/ 目录的旧结构）照常识别与读取，不做迁移；旧结构里的
  版本演进仍按原路径进行，不跨结构混放。
