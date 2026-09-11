# lite-work 架构文档

> 本文面向想理解 / 参与 lite-work 开发的工程师。所有描述均对应仓库当前代码，
> 路径以 `litework/` 为根。

## 总体形态

```text
┌─────────────────────────────────────────────────┐
│ Electron 桌面外壳（electron/）                    │
│  · 窗口生命周期 / 自动注入 Core Token / 真实终端   │
├─────────────────────────────────────────────────┤
│ React Web UI（web/）                             │
│  · 聊天流式渲染 / 工具卡片 / 审批 / 上下文仪表      │
│  · HTTP + SSE ↔ FastAPI                          │
├─────────────────────────────────────────────────┤
│ FastAPI 服务层（litework/server/）               │
│  · REST + SSE 路由 / 会话 / 文件树 / 产出物下载    │
├─────────────────────────────────────────────────┤
│ Python 内核（litework/core/）                    │
│  · AgentLoop / 事件总线 / 洋葱中间件 / Token 预算  │
├─────────────────────────────────────────────────┤
│ LLM 适配器（litework/llm/）                      │
│  · 手写 SSE 流式解析 / 多供应商 / models.dev 元数据 │
├─────────────────────────────────────────────────┤
│ 工具 & 扩展（litework/tools/ mcp/ skills/）      │
│  · 36 个内置工具 / MCP / Cordis 式插件 / 技能     │
└─────────────────────────────────────────────────┘
```

支持三种运行形态：**本地桌面**（Electron 拉起 Python Core）、**远程 Core**
（`client.json` 配置 coreUrl + Token，窗口直连）、**纯浏览器**
（`python -m litework serve` 后访问 Web UI）。

## 目录总览

| 目录 | 职责 |
|---|---|
| `core/` | 内核：事件总线、洋葱中间件、AgentLoop、上下文与 Token 管理、会话持久化 |
| `llm/` | LLM 多供应商适配器，手写 SSE 解析，不依赖 SDK |
| `tools/` | 内置工具实现 + 工具注册表 + 插件加载器 |
| `mcp/` | 手写 stdio MCP Client 与服务管理器 |
| `security/` | 三级风险模型、黑白名单热加载、Web 审批、技能权限 |
| `server/` | FastAPI 应用：REST + SSE，路由按资源拆分 |
| `orchestration/` | 子 Agent 编排：会话级 AgentManager、协作模式策略 |
| `builtin_plugins/` | 七种协作模式的内置插件（头脑风暴 / 辩论 / 会议 / 流水线…） |
| `skills/` | 技能包（SKILL.md 格式），`load_skill` 按需加载全文 |
| `web/` | React + Vite 前端（vitest 组件测试） |
| `electron/` | 桌面外壳（main / preload / loading） |

## 内核（core/）分层

内核刻意做得很小：**不依赖任何高层 Agent 框架**，四类原语组合出全部行为。

```text
Kernel（会话容器）
 ├── TypedEventBus        强类型事件总线（负载校验，strict 模式 fail-fast）
 ├── Pipeline × N         洋葱中间件（before_tool / after_tool 等切面）
 ├── services             register_service / get_service 依赖注入
 └── messages             Message 流水账（push_message）

AgentLoop（任务执行器，一次 run_task = 一个任务）
 ├── 组装上下文 → 调 LLM → 解析工具调用
 ├── 工具批次执行（可并行）→ 结果回填 → 下一轮
 ├── Token 预算：ContextManager 裁剪 + Truncator 截断 + 两阶段压缩
 └── SoL-Pi 效率机制：观察打包 / 压缩经济学 / 动作融合 / 证据收据
```

### 事件驱动

所有跨模块通信走 `TypedEventBus`（`core/events.py`）。每个事件都有
TypedDict 负载类型（`LLMStreamPayload`、`ToolBeforeExecutePayload`、
`ContextStatsPayload`…），设置 `LITEWORK_STRICT_EVENTS=1` 时负载校验
fail-fast，测试全量开启。UI 看到的一切都是事件：流式增量、工具卡片、
审批请求、上下文仪表数字、子 Agent 进度。

### 中间件切面

`Pipeline`（`core/pipeline.py`）是标准洋葱模型：`use()` 注册中间件，
`run(ctx, data)` 依次进入、逆序返回。工具执行前后、安全审批、子 Agent
目录隔离（IsolationPlugin）都以 before/after 中间件挂载，核心循环本身
不知道它们存在。

## 一次任务的完整链路

