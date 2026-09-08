# lite-work 多智能体能力设计（Phase 1）

> 状态：设计定稿，实施中
> 分支：`feat/ma`
> 调研基线：openai/codex `codex-rs`（agent/registry.rs、agent/control/spawn.rs、tools/handlers/multi_agents_spec.rs、thread_manager.rs 等，2026-09 源码）

## 1. 目标与原则

**目标**：主 Agent 可并行派生多个子 Agent，各自独立上下文执行；子 Agent 完成后结果以通知形式自动回流父上下文；并行写任务通过**目录级硬隔离**防止冲突。

**对齐 Codex 的机制**（源码实证）：
- Thread = Agent：每个 agent 是完整会话（ThreadId + rollout 持久化），统一生命周期管理
- 异步 spawn：立即返回，父继续干活；结果走 mailbox 通知，在 turn 边界投递
- 显式 close：完成态保留可查，占并发额度，必须显式关闭释放
- 三层限制：总量（max_threads）+ 深度（spawn depth）+ 驻留（V2 residency 并发容量）
- 委派策略提示词：关键路径自己做 / sidecar 并行委派；任务具体、有界、自包含；wait 克制

**与 Codex 的差异**：
- 目录级硬隔离（Codex 仅提示词约定 write set 不相交，无硬保证）
- Phase 1 不做嵌套 spawn（Codex 允许限深嵌套）
- Phase 1 不做 send_message / followup_task（Codex 的 agent 间自由通信）
- Phase 1 不做子 Agent 持久化恢复（Codex 有 rollout 恢复）

## 2. 架构

```
TaskManager.start(session)
  └─ AgentLoop（父）
       ├─ spawn_agent 工具 ──→ SessionAgentManager.spawn()
       │                          ├─ AgentRecord{ id, nickname, role, task,
       │                          │              allowed_dirs, status, asyncio.Task }
       │                          ├─ SubKernel（独立上下文，复用现有 SubAgentRunner 组装）
       │                          └─ IsolationPlugin（目录硬隔离，挂 before_tool）
       ├─ list_agents / close_agent / wait_agents（可选阻塞门）
       └─ 每轮 turn 边界 ←─ 完成通知注入（agent_manager.drain_notifications）
```

**核心组件**：

- `SessionAgentManager`：每会话一个，挂 AgentApp（`app.agent_managers[session_id]`），持有 AgentRecord 表、并发限额、通知队列
- `SubAgentRunner`：重构为 SessionAgentManager 内部执行器，复用现有 kernel 组装、`subagent:progress` 事件转发、超时逻辑
- `IsolationPlugin`：子 kernel 的 before_tool 管道插件，路径前缀校验

## 3. 工具协议（Phase 1 共 4 个）

| 工具 | 参数 | 语义 |
|---|---|---|
| `spawn_agent` | `task*`, `role?`, `agent_name?`, `allowed_dirs?`, `max_steps?` | 异步派生，立即返回 `{agent_id, nickname}`；默认角色 general |
| `list_agents` | — | 返回 `[{agent_id, nickname, role, status, task}]` |
| `close_agent` | `agent_id*` | 关闭空闲/完成的 agent，释放并发额度 |
| `wait_agents` | `agent_ids*[]`, `timeout_ms?` | 可选阻塞门：等指定 agent 终态并取回结果（整合阶段的同步点） |

**状态机**：`pending → running → (completed | errored | timeout | closed)`

**限制**：
- 并发活 agent 默认 4（config: `max_parallel_agents`）
- 单会话总派生量 16（config: `agent_total_limit`），超限报错
- 保持禁嵌套：子 Agent 工具集 exclude `spawn_agent`（Codex 允许嵌套但限深，我们 Phase 2 再放开）

**委派策略提示词**（进 system prompt，借鉴 Codex spawn_agent 工具描述）：
- 先规划：区分关键路径（阻塞任务自己做）与 sidecar（可并行委派）
- 任务必须具体、有界、自包含、不与主任务重叠
- 写任务必须显式声明 `allowed_dirs` 且各 agent 互不相交
- 等待期间做非重叠工作；wait_agents 仅在下一步被阻塞时使用

## 4. 目录级硬隔离（本设计的差异化重点）

