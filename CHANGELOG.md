# 更新日志

所有显著变更记录在此。格式参考 [Keep a Changelog](https://keepachangelog.com/)。

## [未发布]

### 修复
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