1. `server/` 收到聊天请求 → `TaskManager.start(session)` 创建该任务的 `AgentLoop`；
2. **组装上下文**：System Prompt（含技能索引 / 项目指令 AGENTS.md）+ 历史消息，
   `ContextManager.prune_messages` 按 Token 预算裁剪；
3. **调 LLM**（`llm/` 适配器，手写 SSE 流式解析），增量经事件推给 UI；
4. **解析工具调用** → 安全层（`security/guard.py` 三级风险 + 黑白名单）
   决定直接执行 / 走 Web 审批卡（HITL）；
5. **工具批次执行**：无依赖的调用并行跑；大结果经 `truncator` + 观察打包
   归档落盘，上下文只留占位符（`obs_recall` 可分页回读）；
6. **循环收敛**：直到无工具调用，产出最终回答，会话快照落盘
   （`session_store`，JSON 原子写）。

## Token 与上下文管理（SoL-Pi 机制）

上下文是 Agent 最贵的资源，内核用四层手段控制它：

| 机制 | 文件 | 一句话 |
|---|---|---|
| 观察打包 | `observation_pack.py` | 大工具结果落盘，上下文只放句柄，`obs_recall` 分页取回 |
| 输出截断 | `truncator.py` | 超限工具输出截断 + 全文写盘，过期自动清理 |
| 压缩经济学 | `compaction_economics.py` | 摘要压缩前先算账：一次性写入成本 + 缓存债 vs 每请求节省，回本才压 |
| 两阶段裁剪 | `context_manager.py` | Stage1 免费裁剪（旧工具结果换占位符），不够再走 LLM 摘要 |

配套：`token_counter.py` 估 token，`json_repair.py` 自愈模型输出的脏 JSON，
`state_tracker.py` 检测工具调用死循环，`agent_loop._inject_queued` 支持
工具批次内排队消息即时注入。

## 多智能体与协作模式

- **编排层**（`orchestration/agent_manager.py`）：主 Agent 用 `spawn_agent`
  异步派生子 Agent（独立上下文、独立 Kernel），完成结果在 turn 边界以通知
  注入父上下文。支持目录级硬隔离（`allowed_dirs` 白名单 + 路径前缀校验，
  未授权的 Agent 只有只读工具）、并发限额、状态机
  （pending → running → completed/errored/timeout/closed）。
- **协作模式**（`orchestration/collab_policy.py` + `builtin_plugins/collab-*`）：
  每种模式 = 队形 recipe + 三个 hook（agent_spawned / agent_complete /
  task_done）。头脑风暴、辩论、会议、流水线、互批、编排、测试接力全部是
  插件——`CollabModePlugin` 基类 + `register_collab_policy` 注册，用户可装
  第三方玩法。

## 扩展体系

| 扩展点 | 机制 |
|---|---|
| 工具插件 | `~/.lite-work/plugins/` 放 .py 即注册；可覆盖内置工具，semver 对比，卸载自动回退 |
| 协作模式插件 | 同上，`CollabModePlugin` 子类 |
| MCP | `mcp_servers` 配置 stdio server，工具以 `mcp_<服务>_<工具>` 注册，默认需审批 |
| 技能 | `skills/` 目录 SKILL.md，索引常驻 System Prompt，`load_skill` 按需全文加载 |
| 自定义 Agent | 描述 + 系统提示词 + 工具白名单，设置页可视化管理 |

## 安全模型

- **三级风险**（SAFE / MEDIUM / HIGH）：SAFE 直接放行，MEDIUM 记录，
  HIGH（写文件 / 执行命令 / 删除等）走 Web 审批卡；
- **动态黑白名单**：规则文件热加载，改完即生效；
- **MCP 外部工具**默认全部需确认，可在安全设置中逐个放行；
- **鉴权**：`serve` 自动生成随机 Token，经就绪标记下发给 Electron 注入，
  本地调试可显式 `--no-token`。

## 关键设计决策

1. **不用 LangChain 等框架**——内核 ~3k 行，每个行为可读可测（tests/ 45+ 文件）；
2. **一切皆事件**——UI 与内核完全解耦，`strict events` 让负载契约不腐化；
3. **上下文是稀缺资源**——所有落盘/句柄/压缩决策都为省 token 服务，且
   压缩本身要过经济学审查；
4. **提示词约定不如管道强制**——子 Agent 写权限用路径校验硬保证，而非靠模型自觉；
5. **扩展点全部插件化**——工具、协作模式、技能、MCP 同一套治理
  （安装 / 版本 / 回退 / 审批）。