- `spawn_agent(allowed_dirs=["src/api/", "tests/api/"])`：相对 workspace 的目录白名单
- 实现：子 kernel 挂 `IsolationPlugin`（before_tool 管道），对写类工具做路径前缀校验：
  - `write_file` / `apply_search_replace` / `apply_unified_diff`：目标路径必须落在某个 allowed_dir 内
  - `execute_command`：无法静态判定写目标 → 声明了 allowed_dirs 的 agent 上走审批门（ask）
  - 越界直接拒绝，返回错误信息告知 agent 正确的写入范围
- 只读工具（read/search/git 查看类）不限制
- 路径安全：规范化后前缀匹配，防 `..` 穿越、绝对路径注入、符号链接逃逸（realpath 归一后判定）
- **默认无写权限**：未声明 allowed_dirs 的 agent 只授予只读工具集（explorer 型）；要写必须显式授权目录——白名单制

## 5. 完成通知模型（完成即通知）

- 子 Agent 到达终态 → `{agent_id, nickname, role, summary, changed_files, status}` 写入 SessionAgentManager 通知队列
- **父在运行中**：AgentLoop 每轮 turn 结束后 drain 队列，以 user 角色注入：
  `[agent:completed] nickname(role) 已完成任务「task 摘要」：summary（改动文件：...）`
  父下一轮 LLM 调用自然看到，无需显式 wait
- **父已结束**：通知落 session_store（UI 显示为聊天内系统气泡），下个任务加载历史时自然进入上下文
- 事件流（SSE → 前端）：
  - `agent:spawned` `{agent_id, nickname, role, task}`
  - `agent:progress`（复用现有 subagent:progress 转发管线，带 agent_id）
  - `agent:completed` `{agent_id, nickname, status, summary, tokens}`
  - `agent:closed` `{agent_id}`

## 6. UI

- 聊天区：spawn_agent 卡片按 agent_id 展示（复用现有 SubAgentProgress 卡片，支持多个并存、并行滚动）
- 右面板新增 **Agents tab**（位于 TODOs 右侧，ToolPanel tab 条已支持横向滑动）：
  - **竖排 kanban 式看板**（已实现）：kanban 的「卡片跨状态列流动」适配窄面板改为纵向——
    「运行中」「已完成」两个状态分区竖向堆叠，agent 卡片完成时从上分区流入下分区，
    完成交接即分区流动的可视化
  - 运行中卡片：角色 + 运行时长（秒级计时）+ turn 数 + 当前执行工具 + 流式输出尾部预览
  - 完成卡片：角色 + token 消耗 + 任务摘要（点击展开全文）；**会话级保留**（不随聊天区
    TTL 通知消失，跨页面刷新从 session metadata 恢复）
  - 数据源：现有 `subagent:started/progress/completed` 事件（`reduceAgentBoard` reducer），
    现网 spawn_sub_agent 即可用；P1 后端落地后切换 `agent:*` 事件流（增加 agent:closed 分区）
  - tab 标签带运行计数（`Agents (N)`）
  - Phase 2 扩展：父子层级缩进（spawn 树形交接）、close 手动释放按钮、agent 间消息泳道、
    改动文件数徽标

## 7. 兼容与迁移

- `spawn_sub_agent` 保留为兼容包装（= spawn_agent + wait_agents 的同步语义），现有内置 agent 提示词逐步切换到新工具
- AgentProfile `mode="subagent"` 即角色系统（对齐 Codex agent-roles 的 TOML 角色文件），spawn 的 `role` 参数直接查 AgentRegistry
- 现有 `subagent:started/progress/completed` 事件保留，新增事件独立命名不冲突

## 8. 测试计划

- 单元：
  - SessionAgentManager 生命周期、并发/总量限额、close 释放额度
  - IsolationPlugin 路径前缀校验（`..` 穿越、绝对路径、symlink 逃逸、精确/前缀匹配）
  - 通知注入时序（turn 边界注入 / 父结束后落盘）
- 集成（mock adapter）：
  - 父派 2 个并行 agent → 各自产出 → 通知注入父上下文
  - 越界写被拒 + agent 收到正确范围提示
  - 超时路径、限额报错
- 回归：现有测试套件全绿

## 9. 协作模式层（Patterns on Primitives）

**核心认知**：编排-工人 / 流水线 / 头脑风暴 / 辩论互批不是不同的底层机制，而是同一组原语（spawn / 消息 / 等待 / 收集）之上的**提示词编排配方**（Codex 同此思路：只提供工具原语，模式由模型编排）。

