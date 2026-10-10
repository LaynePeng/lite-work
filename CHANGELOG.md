# 更新日志

所有显著变更记录在此。格式参考 [Keep a Changelog](https://keepachangelog.com/)。

## [1.10.7] — Goals 全局视图 + 个人区与批量授权 + 「规则」Tab + 反思草稿（W10）+ 富内容气泡

### 新增（富内容气泡：render_card 工具）
- **Agent 可生成富内容卡片**（对齐 Muse 富气泡场景）：新工具 `render_card(html,
  title, height)`——多实体对比（商品/方案配图片价格）、图表、结构化展示在
  聊天流内直接渲染，不再只是一坨 markdown 文字；
- **渲染安全**：HTML 落盘 `.lite-work/cards/<session>/`（消息流只存卡片 id，
  LLM 上下文不回填 HTML）；前端 sandbox iframe 渲染（无 allow-scripts，
  LLM 生成的脚本不执行、无弹窗/顶层导航）；`<script>` 落盘前再剔一层；
- **媒体引用**：卡片内 `<img>/<video>/<audio>` 支持远程 https URL 与
  workspace 相对路径（前端 rewrite 到 `/api/files/raw`，复用越界检查与鉴权）；
- **markdown 图片修复**：assistant 回复里的 `![](相对路径)` 此前断链——现在
  同样 rewrite 到 `/api/files/raw` 渲染（本地截图/图表/技能产物直接可见）；
- 后端新增 `GET /api/cards/{session}/{id}`（card_id 格式校验防路径注入）。

### 新增（任务后反思 → 技能草稿，路线图 W10）
- 任务完成后基于 W7 轨迹（未开启轨迹时退化用会话消息尾部）提炼可复用流程，
  生成 SKILL.md 草稿落个人区 `~/lite-work/personal/skill-drafts/`，文件头带
  会话/素材溯源与采纳指引——**用户审阅后手动采纳，绝不自动生效**；
- 旁路纪律：反思失败静默（不阻塞任务收尾）；LLM 判定不值得沉淀（NO_SKILL）
  则不落盘；默认关闭（`reflection_enabled=false`，反思花 LLM 费用）。

### 新增（「规则」Tab，dots custom rules 思路）
- 侧栏新 Tab：**全局规则**（`~/.lite-work/RULES.md`，注入所有任务的 system
  prompt 基线）与**项目规则**（当前项目 AGENTS.md 可视化编辑）双 scope 切换；
  全局先注入、项目指令就近覆盖；保存后新任务即生效；
- 后端 `GET/PUT /api/rules`；system prompt 新增全局规则注入段（文件不存在时
  prompt 与旧版逐字节一致，缓存前缀不受影响）。

### 新增（Goals 全局视图 + 个人区，设计对齐 Muse/dots「任务属于我」）
- **任务 Tab「本项目 | 全部」切换**：「全部」显示跨项目全部任务（含个人区），
  按运行中 → 置顶 → 最近活动排序——目标不属于某个聊天窗口，属于我；
- **项目徽标**：全局视图每张任务卡显示归属（📁 项目名 + 路径 hash 色点 /
  👤 个人），点跨项目卡 = 热切换项目 + 进会话（复用 openSessionWithProject
  管线，无确认弹窗）；
- **新建任务向导「落盘位置」**：当前项目 / 👤 个人（`~/lite-work/personal`，
  跨平台 expanduser，与隐藏数据目录 `~/.lite-work` 区分）/ 📂 任意目录
  （新增 Electron `choose-directory` IPC 纯选目录不切工作区）；个人/自定义
  目录时隔离工作树置灰（无 git 上下文）；
- **批量目录授权**（跨项目任务核心）：新工具 `request_path_permissions`
  ——Agent 规划第一步统一盘点所需目录一次性申请（避免边做边逐个触发审批），
  批准后落 path_prefix 通配规则（`tool:"*"` 任意路径型工具命中，读写分离）；
  漏盘点的路径仍走既有单路径审批兜底；向导「先规划」分支注入盘点指令。

### 新增（任务 Tab P2 补齐：重命名/置顶 + 子 Agent 树 + 交付物指针）
- **任务卡重命名/置顶**：`PATCH /api/sessions/{id}`（name 空串 = 清除自定义名；
  metadata.pinned）；排序变为运行中 → 置顶 → 更新时间倒序；卡片行内重命名
  输入 + 📌 置顶按钮（完成态卡同样可用）；
- **子 Agent 派生树**：任务卡「🌳」按钮打开派生树抽屉（subagent_records
  归档 + 运行态合并展示，按嵌套深度缩进）；subagent:completed 事件与归档
  记录补 depth 字段，跨重启恢复保真；
- **交付物指针（P3）**：TODO 全部完成的任务卡聚合根目录顶层、任务期间产出
  （mtime ≥ 会话创建时间）的文档/媒体文件（正向扩展名清单 + 工程约定文件
  排除），完成卡显示「📄 文件名」chips 点击经文件页签打开；仅对完成态计算
  （省 O(n) 目录扫描）。

### 修复
- **后端可执行路径必须判定为文件**（electron/main.js resolvePython）：自动
  更新落盘过程中 `litework-bin/lite-work-backend` 会先以目录形式存在，旧逻辑
  `existsSync` 一看存在就 spawn → EACCES（v1.10.5 更新窗口内启动失败即此因）；
  改为 `statSync().isFile()` 判定（打包产物已含此修复的 DMG 曾验证）。

### 新增（任务 Tab P2 续作：新建任务向导弹窗）
- **「＋ 新建任务」改为目标向导**（不再开空白对话）：任务目标（必填）+ 执行方式
  （先规划再执行 / 直接干）+ **计划项（可选，每行一个 TODO 步骤；留空则 Agent
  自动规划）**+ 可选开关（目标循环 /loop · 自动续推 /continue · 隔离工作树
  /worktree，非 git 仓库时置灰）+ 协作模式下拉；
- **开始任务**：建会话 → 写目标（🎯 横幅 + 任务 system prompt 注入）→ 填了计划项
  就经新端点 `POST /api/sessions/{id}/todos` 预置 TODO 看板（进对话即有进度）→
  套配置 → 进入会话 → 自动发出引导指令（先规划分支要求 Agent 先 todo_write 建
  TODO 再逐项推进；直接干分支不强制；有预置计划则一并列出供 review）→ 任务 Tab
  立即出现运行中置顶的任务卡，Agent 建立看板后即显示进度；
- 无模板、无任务类型快捷（目标与计划均由用户手动输入）；后端仅新增
  `POST /api/sessions/{id}/todos`（复用 TodoPlugin.seed_board），其余全复用
  createSession / setSessionGoal / setSessionWorktree / setSessionCollab /
  taskLauncher 既有管道。
- 修复：任务 Tab 顶部「←」此前只切 projectsView 不切 Tab，点击无效——现在
  一并切回第一个 Tab（项目）。

### 新增（任务 Tab P2：续任务 / 派生按钮）
- **任务卡「⏭ 续」**：未完成且不在运行的会话可直接从卡片续推——进入该会话并
  自动发送推进指令（复用 AUTO_CONTINUE 同款 prompt 与发送管线）；
- **任务卡「⤴ 派生」**：完成态卡一键克隆会话骨架到新会话并进入——继承
  goal / 协作模式 / 模型 / 隔离工作树开关 / TODO 结构；TODO 全部重置为待办
  （不继承旧进度），对话历史不复制；非 git 项目静默跳过隔离工作树继承；
- 后端新增 `POST /api/sessions/{id}/derive`（复制 metadata + 以 TODO 种子写入
  新看板）；`TodoPlugin.seed_board` 提供看板种子写入（状态强制 pending）。

## [1.10.5] — 项目结构 v2 + 设置页配置全量透出 + 侧栏「任务」Tab

### 新增（任务 Tab：Goals 视图，参考 Muse / dots 的 Goals 设计；设计文档 docs/goals-tab-design.md）
- **侧栏「任务」Tab**：会话从聊天列表升格为目标看板——任务卡四段式呈现：
  进度（TODO 看板百分比 + 渐变条）/ 当前步骤（▶ in_progress + 下一步）/
  最近活动（最后工具调用 + 成败 + 相对时间）/ 元信息（成本 · 子 Agent 数 ·
  消息数 · 快捷操作）。宽度纪律：280px 侧栏内设计，标题/活动行
  nowrap+ellipsis，步骤最多 3 行；**完成态卡自动收敛**为三行（✅ + 100% 细条 +
  全部完成），翻历史不被旧任务占满；无 TODO 的会话退化为轻量卡。
- **运行中徽标**：绿点脉动（TaskManager active_for_session），运行中的任务
  钉在列表最前——「正在替我干活的优先可见」。
- **轨迹抽屉（W7 透出）**：任务卡 📈 按钮打开，展示最新轨迹的事件时间线
  （7 类事件图标）+ 级联漏斗 findings；未开启轨迹时引导去设置页开启。
- **/api/sessions 聚合字段**（全部可选，旧前端无感）：`todo_progress` /
  `last_activity`（消息尾部倒扫，含工具成败对齐）/ `subagent_count` /
  `cost_usd`（内存态，跨重启留空）/ `running`。
- **刷新链路**：`todo:updated` / `task:done` → 400ms 合并刷新（防 TODO 高频
  更新引发列表刷新风暴）。

### 修复
- **test_sessions_list_strictly_scoped_to_workspace 偶发失败**（本会话两次复现）：
  根因是 `POST /api/chat` 异步落盘首条消息与测试断言竞态。新增
  `_wait_first_message_saved` 轮询等落盘——顺带修复「other 会话未等落盘就断言
  not in」的假阳性漏洞（未显示可能是还没保存而非过滤生效）。单测×8、
  全文件×8、全量×3 连续通过。

### 变更（项目结构 v2，参考 Muse / dots 的 Artifacts 思路）
- **交付物直接放项目根目录**：不再建 `产出物/<品类>/` 四层目录树（6 品类 ×
  INDEX.md + 中间产物/ + 归档/）；品类信息用文件名表达，Finder/IDE 打开即见。
- **中间产物/ 与 归档/ 提升为顶层可见目录**；版本演进保留（`_vN` 命名 +
  旧版进 `归档/YYYY-MM-DD_<原名>` + 禁止补充文件）。
- **侧栏「产出物」Tab 移除**：交付物在「文件」树根目录直接可见；办公工具
  执行后刷新文件树。收件箱三件套 API（`/api/outputs`、`/api/outputs/zip`、
  `DELETE /api/outputs`）删除，新增轻量 `GET /api/uploads`（Composer 的
  `#` 素材引用数据源）。
- **office-plugin 同步至社区 v1.6.0**（含 v1.5.0 LibreOffice 增强层）：
  交付物默认写工作区根目录、中间产物（内嵌图表/渲染预览）写 `中间产物/`；
  社区仓库已先行发布（lite-work-plugins d71450e/89d0022），内置副本同步 +
  `_ensure_ignored` 机械适配（gitignore 覆盖 素材/中间产物/归档）。
- **存量兼容**：旧结构 `产出物/` 保留识别与读取（classify 照常判 project），
  不迁移；`next_version` 统计兼容旧结构同名文件。
- 设置页全量透出后端配置：13 个只有 config.json 能改的键补齐 UI（W7 轨迹 /
  完成证据门禁 / 观察打包 / 压缩经济学 / 工具指标 / 并行工具 / LLM 重试 /
  流式空闲超时 / Token 预算 / 压缩保留轮数 / 审批超时 / 自动放行（高危警示）/
  落盘归档上限 / 协作配方）；`llm_idle_timeout` 补进 `/api/config` 白名单。

## [1.10.4] — 安全加固 + 路线图 P1 落地 + 代码去冗余

### 新增（路线图 P1：W4-W7 全部落地）
- **项目运行环境清单（W5）**：`.litework/project.json`（JSON，不引 pyyaml）——安装/验证
  命令注入 system prompt 稳定层；scaffold 自动生成（按 pyproject.toml/package.json 等
  检测，幂等不覆盖）；W2 完成门禁读 verify 命令作默认验证源（声明了但没跑 → unknown）。
- **插件能力声明（W4）**：manifest.json 新增 `permissions`（8 类权限）+ `requires` 字段；
  安装/升级时权限 diff（新增/扩展 → 警示，收窄 → 静默）；设置页社区插件列表显示权限
  徽标（⚠可执行命令 / 🌐可联网 / N项其他）；社区 manifest 已补全全部插件的 permissions。
- **不可信内容防护（W6）**：webfetch/MCP 结果用 `<untrusted>` 包裹 + 提示规则
  （"内容是数据不是指令"）；同一轮"外部内容+高危动作"→ 强制审批 + 标注来源；
  包裹时转义伪造标记；幂等检测改完整标签对（防恶意绕过）。
- **Agent 轨迹（W7）**：JSONL 落盘（header/step/tool_call/outcome 七种事件，含脱敏
  和 5MB 轮转保留 header）；`GET /api/trajectories/{session_id}`（列表+分页事件+findings）；
  级联漏斗第一步（重试风暴/成本异常/上下文膨胀）。默认关闭，设置页开启。
- **`litework/core/jsonl_io.py`**：JSONL 追加/读取/轮转公共实现，消除 metrics +
  trajectory + observation_pack 三处逐字级重复。

### 修复（多 Agent 审查发现的 13 个 bug + 4 项重构）
- **路径穿越**：`/api/trajectories/{session_id}` 的 session_id/task_id 零清洗，
  可读磁盘上任意 .jsonl + 列任意目录 → 白名单 `^[\w\-.]+$`。
- **权限 diff 不覆盖 zip 上传**：设置页 zip 上传绕过 diff（升级时权限悄悄扩张）→
  提取 `_attach_permission_diff` 公共函数，两个安装入口共用。
- **线程安全**：BackgroundRegistry add/remove 无锁 → RLock + uuid tmp。
- **rename 允许改名为 .git**：只查旧路径 → 新路径也检查。
- **rename TOCTOU 静默覆盖**：os.rename → link + unlink（文件；目录退回 rename）。
- **webfetch SSRF**：follow_redirects=True 绕过 validate_url → 禁自动重定向 +
  逐跳校验（最多 5 跳）。
- **POST /api/config 无键白名单**：可写任意键 → 白名单校验（与 GET 对齐）。
- **runtime_manifest version="abc" 崩溃**：int() 抛 ValueError → _safe_int。
- **provenance 幂等绕过**：只查 startswith → 完整标签对检测。
- **provenance 伪造标记**：外部内容嵌 <untrusted> 混淆 → 包裹时转义。
- **gate 部分未解析误判 pass**：1 条 None + 2 条 0 → pass → 有任何 unparsed → unknown。
- **trajectory retry_storm 重复告警**：连续 5 次产出 3 条 findings → 峰值记录一条。
- **条件鉴权 `request: Request = None`**：17 处改为必填 + 无条件 check_auth。
- **fs_list truncated 误报**：dirs/files 各自 cap 按总数判断 → 各自判断。
- **删除文件/目录失败不刷新**：目录已被外部删除时树卡在旧状态 → 失败也 refresh。
- **zip 解包前缀校验少 `/`**：`startswith(tmp)` → `startswith(tmp + os.sep)`。

### 重构
- 删除 `install_from_source` 纯别名层（直接调 `import_source`）。
- `set_workspace` 与 `open_recent_project` 的守卫提取为 `_switch_workspace_guarded()`。
- 清理运行时代码中 49 处 W 编号与手册页码引用（18 个文件）。
- `rotate_if_needed` 保留 header 行（轨迹轮转后不丢失元数据头）。

### 测试
- 新增 `test_runtime_manifest.py`(18) / `test_plugin_permissions.py`(18) /
  `test_provenance.py`(13) / `test_trajectory.py`(15) 共 64 个新用例。
- 全量 pytest 797 passed；mypy 91 文件零错；tsc 零错。

## [1.10.3] — 工作区缓存修复 + 文件页签目录操作 + 界面偏好持久化

### 新增
- **文件页签支持目录右键操作**：此前只有单文件能右键，目录行没有菜单。现支持
  **新建文件 / 新建文件夹 / 重命名 / 在文件管理器中打开 / 复制路径 / 删除目录**
  与目录行内重命名（用 `menu.isDir` 区分菜单，文件菜单保持不变；批量删除仍只支持文件）。
  后端配套：新增 `POST /api/files/create`（新建空文件/空目录）、`DELETE /api/files` 支持
  `recursive=true` 递归删目录（不带该参数仍拒绝，工作区根与 `.git/` 一律拒绝）、
  `POST /api/files/rename` 支持目录改名（扩展名一致约束仅对文件生效，目录名 `v1.2` 合法）。
- **`docs/ai-native-harness-roadmap.md`**：基于《AI Native 研发范式实践手册》整理的
  路线图与具体设计（W1–W10 工作流、排期依赖、横切原则、验证灰度与待决问题）。

### 修复
- **切换工作区后 `execute_command` 的 cwd 停留在旧项目**（核心修复）：`ShellPlugin`
  改为**按 workspace 为键缓存**（缓存键即目录，结构上不可能再用到旧目录实例），后台命令
  注册表提到 app 级共享（切换项目后旧 `task_id` 仍可查询/终止），并删除为此打补丁的
  “有后台命令就拒绝切换”拦截。对齐 OpenCode 的「实例按目录缓存」模型。
- **删除目录/目录变化后文件页签卡在「无法读取工作区」**（需切 tab 重挂载才恢复）：
  根因是刷新用 `Promise.all` 并发加载所有已展开目录，任一目录 4xx 即整体 reject →
  错误态，且失效路径一直留在已展开集合里使之后每次刷新都失败。改为 `Promise.allSettled`
  逐目录独立结算，剔除失效目录（含其子孙路径缓存），仅根目录不可读才算错误；
  点击刚被删除的目录也不再置全局错误态。
- **界面偏好（侧栏/右栏宽度、侧栏页签）重启后回默认**：桌面端本地 Core 端口随机导致
  渲染层 origin 变化、`localStorage` 按 origin 隔离读不回。改为存后端 config（新增
  `ui_prefs` 键 + `/api/config` 白名单暴露），首帧前 hydration（无闪烁）；右栏默认宽度
  改为随窗口比例实时重算（不再是一次性 `innerWidth` 快照）。
- **输入历史 `↑` 只能往前翻一条**：守卫误用 `!text`（翻出一条后输入框非空即被挡），
  改为「输入为空**或**已在翻阅态」，手动编辑退出翻阅态。

### 测试
- 新增 `tests/test_fs_dir_ops.py`（13 例：新建/递归删除/目录改名 + 越界、`.git`、
  同名、批量删除仍拒目录等守卫）、`tests/test_background_agent_guard.py` 配套用例、
  `web/src/components/Sidebar.test.tsx`（目录右键 + 文件树卡死回归，后者经变异验证
  可复现原 bug）、`web/src/hooks/useResizable.test.tsx`、`Composer.history.test.tsx`。

## [1.10.1] — 判断面板决策树 + 压缩判定器 + 插件钩子架构修复

### 新增
- **判断面板改决策树（插件判定 UI 协议 v2）**：此前面板按「节点时间线」渲染（`trigger` / `kind` /
  `verdict` / `confidence` / `accepted` / `action` 一串 chip），读者得自己在脑子里把节点拼成结论。
  现改为**决策树卡片**：根问题（`title` / `context`）+ 选项分支（每项带概率与语义色比例条，
  **命中的那项高亮**）+ 置信度/阈值判定行——判定与依据一眼可读。schema 同步升级为
  `trees[].{title, context, question, kind, options[{label, prob, tone, chosen}], confidence, threshold, accepted}`。
  面板 tab 与内置 6 个 tab **共用同一选中态**（视图 key `plugin:<插件名>:<面板id>`，`PanelTabId`
  拓宽为开放字符串，避免每加一个插件面板都要改联合类型）。
- **判断面板激活期间每 5s 自动刷新**：判定记录本质是日志，只在激活时抓一次会看不到新记录（只能手动点刷新）。
  现在**每次激活重抓**，且激活期间每 5s 轮询，切走即停（`setInterval` 在 effect 清理里 `clearInterval`）。
- **压缩判定器钩子（`app.compaction_decider`）**：上下文压缩新增内核钩子，插件（Jev 判定）可返回
  「保留消息」直接替换旧历史；判定器返回 `None` / 抛异常 / 未注册时**退化回原 LLM 摘要**（D0–D8 退化回退），
  行为不倒退。实现要点：`header_context` 注入**只在 LLM 摘要分支**做（Jev 路径不需要 LLM，
  不被前置依赖卡死）；Jev 分支必须**拼接 tail**——最新轮次不得丢失（测试抓出的关键 bug）。

### 修复
- **插件钩子在真实任务路径失效（本版最重要的一处架构修复）**：`create_kernel(registry)` 此前不装工具插件，
  而插件注册在 `build_registry` 引导 kernel 上的 `before_tool` / `before_llm` 钩子随该内核一起被丢弃 →
  判定类插件（Jev 门禁 / 直答）在**真实 Agent 任务里从不生效**（只在插件自测路径生效，所以一直没被发现）。
  现以 `registry.has(name)` 作过滤器重装（被裁剪的工具不重注册，plan 只读语义保持），并用
  `plugins_workspace` 维持 worktree 隔离语义；`SecurityPlugin` 的判定意见暂存移到 middleware 最顶部
  （`delete_file` 等分支不再先发审批）。
- **审批卡「Jev 意见」块高度**：先固定 148px（去滚动条）——实测内容一多 `overflow: hidden` 就**截断**、
  `why` 被 `-webkit-line-clamp: 2` 吃掉，反而更乱；改为**自适应高度**（内容自然展开、不截断、
  无自己的滚动条），溢出统一交给中段滚动区（`.approval-body`）。
- **审批卡可能「很矮 + 内容显示不全」，按钮甚至被挤出卡片**：卡片限高用的是 `max-height: 85%`，
  但遮罩是 grid 容器，卡片作为 grid item 的**百分比基准偏小**（实测聊天区 420px / 遮罩内容盒 372px
  时只解析出 285px，而非 316px——把子项全设 `flex-shrink: 0` 后卡片仍 285px，可见不是子元素收缩所致），
  而且**基准是聊天区高度**：聊天区一矮（小窗口 / 布局被挤）卡片就跟着塌。
  叠加收缩策略错误——`.approval-action` / `.approval-reason` 带 `min-height: 0` 可被压到 0，
  而 `.approval-opinion` 已改自适应（不可缩）：收缩全压在命令与原因上。实测聊天区 420px 时
  **卡片 285px 装 515px 内容**，命令只剩 20px、**原因被压成 0px（完全看不见）**，
  按钮被挤出卡片、被 `overflow: hidden` 裁掉（点不到「允许/拒绝」）。
  修复分两步：① 限高改**视口基准** `min(calc(100vh - 96px), 100%)`——不再随聊天区塌缩，
  同时保留「不超出遮罩」的兜底（实测只按视口封顶时，遮罩 grid 居中溢出会让**卡片顶部不可达**）；
  ② 命令 / 原因 / 判定意见统一放进新增的 `.approval-body` 滚动区（三者 `flex-shrink: 0`，
  去掉各自的内滚动）——长内容只出现**一个**滚动条，底部按钮常驻卡内；末尾子元素 `margin-bottom`
  归零，避免内容本来就装得下时也冒滚动条。实测（无头 Chrome，真实样式表）：聊天区 900/700/500/420
  及多审批标签场景下按钮均在卡内、卡片不超遮罩，命令/原因/判定意见都保持完整高度（420/40/254px）。
- **`_approve_skill` 的 `approval:request` 缺必填字段 `judge_opinion`**：strict 事件负载校验（CI）直接失败。
  补 `judge_opinion: None`（恒定携带，`None` = 本路径无判定插件意见）。

### 内部
- **核心插件无关性门禁**：新增 `tests/test_core_plugin_agnostic.py`——核心代码与文档字符串零插件名
  （注释放行；测试文件排除，只守运行时核心）；新增 `tests/test_judge_opinion_e2e.py`：真实
  `AgentApp` + `SecurityPlugin` → `approval:request.judge_opinion` 带插件自报 `source`，且 0.91 不硬拦
  （并断言 `judge` 必须是 `async`，防 `'dict' can't be awaited` 回归）；顺带修 4 条与实现脱节的插件测试
  （私有镜像隔离 fixture、noul mock、门禁阈值预期）。压缩判定器日志文案同步中立化。
- **打包体积**：PyInstaller `--collect-all` 会把 numpy/pandas 等第三方包的 `tests/`、`__pycache__`
  一并收进 `_internal`（运行时用不到，纯冗余）→ 打包后自动清理，只清第三方、不碰 lite-work 自身产物。
- **版本**：1.10.0 → 1.10.1（`sync-version` 同步 4 个 npm 文件）。

## [1.10.0] — 通用插件 UI 协议（插件设置页 + 右栏插件面板 + 用户直问短路）

### 新增
- **通用插件 UI 协议（`Plugin.contributes`）**：插件基类新增 `contributes`（声明设置项 / 面板）、
  `status_from_config`、`panel_content`；`list_plugins` 透传 `contributes` 与 `status`，
  设置页据此渲染「未启动 + 原因」与配置表单。**插件 UI 不再需要主程序为每个插件写死界面**。
- **插件设置页**：新增 `GET /api/plugins/{name}/settings`（schema + 当前值，secret 打码）与
  `GET /api/plugins/{name}/panel/{id}`（Markdown）。设置页插件卡片新增「⏸ 未启动」徽章与
  「▸ 配置」通用表单：支持 Base URL、自定义请求头（JSON 键值表）、密钥（**留空不改、不回传真值**）、
  数字、开关；保存复用 `POST /api/config` 任意键透传。
- **右栏插件面板**：`ToolPanel` 动态 tab——拉取插件列表，把 `contributes.panels` 渲染为右栏 tab
  （**无插件时不影响内置 tab**）；面板内容走后端 Markdown 接口，用 ReactMarkdown 渲染；
  视图 key 形如 `plugin:<插件名>:<面板id>`，与内置 6 个 tab 共用同一选中态。
- **用户直问短路协议（内核）**：`before_llm` 中间件可在 `ctx.metadata["final_answer"]` 放答案 →
  `AgentLoop` **跳过本次 LLM 调用**，以该内容作为助手回复收尾并广播 `message:added`
  （复用现有事件与 `_finish`，不新增事件类型）。一次性取用（`pop`），任何异常都不影响正常链路；
  **无插件设置时链路零变化**。
- **插件配置表单重排**：字段标签在上、控件满宽（`max-width: 820px`）；`headers` 改为 6 行等宽大文本框，
  可整段粘贴；**保存时才解析 JSON**（打字不重排，非法 JSON 明确报错）。右栏插件面板去掉图标
  （与其他内置 tab 一致），并去掉「抓一次就缓存」，改为每次激活重抓 + 刷新按钮。

> 说明：1.10.0 未单独打 tag / 发 GitHub Release；GitHub Release `v1.10.1` 的版本范围说明覆盖
> `v1.9.11..v1.10.1`，即本条目与上一条一并包含在内。

## [1.9.11] — 本轮 token 速度面板 + 打开项目默认最近会话 + 布局修复

### 变更
- **右侧面板页签顺序：「MCP」移到「工具」之后**：MCP 属低频的配置/状态类入口，日常主要看
  工具与后台；新顺序为 上下文 / TODOs / Agents / 后台 / 工具 / MCP。（页签 id 未变，
  已选中的页签与外部传入的 `activeTab` 不受影响。）
- **输入框下方「模型」选择器：关闭态加省略号**：长模型名（自定义网关常见的 `provider/model`
  全路径）此前在关闭态是**硬裁**——在字符中间断开、文字顶到边框，观感上像「宽度跟着模型名
  变长」。现在限制在 `max-width: 260px` 内并以省略号收尾（`overflow/text-overflow/nowrap`）。
  **点击展开的弹层不受影响**：它由浏览器按最长选项绘制，仍显示完整模型名——**两者宽度无需
  一致**。触发器的 `title` 悬停提示本就带完整「供应商 / 模型」，信息不丢。
- **打开项目后的默认落点改为「该项目最近一条会话」**：此前「打开项目 / 打开代码 / 最近项目」
  三个入口都落到**空白新会话**，换项目后容易以为历史丢了、还得自己去侧栏翻上次的进度。
  现在统一改为：取该项目会话列表中 `updated_at` 最新的一条并打开（消息、`/goal`、协作模式、
  隔离工作树等沿用既有会话恢复逻辑）；**没有历史会话时仍新建空对话**。
  实现要点：「打开项目」（原生目录对话框）那条路径会整页 `window.location.reload()`，
  在旧页面里选不了会话，因此把落点意图写进 localStorage 带过重载边界，启动时消费一次即清除
  （不影响之后的普通启动）；该意图用 ref 保证 StrictMode 双执行下不重复开页签，并复用启动时
  建的空对话 tab（不会留下多余的「新会话」页签）。会话列表拉取失败同样退化为新建空对话，
  不阻塞打开项目。取「最近」不依赖接口顺序（显式按 `updated_at` 取最大）。
- **上下文面板「计费单价」两档标签精简**：「高峰全价（输入 / 输出 / 缓存命中）」→「高峰全价」，
  「空闲半价（输入 / 输出 / 缓存命中）」→「空闲半价」。括号说明在 331px 窄面板里会挤成多行，
  而两档的三个数字本就按「输入 / 输出 / 缓存命中」顺序同行展示，标签只需标明档位。

### 修复
- **顶部「模型元数据提醒」横幅会挤垮三栏布局**：该横幅是 `.app`（横排 flex 容器）的直接子元素，
  自身又没有宽度约束 → 它按内容宽度吃掉数百 px 行宽（1440 视口实测 553px），把对话区压成
  173px 窄条；又因横排的 `align-items: stretch` 被拉成 553×900 的竖块。触发条件是元数据缓存
  缺失或超 7 天（**首次安装必现**），因此平时不易发现。
  修复：`.app` 改为纵向容器，三栏移入新增的 `.app-row`（横排），横幅独占顶部一行；顺带把原先
  散在 `.main`(38px) / `.panel-tabs-wrap`(40px) / `.sidebar-brand`(44px) 的顶部让位统一到
  `.app` 的 `padding-top: 38px`（三处各自减去 38px），并把 `.tool-panel` / `.resizer.col` 的
  `height: 100vh` 改为 `100%`。
  实测：横幅由 553×900 变为 **1440×38**（「立即同步」按钮可点），对话区由 173px 恢复到
  **727px**；**无横幅时** TabBar(y=38) / 面板 Tabs(y=40) / 输入框(y=743,h=157) 三个基准点与
  改动前完全一致；两侧折叠恢复条位置与宽度正常。
- **测试基建：live server 用例偶发 `RemoteProtocolError`**：`test_server.py` / `test_sse_reconnect.py`
  都用「起真实 uvicorn（随机端口）→ 等 `server.started` → 立刻发请求」的写法，而
  `server.started` 只表示 lifespan 启动完成、并不保证 accept 循环已就绪；全量套件高负载下
  第一个请求偶发被断开（表现为随机变红）。抽成共享的 `tests/conftest.py::live_server`：
  用一次幂等 `GET /api/status` 轮询到 200 才算就绪，并给 httpx transport 打开连接层重试；
  服务器任务收尾移入 `finally`，就绪断言失败也不再泄漏。
  （不用 `ASGITransport` 的原因见该 helper 文档：它会缓冲完整响应体，无法交互式消费 SSE。）

### 新增
- **上下文面板显示「本轮 token 速度」**（对齐新版 opencode 的 current + average tokens/s）：
  右侧「上下文」面板新增一条速度条——本轮**生成速度**大数字 + 细分（**首字延迟**、
  **本任务平均**）+ 近 N 轮速度趋势图。为让「生成中 / 结束后」两态**布局尺寸完全一致**，
  不显示状态文案（「本轮生成中…／本轮生成速度」）与端到端速度，趋势图宽度固定
  （实测两态均为 145.81px，副行恒为「首字 + 本任务均」）。
  口径与实现：
  - **生成速度** = 输出 tokens ÷ 生成窗口（首块→末块），**排除首字等待**——这才是「模型吐字
    速度」，跨供应商/模型可比；首字延迟（TTFT）单列展示，避免把「推理模型首字慢、吐字不慢」
    误读成整体慢；端到端速度 = 输出 tokens ÷（请求发出→末块），贴近体感；
  - 流式期间模型只给字符增量（usage 要等流末），因此**流式中显示估算值**（增量 CJK×1.3 +
    其余÷3.8，与 `TokenCounter` 同公式）并标「估算」，**本轮结束用 usage 精确校准**；
  - 数据采集由一个极轻的 `StreamMeter` 完成：每个增量块只做「整数累加 + 该增量的 CJK 小正则」，
    **绝不对全文重复分词、也不引入 tiktoken**；实时进度事件 `llm:progress` 后端**节流约 400ms**
    （≤2.5 次/秒），因此额外开销相对「每块一次 `llm:stream`」可忽略；
  - 计量器通过 **contextvar** 注入适配器（`chat_stream` 签名不变）：社区/自定义适配器无需改动，
    不实现也只是拿不到速度；超时重试按**每次尝试重置**，只统计成功那次；子 Agent 与证据收据
    小模型不计入主任务速度；
  - 任务平均用**配对**分子/分母（Σ输出 ÷ Σ生成窗口），缺测轮次不会把平均值抬高。
  兼容与降级：无 usage → 保留估算值并标「估算」；旧载荷/适配器未计量 → 速度条整块不渲染
  （不占位、不报错）。`context:stats` 新增 `task.last.{ttft_ms,gen_ms,e2e_ms,tps_gen,tps_e2e,
  tps_estimated}` 与 `task.avg_tps`；新增 `llm:progress` 事件（已加入 `EVENT_FORWARD`）。
- **「价格检查过期」可在设置里配置（0 = 永不过期）**：设置 → 综合设置 →「模型元数据与定价」
  新增「价格检查过期时间（天）」（`config.pricing_check_ttl_days`，默认 7，**0 = 永不过期**），
  设为 0 后不再提示「建议同步」。
  设计上按**「数据归插件、策略归主程序」**落地：定价插件只报缓存年龄
  （`source_age_seconds` / `age_seconds`），是否算过期由主程序按该配置统一复算——
  因此**同一个窗口同时作用于定价插件各官方源与 models.dev**，且**不必改插件本体**
  （`pricing-plugin` 属社区同步代码，只改内置副本会造成与上游分叉，见 AGENTS.md §6）。
  口径保持：`status()` 的「从未同步」仍算过期（除非设为永不过期），`lookup()` 的
  「用内置快照兜底」不算过期；`stale` 只影响提示，**不改变联网行为**
  （models.dev 的联网挡板仍是 `model_meta.CACHE_TTL_SECONDS`，定价同步一直是手动的）。
  `/api/model-meta` 新增 `check_ttl_days` 字段供设置页回显；`/api/config` 暴露该键。

## [1.9.10] — Esc 连按两次停止当前任务

### 新增
- **`Esc` 停止当前任务（连按两次）**：任务运行中连按两次 `Esc` 等价于点输入区的「停止」按钮
  （同一 `stop` 路径，请求后端停止任务并收起待审批卡）。首次 `Esc` 只进入「待确认」态——
  提示行显示「再按一次 `Esc` 停止任务」——2 秒（`ESC_STOP_CONFIRM_MS`）内再按一次才真正停止，
  超时自动解除。之所以不做「一按就停」：`Esc` 是高频手滑键，而停止会丢掉正在进行的回合。
  待确认态在「任务结束 / 切换会话或标签」时一并解除，避免在 A 会话武装、切到 B 会话后
  一次按键就停掉 B 的任务。
  与其它 `Esc` 交互的关系（「逐层退」）：
  - 设置 / 关于弹窗打开时让位给弹窗自身的 `Esc` 关闭；
  - 命令面板 / 输入历史 / 右键菜单 / 行内重命名等已占用 `Esc` 的交互优先——这些处理都在
    React 事件里 `preventDefault()`，全局监听据此跳过（命令面板还顺带覆盖了「无候选」的空面板）；
  - 当前会话没有运行中的任务时无操作。
  提示行同步：运行中提示「连按两次 `Esc` 停止任务 + 排队说明」，待确认态提示「再按一次」；
  README 快捷键表新增一行。新增前端回归测试：`App.esc.test.tsx`（连按停止 / 单按不停 /
  超时解除 / 空闲不动作 / 弹窗让位）与 `Composer.test.tsx` 的提示行与「面板吞掉 `Esc` 不冒泡」用例。

## [1.9.7] — 修复推理强度切换失效

### 新增
- **内置专利技能 `patent-disclosure-skill`（v4.8.0，随包分发）**：中国专利一站式技能——专利点挖掘
  与交底书（发明/实用/外观）编写、交底改写成申请文件四件套、案卷一条龙、公布公告著录检索、
  专利通俗解读、本地专利地图、审查政策简报与审查答复辅助；8 个子技能按意图路由，随包离线可用，
  可在 设置 → Skills 里管理。内容与社区上游 [lite-work-plugins](https://github.com/laynepeng/lite-work-plugins)
  的 `patent-disclosure-skill` 同源，可随社区更新独立升级。
- **专利技能语言规范**（随 v4.8.0 内置）：交底书、申请文件等一切交付文档的技术特征**尽量使用本领域
  通用技术术语**（连接/固定/夹持/检测/处理单元等），**禁止随意生造词汇**（如「环抱式定位握持」
  「插拔耦合组件」「时序锚定器」）；新概念用通用词组合并在首次出现处定义。落点覆盖顶层 SKILL.md
  总则、交底成稿与自检、申请文件主文件纪律与权要撰写。
- **专利技能依赖随包内置**：`latex2mathml`（公式→Word 可编辑）、`mammoth`（docx→md）、
  `playwright`（mermaid 出图 + CNIPA 检索，含平台 node driver）随安装包分发（`_internal/`），
  技能解释器离线直接复用，无需用户机器 Python 再装；浏览器复用系统 Chrome/Edge，无则首次
  `playwright install chromium` 后即可用。

### 修复
- **推理强度（reasoning_effort）切换失效**：主输入框与设置弹窗切换档位后，实际请求仍带旧档位
  （通常为空串 → 后端按「未指定」跟随供应商默认）。根因是 `send` 回调的 `useCallback` 依赖数组
  漏列 `reasoningEffort`/`draftReasoning`，闭包捕获了切换前的旧值。修复：`send` 内改走
  `getChat`/ref 读最新值（与 `sendQueuedItem` 一致），并补「新建会话 tab 的暂存档位在创建会话时
  接力写入会话」；新增前端 App 级回归测试（draft 路径 + 会话路径）与后端 override/payload 护栏测试。

## [1.9.4] — 桌面体验修复

### 修复
- **打开项目会把应用自身页面塞进系统浏览器**：本地模式下窗口先用 `file://loading.html`
  创建（`file://` 的 origin 是 `"null"`），而 `will-navigate` 的同源判定却拿创建窗口时的
  初始 URL 比对——打开项目成功后前端 `window.location.reload()` 触发导航，target 正是应用
  自身地址 `http://127.0.0.1:<port>/`，因与 `"null"` 不匹配被误判为外部链接，整页转交
  系统浏览器（应用 reload 照常执行，于是「浏览器多开了一个应用页面」）。改为在导航发生时
  动态取 `webContents.getURL()` 判断同源；外部链接转交浏览器的行为不变。

## [1.9.3] — 快捷键对齐 opencode v2 与桌面体验修复

### 新增
- **全局快捷键（键位对齐 opencode v2）**：
  - `Shift+Tab` 循环切换 Agent（原为 `Tab`）——把 `Tab` 让给命令面板补全，顺带修掉
    「面板里按 Tab 补全时误切 Agent」（`App.tsx` 的 window 监听未判事件目标，
    与 `Composer` 的补全处理同时触发）；
  - `Ctrl+Tab` / `Alt+↓`、`Ctrl+Shift+Tab` / `Alt+↑` 前后切换标签页；
  - `Ctrl+1..9` / `Ctrl+0` 切到第 N 个标签页；
  - `Alt+W` 关闭当前标签页、`Alt+N` 新建会话（未沿用 `Ctrl+W`：Electron 默认菜单的
    关闭窗口占用了它）；
  - `Ctrl+G` / `Ctrl+Home` 跳到对话最开头，`Ctrl+Alt+G` / `Ctrl+End` 跳到最末尾
    （焦点在输入框内时不劫持，保留光标到文首/文末的原生行为；跳到末尾即恢复流式贴底）；
  - `Ctrl+T` 循环切换推理强度（模型变体：关 → 低 → 中 → 高 → 最大）。
  - Alt 组合按物理键位 `event.code` 匹配（并保留 `e.key` 兜底）：macOS 上 Option+数字/字母
    可能产出特殊字符（如 `¡`/`∑`），按物理键位可保证 `⌥1`…`⌥9` 选 Agent 在任何键盘布局与
    输入法下都命中——原先只看 `e.key` 的实现会随布局/输入法而变（挂中文输入法时 `⌥1`
    通常不产出 `¡`，故当时可用）。
- **状态驱动的快捷键提示**：Composer 底部提示行按当前状态显示最相关的一条——对话区被上翻时
  提示「`Ctrl+End` 回到对话最新」、多标签页时提示标签页快捷键；都不命中时回落原有安全策略
  文案。刻意不做随机/轮播：该行在流式输出期间高频重渲染，随机文本会跳动，且纯属噪音；
  也刻意不因「Agent 多于 1 个」提示 `Shift+Tab`（默认就有 4 个 Agent，那等于把安全提示永久挤掉）。

### 修复
- **产出物收件箱混入 Office 锁文件**：Word/Excel/PPT 打开文档时会生成 `~$xxx.docx` 之类的
  锁文件；它带交付物扩展名、又不以 `.` 开头，绕过了原有的隐藏文件过滤，被解析成一份独立产出物
  与真实文件并列显示。现按 `~$` 前缀过滤（LibreOffice 的 `.~lock.xxx#` 本就落在 `.` 规则里）。
- **产出物单击与文件树行为不一致**：文件树走 `resolveOpenTarget`——非文本类（docx/xlsx/pptx/
  pdf/图片）在桌面端用系统默认程序打开；而产出物面板另有一套判断，只对 markdown 优先系统程序，
  于是 `.docx` 单击进的是内置预览。现改为复用同一套规则：桌面端 markdown 与非文本类一律优先
  系统默认程序（与右键「用系统默认程序打开」一致），失败时文本类静默回退内置预览、非文本类
  明确报错；浏览器模式仍走内置预览（图片/PDF 可渲染）。
- **对话气泡内超长不可断词溢出气泡**：`.md` 规则块缺 `overflow-wrap`，超长 URL、`?token=…`
  这类无断行点的长串、长英文单词、行内 `code` 长路径都会直接撑破气泡背景（`.bubble` 的
  `max-width` 只约束盒子，不约束内容；实测溢出 636px / 468px / 317px）。现于 `.md` 加
  `overflow-wrap: anywhere`——用 `anywhere` 而非 `break-word`，因为前者参与 min-content
  计算，flex 子项（`.bubble`）才真的能收缩。代码块（`pre` 仍 `white-space: pre` 横向滚动）
  与表格（`th,td` 仍 `nowrap`、表格整体 `overflow-x: auto`）行为不变。
- **内置终端中文乱码（zsh 把多字节字符转义成 `\M-^X`）**：从 Finder/Dock 启动的 app 不继承
  shell 环境，`LANG` 为空；而 node-pty 起的是**非登录** shell，`/etc/zprofile` 里那句
  `LANG=C.UTF-8`（只对登录 shell 生效）不会执行 —— `LC_CTYPE` 落到 `US-ASCII`，zsh 会把
  prompt / 路径 / 输出里的多字节字符**逐字节转义成 `\M-^X` 字面量**（不可逆），各类工具的
  列宽计算也随之出错。现于 `terminal-start` 里补一个 UTF-8 locale（`LANG=C.UTF-8`，仅在
  用户与系统都没设时），与 Terminal.app 的登录 shell 行为对齐。
- **内置技能同步不再降级社区更新**：`sync_builtin_skills_to_user()` 的 `.litework-builtin`
  标记记录的是安装时的应用版本，应用升级时会用内置副本覆盖——若用户经社区更新过该技能，
  会被降回内置旧版。现改为按技能自身 `SKILL.md` 的 `version`（semver）比较：仅内置更新才
  覆盖，目标更高则保留，版本相同仅在应用版本变化时刷新内容。

## [1.9.0] — 内置定价插件与随包技能解释器

### 新增
- **文件 Tab 右键文件管理**：侧边栏「文件」页签的目录树支持右键 **重命名 / 删除** 文件
  （此前仅「产出物」面板可用）。重命名走行内编辑，交互与产出物面板一致。
- **内置技能新增学术论文配图** `academic-diagram`（v0.1.1）：TikZ 架构图 / 神经网络图 /
  算法流程图，73 个图标 + 9 个参数化模板，含 LaTeX 编译校验与 SVG 预览链路；
- **随包技能解释器**：打包内置独立 CPython 3.14（`_internal/skill-python`），技能脚本的
  `python3` 优先命中它，并通过 sitecustomize 复用 `_internal` 里随包收集的第三方包
  （PyYAML / pymupdf / python-docx / python-pptx / lxml / Pillow / openpyxl / xlsxwriter）——
  **用户机器无需安装 Python、无需联网 pip，装完即离线可用**；未随包分发时自动回退系统
  `python3`，行为与旧版一致。
- **内置定价插件 `pricing-plugin` 成为一等工具插件**：改写为 `ToolPlugin` + `PricingProvider`
  双基类（`get_tools()` 为空，不向 Agent 暴露工具，仅供主程序按模型指纹计费），出现在
  设置 → Plugins 的「已安装」列表（内置 v1.0.0，可随社区更新独立升级，同名本地包覆盖）；
  内置插件发现统一为 `load_builtin_plugins()`（工具 / 协作模式 / 定价三类共用一套机制）。

### 变更
- 内置技能 `academic-paper-engineering` 升级 **1.0.2**：能力分层修正（②a 四类输入解析为纯
  Python，不需要 LibreOffice；②b 旧格式转换 / xlsx 重算 / pptx 缩略图为可选增强），
  安装指引补 MacPorts / 官方 dmg 与 soffice 入 PATH，并写明 textutil / pandoc 降级路径；
  同时补 PDF 解析的 PyMuPDF 兼容 import（pymupdf 优先、fitz 回退）；
- PyInstaller 收集补充 `fitz` / `openpyxl` / `et_xmlfile` / `xlsxwriter` / `docx` / `pptx` /
  `lxml` / `PIL` / `yaml`，保证随包技能解释器可离线 import；
- `DELETE /api/files` 与 `POST /api/files/rename` 放开到**工作区内任意文件**（含嵌套目录下的
  源码文件），不再限定 `产出物/` `素材/`。底线保持不变：路径越界 `..` → 403、`.git/` 内部
  文件 → 403、删除仅限文件（目录 → 400）、重命名仅限同目录且不可改扩展名；
- 补充后端用例（嵌套路径删除/重命名、目录与 `.git` 拒绝）与前端「文件」页签右键用例。

### 修复
- **models.dev 手动同步失效**：`refresh()` 的 7 天 TTL 挡板会短接手动同步——缓存未过期时
  点「立即同步定价数据」也不联网，时间戳永远停在旧值（表现为「一直显示 N 天前」）。
  现手动同步走 `force=True` 强制拉取（启动路径不联网、TTL 行为不变）。
- **分时（空闲半价）展示与节省口径**：上下文面板顶部单价此前始终显示高峰基准价
  （`pricing.input` 是 peak，空闲档在嵌套的 `off_peak`），出现「标签写空闲·半价、数字却是
  全价」的错位；现按当前生效档展示，「缓存帮你省下」也改用生效档计算。后端计费本就按
  计价时刻选档（`_pricing_now`），成本数字未受影响。

## [1.8.0] — 素材引用与产出物文件管理

### 新增
- **办公 / 调研 Agent 支持 `#` 引用素材**：输入框输入 `#` 列出工作区 `素材/`（用户上传的分析素材）
  供选择，`Tab` / `Enter` 补全为 `#素材/xxx`，随消息交给 Agent 读取处理；
- **产出物 Tab 右键文件管理**：右键「产出物 / 素材」文件弹出菜单，支持 **重命名** 与 **删除**；
  重命名走行内编辑，后端校验（禁止改扩展名、同名冲突拦截），删除复用既有确认逻辑。

### 变更
- 新增 `POST /api/files/rename` 接口（仅限 `产出物/` `素材/` 内，防误改代码）；
- 补充前端用例（`Composer.materials.test.tsx`、`Sidebar.test.tsx`）与后端重命名用例。

## [1.7.1] — 审批卡与多窗口稳定性修复

### 修复
- **多个审批同时挂起时「卡在问权限前」**：此前每张待审批各自渲染一个全屏遮罩，多张互相覆盖
  （只有最后一张可见可点；点掉一张后下一张在原位几乎原样弹出），用户会误以为「点了没反应 /
  任务卡死」。改为**单个弹层 + 序号标签**（「第 i/N 个待审批」）逐条切换确认；
- **同进程多项目窗口互相顶掉鉴权令牌（先开窗口全部 401）**：Electron 的 `webRequest` 同一事件
  「只认最后注册的监听器」，而每个项目窗口（「新建项目窗口」）各自拉起一个本地 Core
  （随机端口 + 独立令牌），后开窗口会顶掉先开窗口的 `Authorization` 注入，先开窗口随即
  所有请求 401（表现为「停止失败: 401 Unauthorized」）。改为**只安装一次**监听器、
  令牌按请求 origin 查表注入；重启某个窗口的 Core 也不会再影响其它窗口；
- **启动前 ask 技能审批的事件负载缺字段**：`approval:request` 缺必填 `rememberable`、
  `approval:resolved` 缺 `by`/`action`——非 strict 下只刷错误日志，strict 事件模式
  （CI / 生产 fail-fast）会直接抛异常中断任务；
- 审批待办同步（`GET /api/approvals/pending`）补齐 `rememberable`：SSE 断线重连后补卡
  不再丢失「⚡ 记住并允许同类」按钮；
- 删除 `/api/tasks/{id}/events` 流收尾中重复的 `unsubscribe + cleanup` 代码块。

### 安全
- Core 令牌只注入到已登记的 Core origin：此前默认 session 的**所有**请求都会被贴上令牌，
  包括渲染进程对 GitHub 等外部地址的请求（插件图标），存在令牌外泄风险。

### 内部
- 新增 `electron/token-injector.js`：令牌注入器（按 origin 查表）含 7 个 `node:test` 用例，
  已接入 CI（frontend job）；
- 新增审批负载 strict 校验回归用例（`tests/test_server.py`）。

## [1.7.0] — Agent 权限档位与职责域重构

> 补记：该版本发布时未写入本文件，以下内容依据 `v1.6.2..v1.7.0` 的提交记录整理。

### 新增
- **Agent 权限档位 UI + 职责域权限模型重构**：工具按职责域分组授权（`read` / `plan` / `edit` /
  `execute` / `git_write` / `web` / `office` / `collab` / `interactive` / `misc`，各域可取
  allow / ask / deny）；移除 `spawn_sub_agent`，改为 `@` 主 Agent 派生；Plan 走 `plan_save`
  专用写通道；四个内置 Agent（build / plan / office / research）提示词重写；
- Agent 图标跟随对话气泡，支持自定义 Agent 图标；
- 快捷键：`Alt+1/2/3/4` 直达切换 primary Agent（Tab 循环保留）；
- 输入框长文本粘贴自动折叠为「粘贴块」；
- 引入 mypy 类型检查并接入 CI。

### 修复
- 审批卡断线丢失、新增「记住并允许同类」（会话级自动同意）、死循环检测分级；
- 嵌套事件载荷（TypedDict）校验误判；
- 审批卡超长内容溢出、Agent 配置页三层嵌套滚动、「运行中 / 与 @ 面板」；
- 图表渲染失败残留空 `diagrams` 目录。

### 变更
- 缓存命中率与「缓存帮你省下」改为会话累计口径（与累计成本一致）；
- 删除已无引用的旧同步工具 `litework/tools/sub_agent.py`；
- 新增「执行方式指南」文档，README 精简为快速开始。

## [1.6.1] — 审批卡死与上下文完整性修复

### 修复
- **「点了允许但 Agent 卡住」**：LLM 流式请求新增空闲看门狗（`llm_idle_timeout`，
  默认 120s）——连接卡死不再静默挂起 300s×3，而是可见中断并走重试（llm:retry 事件）；
- **审批卡永不关闭**：`/api/approve` 现在直接广播 `approval:resolved`（此前依赖
  等待审批的协程恢复后自己发事件，任务卡死/取消时永远发不出，卡片一直挂着）；
- **审批 Future 跨线程安全**：`ApprovalGate.resolve` 改用 `call_soon_threadsafe` 唤醒，
  并为审批请求/放行补齐 INFO 日志（此前排查无迹可寻）；
- **悬空 tool_calls 污染历史**：任务被取消/停止落盘前自动补 `[Interrupted]` 占位结果
  （`patch_dangling_tool_calls`），加载历史时顺手治愈旧损伤——不再触发 repair
  整条丢弃（丢上下文 + 反复击穿 prompt cache 拖慢每次调用）。

## [1.6.0] — SoL-Pi 效率机制

### 新增
- **SoL-Pi 效率机制四件套**：
  - 观察打包：大工具结果归档落盘，上下文只留句柄，`obs_recall` 分页回读；
  - 压缩经济学：摘要压缩前计算回本请求数（一次性写入成本 + 缓存债 vs 每请求节省），划算才压；
  - 动作融合：合并模型啰嗦的多步操作，减少无效轮次；
  - 证据收据：长工具结果压成结论性摘要再进上下文；
- 上下文面板重设计：仪表 + 账单卡 + 平铺明细；
- TODOs 面板：渐变进度条 + 平铺排序 + 收尾态 + 悬停元信息；
- 聊天区折叠阈值可配置，新增消息数维度（修复高工具密度会话不折叠）。

### 修复
- 上下文统计首轮双计（调用前估算与真实 usage 不再叠加）；
- 子 Agent 输出贴底跟随；打包版启动优化（matplotlib 字体缓存持久化）；
- 产出物/素材改为工作区内可见目录。

## [1.5.5] — 成本与模型元数据

- 成本统计口径与定价修正（修复「才用几下就 1M token」）；
- models.dev 同步可重试/可手动刷新，配置迁移落盘；
- DeepSeek 现行模型名；模型下拉去重；
- review_code 误报治理（TS 解析告警降级/去重/限流）。

## [1.5.4] — 前端性能

- buildTurns 增量缓存：长会话追加消息不再全量重渲染；
- 超长会话（1000 turn）折叠占位条 + 逐级展开。

## [1.5.3]

- 聊天流式贴底重构；MCP 工具自动注册。

## [1.5.2]

- 稳定性修复版本。

## [1.5.1]

- 子 Agent 的 todo_write 合并进主会话看板。

## [1.5.0] — 协作模式与许可

- 多 Agent 评审决议落地；协作模式全插件化
  （头脑风暴/辩论/会议/流水线/互批/编排/测试接力）；
- 新增 `delete_file` 工具；macOS 窗口生命周期修复；
- 许可证变更为 Apache-2.0。
