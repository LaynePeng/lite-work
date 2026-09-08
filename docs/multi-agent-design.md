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

## 9. 分期

| 阶段 | 内容 |
|---|---|
| **P1**（本文档） | 异步 spawn + 并行 + 4 工具 + 目录硬隔离 + 完成通知 + Agents tab |
| **P2** | send_message / followup_task、嵌套 spawn（depth=2）、fork_context（继承父最近 N 轮）、子 Agent 落盘恢复 |
| **P3** | MultiAgentMode 三档（默认 ExplicitRequestOnly）、角色 description 进 spawn 提示、nickname 池、权限默认收敛 |

## 附：Codex 参考要点速查

- 工具面（V1/V2 双版本）：spawn_agent / send_input / send_message / followup_task / resume_agent / interrupt_agent / wait_agent / list_agents / close_agent
- 寻址：canonical task path（`/root/task1/task_3`，同父短名，跨子树全名）+ 昵称池（重名 "the 2nd"）
- fork 语义：`fork_turns` = none / all / N（保留 system/user/final answer，丢弃工具调用细节）
- 治理默认：ExplicitRequestOnly（用户/AGENTS.md 未明确要求不得 spawn）
- 委派提示词核心：critical path vs sidecar、write set 不相交、等待期做非重叠工作、wait 克制