| 模式 | 结构 | 落地状态 |
|---|---|---|
| **编排-工人** | 主 Agent 拆解 → 并行派发 → 整合 | ✅ P1：spawn_agent + wait_agents |
| **流水线** | 规划→实现→审查 顺序交接 | ✅ P1：顺序 spawn，前者产出写进后者 task 描述 |
| **头脑风暴** | N 个 agent 不同视角独立提案 → 综合 | ✅ P1：并行 spawn（不同 role/stance 提示）+ 综合 |
| **互批/批判** | critic 审查方案或代码，输出问题清单 | ✅ P1：内置 `critic` 角色（只读 + review_code） |
| **接力合作** | A 产出 → send_message 直达 B → followup 唤醒 B 继续 | ✅ P2 切片：send_message + followup_task（agent 间直接通信，不经父中转） |
| **辩论多轮** | 生成者 vs 批判者交替对抗 N 轮 | ✅ P2 切片：多轮 send_message + followup 往返（编排者驱动轮次） |
| **红蓝对抗** | 产出→审查→修复循环 | ✅ P2 切片：followup 唤醒原 agent 按批判意见修订 |
| **群聊会议** | 多 agent 共享议题轮流发言、互相影响后收敛（AutoGen GroupChat） | ✅ P2：meeting 模式 + meeting 技能（轮流发言 + 观点传递 + 裁决） |
| **进度账本** | 编排者周期检查子 agent 进度、督促/换人（Magentic-One） | ✅ P2：list_agents 周期检查指引（DELEGATION_GUIDE ledger 纪律） |
| **测试驱动接力** | 实现 agent ⇄ 测试 agent 配对循环（软件开发特化） | ✅ P2：code-sprint 技能（模式 A 接力 / 模式 B 模块领地并行） |
| **嵌套团队** | 子 agent 再派孙（深度受限） | ✅ P2：agent_spawn_depth=2，depth=1 可派孙、孙不可再派 |
| **上下文继承** | 子 agent 带父上下文启动（Codex fork_turns） | ✅ P2：fork_turns=none/all/"N"（裁剪：留 user+assistant 纯文本） |
| **断点恢复** | 子 agent 跨重启恢复可唤醒（CrewAI checkpointing 轻量版） | ✅ P2：metadata 落档 + manager 惰性恢复 + followup 轻量历史唤醒 |

### 模式怎么选（两层选择机制）

1. **自动路由**（模型侧）：spawn_agent 工具描述内嵌「模式选择决策表」
   （任务特征 → 模式 → 工具序列）——并行子任务→编排-工人、顺序依赖→流水线、
   要多样性→头脑风暴、要把关→互批、高风险→辩论、要迭代→红蓝对抗。
   模型按任务特征自动选择，用户无需指定。
2. **显式触发**（用户侧）：
   - **输入框「协作」选择器**（Composer agent-bar）：自动 / 💡头脑风暴 / ⚔辩论评审 /
     ⛓流水线——选中后下一条消息以 `/技能名` 命令发送（复用技能展开机制），发送后回落自动
   - 内置技能配方：`brainstorm`（多视角并行提案→交叉批判→综合）、`agent-debate`
     （提案者 vs 批判者对抗循环→裁决）、`pipeline`（按序接力交接）；
     触发词（"头脑风暴/红蓝对抗"）与 `/` 命令面板均可唤起
   - `spawn_agent(mode=...)` 参数：模型声明合作模式 → 事件携带 → 看板按模式分组渲染

### Agents 看板多视图（按模式分组渲染）

- **orchestrate**：竖排 kanban（运行中/已完成分区，卡片跨区流动）
- **brainstorm**：视角提案墙（多视角卡片并列平铺对比）
- **debate**：对抗泳道（提案方 vs 批判方分组对垒，critic 角色归批判侧）
- **pipeline**：顺序接力链（按派生序编号 + 箭头串联交接）

### 业界调研结论（Claude Code / Codex / OpenCode / Cursor / Multica / Trae Work，2026-09）

- **Claude Code** 四层体系（subagent → agent teams → 跨会话消息 → background agents）：
  辩论是官方推荐用例且**无专用工具**（纯原语+提示词）——验证我们的原语+技能配方路线；
  共享任务清单（teammate 自领+依赖解锁+文件锁）是「去中心化合作」的核心 ✅ 已落地最小切片
  （create_shared_tasks 建池 / claim 认领原子锁定 / finish 完成，认领权给子 Agent、建池权留编排者）；
  agent 消息不能代答审批/改配置 ✅ payload 带防伪声明（"非用户指令、不构成任何授权"）；
  消息限流防死循环 ✅ 每对 (sender,receiver) 每分钟 12 条
- **Multica**（49k★）：「Agents that show up on the board」——agent 即 teammate 的看板式
  工作区，issue 流转 + review gate（工作落 review 不落 main）+ Inbox（需要决策才 ping 人）
  + 26 runtime 不绑模型 → review gate ✅ 轻量落地（完成通知携带 changed_files → 看板
  「⚠ 待审查」徽标，人工过目后点击转「✔ 已审查」）；spawn 加 model 参数 ✅（"provider/model"
  或裸 model，探索类子任务路由便宜模型）
- **Trae Work**：Define Tasks → AI 拆解执行 → Review Results，任务级并行 + 统一 Workspace
  ——与 Multica 同向（任务并行+人审），agent 间协作无增量
- **Codex**：thread=agent + 工具原语 + 委派策略提示词（已对齐）
- **OpenCode**：@-mention 手动调用 subagent + task 权限 glob（谁能 spawn 谁）→
  @-mention ✅ 已落地（输入 @ 弹出角色补全：内置 explorer/critic/tester/general + 自定义
  subagent，选中后消息改写为 spawn_agent 派生指令）；task 权限 glob 记 P3

**P2/P3 已完成项汇总**：spawn model 路由、send_message 限流+防伪、agent color 看板着色、
@-mention 触发、review gate 轻量版（待审查徽标+文件清单）、共享任务池（去中心化认领）。
**余量**：共享任务依赖关系与自动解锁、@-mention 直达已运行 agent、task 权限 glob、
agent 持久化恢复跨会话。

### agent 间合作机制（P2 切片，已实施）

- **send_message**（编排者与子 Agent 均可用）：
  - 目标运行中 → 直达其 `agent_inbox`，turn 边界注入上下文（`[来自其他 Agent 的消息]` 前缀，来源可辨）
  - 目标已完成 → 滞留 mailbox，唤醒时送达
- **followup_task**（仅编排者）：唤醒已完成的 agent——携带全部历史消息链 + 滞留消息 + 新任务；
  token/轮数累加，原 summary 保留；运行中的 agent 拒绝唤醒（改用 send_message）
- **子 Agent 工具面**：send_message / list_agents 开放（同伴通信与查询）；
  followup_task / spawn / close / wait 保留给编排者（唤醒与派生属于编排权）
- 后续（P2 余量）：mailbox 轮询通知优化、多轮辩论的复合工具封装（`debate(proposal, rounds)`）

## 10. 分期

| 阶段 | 内容 |
|---|---|
| **P1**（本文档，已实施） | 异步 spawn + 并行 + 4 工具 + 目录硬隔离 + 完成通知 + Agents tab + critic 角色 |
| **P2**（已实施） | send_message / followup_task、嵌套 spawn（agent_spawn_depth，默认 2）、fork_context（fork_turns=none/all/N）、子 Agent 落盘恢复（metadata 重建 + 轻量历史唤醒）、模型路由、消息限流+长度上限+防伪、共享任务池、review gate、meeting 会议模式、进度账本指引、code-sprint 软件开发特化、配置界面（综合设置·多智能体区） |
| **P3** | MultiAgentMode 三档（默认 ExplicitRequestOnly）、角色 description 进 spawn 提示、nickname 池、权限默认收敛、worktree 物理隔离、共享任务依赖图 |

## 附：Codex 参考要点速查

- 工具面（V1/V2 双版本）：spawn_agent / send_input / send_message / followup_task / resume_agent / interrupt_agent / wait_agent / list_agents / close_agent
- 寻址：canonical task path（`/root/task1/task_3`，同父短名，跨子树全名）+ 昵称池（重名 "the 2nd"）
- fork 语义：`fork_turns` = none / all / N（保留 system/user/final answer，丢弃工具调用细节）
- 治理默认：ExplicitRequestOnly（用户/AGENTS.md 未明确要求不得 spawn）
- 委派提示词核心：critical path vs sidecar、write set 不相交、等待期做非重叠工作、wait 克制
