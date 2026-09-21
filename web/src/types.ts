// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

// 与后端对齐的类型定义

/** 协作模式（/api/collab/modes）：内置策略 + 已安装模式插件 */
export interface CollabMode {
  name: string;
  display_name: string;
  description: string;
  source: "builtin" | "plugin";
  version: string;
  /** 插件自带图标（icon.svg 等）；null 时前端用内建图标回退 */
  icon_url?: string | null;
}

export interface AgentInfo {
  id: string;
  mode: "primary" | "subagent";
  description: string;
  model: string | null;
  temperature: number | null;
  tools: string[] | null;
  permissions: Record<string, string>;
  hidden: boolean;
  /** 自定义图标（emoji；空 = 前端回退默认映射） */
  icon?: string;
  /** 职责域权限模型：{域: "allow"|"deny"|"ask"}；null/缺省 = 跟随后端默认 */
  domains?: Record<string, "allow" | "deny" | "ask"> | null;
  /** 高级微调：额外放行的未映射工具（MCP / 插件动态工具） */
  extra_tools?: string[];
}

export interface ToolCall {
  id: string;
  type: string;
  function: { name: string; arguments: string };
}

export interface Msg {
  role: "system" | "user" | "assistant" | "tool";
  content: string | null;
  name?: string;
  tool_calls?: ToolCall[];
  tool_call_id?: string;
  queued?: boolean; // 任务运行中提交、待注入的补充指令（仅前端标记）
  /** 产生该消息的 Agent id（后端 AgentLoop 打标；历史消息可能缺失） */
  agent?: string;
}

// Agent 维护的任务 TODO 清单（todo_write 工具全量覆盖）
export interface TodoItem {
  content: string;
  status: "pending" | "in_progress" | "completed";
  /** 最近一次状态/内容变化的时间戳（秒）；面板 hover 展示 */
  updated_at?: number;
}

export interface SessionInfo {
  session_id: string;
  created_at: number;
  updated_at: number;
  message_count: number;
  title: string;
  metadata: Record<string, unknown>;
}

/** 最近打开的项目（侧边栏「项目」页签） */
export interface RecentProject {
  path: string;
  name: string;
  /** code=代码仓库 / project=通用项目 */
  kind: "code" | "project";
  is_git: boolean;
  /** 已置顶（显示在最近列表最前，不受打开时间排序影响） */
  pinned: boolean;
  opened_at: string;
}

export interface ToolDef {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
}

// 目录树条目（/api/workspace/tree-json）
export interface TreeEntry {
  name: string;
  path: string;
  type: "dir" | "file";
  status?: string | null; // git 状态字母: A/M/D/U/R/C
  has_changes?: boolean; // 目录下是否包含改动
}

export interface TreeResponse {
  workspace: string;
  path: string;
  git: { branch: string | null; has_repo: boolean };
  entries: TreeEntry[];
}

export interface Stats {
  input_tokens: number;
  output_tokens: number;
  tool_calls: number;
  turns: number;
  blocked: number;
  cost_estimate: number;
  status: string;
}

export interface ServerStatus {
  version: string;
  workspace: string | null;
  model: string;
  base_url: string;
  api_key_configured: boolean;
  active_tasks: number;
  sessions_count: number;
  token_auth: boolean;
  active_provider?: string;
}

/** models.dev 元数据缓存状态（GET /api/model-meta） */
export interface ModelMetaStatus {
  /** 缓存存在且未过期；false 时上下文窗口走内置表、成本走回退定价 */
  cached: boolean;
  /** 已索引的「供应商/模型」条目数 */
  models: number;
  /** 缓存文件年龄（秒）；无缓存为 null */
  age_seconds: number | null;
  /** 无缓存或超过 TTL（前端提示「建议同步」，不强制） */
  stale?: boolean;
}

/** 定价插件里的单个官方数据源状态（同步步骤逐行呈现） */
export interface PricingSourceStatus {
  id: string;
  label: string;
  url: string;
  cached: boolean;
  models: number;
  age_seconds: number | null;
  stale: boolean;
  snapshot_date?: string | null;
  /** 同步中/刚完成的瞬时状态（仅前端维护） */
  pending?: boolean;
  error?: string | null;
  elapsed_ms?: number;
}

export interface PricingStatus {
  models_dev: ModelMetaStatus;
  /** 定价插件信息；未安装为 null */
  provider: { name: string; version: string; description: string; source: string } | null;
  sources: PricingSourceStatus[];
}

export interface SessionModel {
  provider: string;
  model: string;
}

export interface SessionModelResponse {
  override: SessionModel | null;
  effective: SessionModel;
}

export interface AppConfig {
  max_steps: number;
  token_budget: number;
  tool_timeout: number;
  llm_timeout?: number;
  subagent_timeout?: number;
  auto_approve: boolean;
  context_full_turns?: number;
  pricing: { input_per_mtok: number; output_per_mtok: number; cache_hit_per_mtok?: number };
  /** 技能权限规则：glob 模式 → allow/deny/ask */
  skill_permissions?: Record<string, "allow" | "deny" | "ask">;
  /** Skills zip 导入大小上限（MB） */
  max_zip_size_mb?: number;
  /** triggers 匹配模式：substring | advanced */
  skill_trigger_mode?: "substring" | "advanced";
  /** 证据收据 reducer：小模型名（空=停用）；provider 留空用当前默认供应商 */
  reducer_model?: string;
  reducer_provider?: string;
  /** 聊天区展示折叠阈值（轮数/消息数任一超限即折叠；数据不删，仅 UI 折叠） */
  chat_fold_turns?: number;
  chat_fold_messages?: number;
  // 多智能体配置（docs/multi-agent-design.md §3）
  max_parallel_agents?: number;
  agent_total_limit?: number;
  agent_max_steps?: number;
  agent_max_steps_cap?: number;
  agent_spawn_depth?: number;
  agent_message_max_chars?: number;
  agent_meeting_rounds?: number;
  agent_ledger_interval?: number;
  agent_persist_max?: number;
  /** 治理档位：explicit（默认，明确要求才派生）| proactive（主动并行委派） */
  agent_collab_mode?: "explicit" | "proactive";
  /** 协作模式（default/review/已安装模式插件的 mode_name）与自定义配方文本 */
  collab_policy?: string;
  collab_recipe?: string;
}

/** 最近一次 LLM 调用的用量（与「本任务累计」区分：每轮都会把整段上下文重发）。 */
export interface ContextCallStats {
  prompt_tokens: number;
  output_tokens: number;
  cache_hit_tokens: number;
  cache_miss_tokens: number;
  cost_estimate?: number;
}

export interface ContextTaskStats {
  /** 以下均为「本任务累计」（跨轮累加，≈ 轮数 × 单轮上下文） */
  prompt_tokens: number;
  output_tokens: number;
  cache_hit_tokens: number;
  cache_miss_tokens: number;
  cache_hit_rate: number | null;
  compression_count: number;
  compressed_tokens: number;
  usage_ratio: number | null;
  /** 当前上下文水位（最近一次调用实际发出的 prompt tokens） */
  last_prompt_tokens?: number;
  /** 本任务已执行的 LLM 轮数 */
  turns?: number;
  tool_calls?: number;
  blocked?: number;
  cost_estimate?: number;
  /** 最近一次调用的用量与成本 */
  last?: ContextCallStats;
}

/** 实际计费单价（每 M token，美元） */
export interface ContextPricing {
  input_per_mtok: number;
  output_per_mtok: number;
  cache_hit_per_mtok: number;
  /** 价格来源：official:deepseek / snapshot:kimi / models.dev / override / config */
  source?: string;
  /** 价格数据年龄（秒）；内置快照/回退价为 null */
  source_age_seconds?: number | null;
  /** 数据已过期（前端提示去设置页同步） */
  stale?: boolean;
  /** 分时供应商的空闲档（DeepSeek：空闲时段半价） */
  off_peak?: { input_per_mtok: number; output_per_mtok: number; cache_hit_per_mtok: number };
  /** 当前是否处于空闲时段（按计价时刻判断） */
  off_peak_active?: boolean;
}

/** 效率机制节省台账（本任务累计） */
export interface ContextMechanisms {
  /** 观察打包：占位符替换省下的 tokens */
  obs_saved_tokens: number;
  /** 被打包的结果条数 */
  obs_packed: number;
  /** 证据收据：压缩省下的 tokens */
  reducer_saved_tokens: number;
  /** 证据收据是否启用（opt-in：config.reducer_model 为空即停用） */
  reducer_enabled?: boolean;
  /** 最近一次压缩决策理由（economics/window_protection/deferred_economic…） */
  compaction_reason: string | null;
}

/** 上下文水位历史点（前端在每轮 context:stats 时追加，供趋势图用） */
export interface ContextHistoryPoint {
  /** 该轮实际发出的 prompt tokens（≈ 当前上下文水位） */
  p: number;
}

export interface ContextSessionStats {
  prompt_tokens: number;
  output_tokens: number;
  cache_hit_tokens: number;
  cache_miss_tokens: number;
  cache_hit_rate: number | null;
  compression_count: number;
  compressed_tokens: number;
  /** 最近一次压缩后的水位（手动压缩后写入，供面板 GET 刷新时还原环形） */
  last_prompt_tokens?: number;
  tool_calls?: number;
  blocked?: number;
  cost_estimate?: number;
}

export interface ContextStats {
  model: string;
  context_window: number;
  task?: ContextTaskStats;
  session: ContextSessionStats;
  /** 预估成本使用的单价（models.dev per-model 或配置回退价） */
  pricing?: ContextPricing;
  /** 效率机制节省台账（v1.6.0：观察打包 / 证据收据 / 压缩决策） */
  mechanisms?: ContextMechanisms;
}

export interface LLMProviderMeta {
  id: string;
  name: string;
  kind: "openai" | "anthropic";
  models: string[];
  default_base_url: string;
  has_key: boolean;
  model: string;
  /** 支持 reasoning_effort 的模型列表（provider_meta 返回） */
  reasoning_models?: string[];
  /** 当前选中模型是否支持推理控制 */
  reasoning_supported?: boolean;
}

export interface LLMProviderSettings {
  api_key: string;
  has_key: boolean;
  base_url: string;
  model: string;
  models: string[];
  name?: string;
  temperature: number;
  /** 推理强度控制：""（关闭）/ "low" / "medium" / "high" */
  reasoning_effort?: string;
  context_window?: number | null;
  custom_headers?: Record<string, string>;
}

export interface LLMConfig {
  active: string;
  providers: Record<string, LLMProviderSettings>;
}

/** SSE 连接状态：idle=无任务 / connecting=连接中 / connected=已连接（绿） /
 *  reconnecting=断线重连中（黄，原生或指数退避） / lost=重连耗尽（红，任务仍在后端） */
export type SseConnState = "idle" | "connecting" | "connected" | "reconnecting" | "lost";

export type SSEEvent =
  | { type: "message:added"; data: { message: Msg } }
  | { type: "llm:stream"; data: { chunk: string } }
  | { type: "llm:turn_start"; data: { turn: number } }
  | { type: "llm:retry"; data: { attempt: number; max_retries: number; reason: string; wait: number } }
  | { type: "tool:before_execute"; data: { toolName: string; args: unknown; callId?: string; timeoutMs?: number } }
  | { type: "tool:after_execute"; data: { toolName: string; durationMs: number; status: string; result?: string; callId?: string } }
  | { type: "approval:request"; data: { id: string; action: string; reason: string; rememberable?: boolean } }
  | { type: "approval:resolved"; data: { id: string; approved: boolean; by?: string } }
  | { type: "task:start"; data: { session_id: string } }
  | { type: "task:done"; data: { content: string; stats: Stats } }
  | { type: "task:error"; data: { message: string } }
  | { type: "stats:update"; data: Stats }
  | { type: "context:stats"; data: ContextStats }
  | { type: "worktree:merge"; data: {
      name: string; branch: string; phase: string;
      conflicts: string[]; files: number; commits: number; message: string;
    } }
  | { type: "subagent:started"; data: { task: string; role: string; subagentId?: string; callId?: string | null } }
  | { type: "subagent:progress"; data: SubAgentProgressEvent }
  | { type: "subagent:completed"; data: SubAgentCompletedData }
  | { type: "skill:loaded"; data: { names: string[] } }
  | { type: "chat:queued"; data: { text: string; count: number } }
  | { type: "todo:updated"; data: { todos: TodoItem[] } }
  | { type: "question:request"; data: { id: string; question: string; options: string[] } }
  | { type: "question:resolved"; data: { id: string; answer: string } }
  | { type: "agent:closed"; data: { agentId: string; by?: string } };

// 子 Agent 实时进度事件（命名空间转发自隔离 kernel）
export interface SubAgentProgressEvent {
  subagentId: string;
  role: string;
  callId: string | null;
  kind: "llm:turn_start" | "tool:before_execute" | "tool:after_execute" | "llm:stream";
  turn?: number;
  tool?: string;
  brief?: string;
  status?: string;
  durationMs?: number;
  /** llm:stream 时携带的文本块 */
  text?: string;
}

export interface SubAgentCompletedData {
  task: string;
  role: string;
  subagentId?: string;
  callId?: string | null;
  tokens_used?: number;
  turns?: number;
  summary?: string;
  /** 完成态："completed"（正常收敛）/ "errored"（LLM 失败、超时、步数耗尽等） */
  status?: "completed" | "errored";
  /** errored 时的错误摘要（completed 时为空/undefined） */
  error?: string | null;
}

// 子 Agent 活动卡片（嵌在 spawn_sub_agent 工具卡内渲染）
export interface SubAgentStep {
  tool: string;
  brief?: string;
  status: "running" | "done" | "error" | "cancelled";
  durationMs?: number;
}

export interface SubAgentProgress {
  subagentId: string;
  role: string;
  task: string;
  turn: number;
  steps: SubAgentStep[];
  status: "running" | "done" | "error";
  summary?: string;
  tokens?: number;
  /** 子 Agent 实时流式文本（llm:stream 累积） */
  streaming_text?: string;
  /** 派生时间戳（Agents 看板运行时长显示） */
  startedAt?: number;
  /** 合作模式（编排者声明，看板按模式分组渲染）：orchestrate/pipeline/brainstorm/debate */
  mode?: string;
  /** 改动文件清单（review gate：完成卡片「待审查」徽标数据源） */
  changedFiles?: string[];
}

/** 后端 list_agents 返回的子 Agent 状态（前端通过 API 同步，非 SSE 事件）。 */
export interface SubAgentStatus {
  agent_id: string;
  nickname: string;
  role: string;
  status: string;
  task: string;
  tokens: number;
  turns: number;
  changed_files: string[];
  summary: string;
}

// Electron 注入的原生能力（浏览器模式下不存在）
export interface LiteWorkBridge {
  platform: string;
  version: string;
  openProject: () => Promise<{ ok: boolean; url?: string; workspace?: string; error?: string }>;
  openProjectNewWindow: () => Promise<{ ok: boolean; url?: string; workspace?: string; error?: string }>;
  /** 重启当前窗口的本地 Core（插件变更后换取全新进程状态）；成功后页面整页刷新 */
  restartCore: () => Promise<{ ok: boolean; url?: string; error?: string }>;
  /** 用系统默认应用打开工作区内的文件（非代码文件） */
  openFile: (path: string) => Promise<{ ok: boolean; error?: string }>;
  /** 在系统文件管理器中定位（高亮显示）工作区内的文件 */
  showInFolder: (path: string) => Promise<{ ok: boolean; error?: string }>;
  /** 渲染进程通过 HTTP 切换工作区成功后，同步 Electron 主进程的窗口 workspace（终端 cwd 依赖它） */
  workspaceChanged: (workspace: string) => void;
  terminalStart: (cols?: number, rows?: number) => Promise<{ ok: boolean; error?: string }>;
  terminalInput: (data: string) => void;
  terminalResize: (cols: number, rows: number) => void;
  terminalStop: () => void;
  onTerminalData: (listener: (data: string) => void) => () => void;
  onTerminalExit: (listener: (code: number) => void) => () => void;
  onShowAbout: (listener: () => void) => () => void;
}

declare global {
  interface Window {
    liteWork?: LiteWorkBridge;
  }
}

// 显示模型：工具卡片
export interface ToolCardInfo {
  id: string;
  name: string;
  args: unknown;
  status: "running" | "done" | "cancelled" | "error";
  durationMs?: number;
  /** 工具开始执行的本地时间戳（ms）：运行中卡片显示实时已用时 */
  startedAt?: number;
  /** 后端下发的超时毫秒数：running 卡片据此设「疑似卡死」看门狗 */
  timeoutMs?: number;
  result?: string;
  callId?: string;
  subagent?: SubAgentProgress;
}

// 按 SSE / 会话消息原始顺序排列的工作时间线
export type WorkItem =
  | { type: "text"; id: string; content: string; agent?: string }
  | { type: "tool"; id: string; card: ToolCardInfo }
  | { type: "activity"; id: string; tools: ToolCardInfo[] };

// 文件读取响应（/api/fs/read）
export interface FileReadResponse {
  path: string;
  content: string;
  language: string;
  lines: number;
  size: number;
  diff: string;
}

// 产出物列表项（/api/outputs）
export interface OutputItem {
  name: string;
  path: string;
  source: "outputs" | "uploads";
  size: number;
  mtime: string;
  /** 类型分组名（产出物/ 下的一级子目录；根目录散文件为「未分类」） */
  category?: string;
  /** 扩展名（不含点）：docx / xlsx / png … */
  ext?: string;
  /** 版本号（来自文件名 `_vN`；无版本号为 0） */
  version?: number;
}

/** 产出物收件箱分组（同类型聚合，只含每项最新版本）。 */
export interface OutputGroup {
  name: string;
  source: "outputs" | "uploads";
  items: OutputItem[];
}

// 产出物预览响应（/api/files/preview）
export type FilePreviewResponse = {
  name: string;
  kind: "media";
  media_type: string;
  raw_url: string;
} | {
  name: string;
  kind: "table";
  sheet: string;
  sheets: string[];
  rows: string[][];
  truncated: boolean;
} | {
  name: string;
  kind: "text";
  text: string;
  truncated: boolean;
} | {
  name: string;
  kind: "slides";
  slides: { title: string; bullets: string[] }[];
};

// Tab 项：对话 或 文件
export interface TabItem {
  id: string;
  kind: "chat" | "file";
  title: string;
  // chat tab
  sessionId?: string;
  modelOverride?: SessionModel | null;
  // file tab
  filePath?: string;
  fileLanguage?: string;
  fileContent?: string;
  fileDiff?: string;
}

/** /api/approvals/pending 返回的单条挂起审批（SSE 重连/轮询兜底补卡用）。
 *
 * session_id：审批归属会话（子 Agent 的审批后端以 root_session_id=主会话上报，
 * 前端按当前活跃会话 id 过滤即可命中）。 */
export interface PendingApprovalInfo {
  id: string;
  action: string;
  reason: string;
  rememberable?: boolean;
  session_id?: string;
}

// 单个会话的独立状态
export interface ChatSessionState {
  messages: Msg[];
  streaming: { items: WorkItem[]; turn?: number } | null;
  running: boolean;
  turn: number;
  stats: Stats | null;
  contextStats: ContextStats | null;
  /** 每轮 context:stats 的水位轨迹（最近 60 点，供面板趋势图） */
  contextHistory: ContextHistoryPoint[];
  error: string | null;
  // 审批队列：并行工具可同时挂起多个审批请求
  pendingApprovals: { id: string; action: string; reason: string; rememberable?: boolean }[];
  // 任务完成后归档的子 Agent 活动卡（会话级内存态，刷新即失）
  subAgentRecords: SubAgentProgress[];
  /** Agents 看板（右面板 Agents tab）：竖排 kanban，运行中→已完成 卡片流动；会话级累积不随 TTL 清除 */
  agentBoard: SubAgentProgress[];
  stalled: boolean;
  /** SSE 连接状态：绿=已连接 / 黄=重连中 / 红=失联 */
  sseState?: SseConnState;
  modelOverride?: SessionModel | null;
  effectiveModel?: SessionModel;
  skillLoaded?: string[];
  // 任务 TODO 清单（todo_write 工具推送，任务结束后保留展示）
  todos: TodoItem[];
  /** 待回答的提问（ask_user 工具） */
  pendingQuestions?: { id: string; question: string; options: string[] }[];
  /** 当前会话的推理强度（""=关闭 / "low" / "medium" / "high" / "max"） */
  reasoningEffort?: string;
  /** 待发送队列：任务运行中追加的消息，任务完成后逐一发送（类似 Codex） */
  /** 会话目标（/goal 设置）：持久化于会话 metadata，注入每个任务 system prompt */
  goal?: string | null;
  /** 会话协作模式（对话框选择器）：持久化于会话 metadata，覆盖全局 collab_policy */
  collabMode?: string | null;
  /** 目标循环（/loop）：任务结束后自动续发推进指令，直至 [GOAL_COMPLETE] 或达上限 */
  loopEnabled?: boolean;
  loopMax?: number;
  loopCount?: number;
  /** 自动继续（/continue）：任务结束后 TODO 有未完成项 → 自动续推（上限 N 轮） */
  autoContinue?: boolean;
  autoContinueCount?: number;
  /** 任务结束时 TODO 仍有未完成项（未开 autoContinue 时展示「继续」按钮） */
  unfinished?: boolean;
  /** 隔离工作树（/worktree）：任务在独立 worktree 执行，主工作区不受影响 */
  worktreeEnabled?: boolean;
  /** worktree 变更状态（评审卡数据源；任务结束时轮询刷新） */
  worktreeStatus?: WorktreeStatus | null;
  /** 合并冲突文件列表（AI/手动合并后主工作区仍有冲突时展示） */
  worktreeConflicts?: string[];
  /** AI 合并进度（worktree:merge 事件；merged/failed 后短暂保留再清理） */
  worktreeMerge?: { phase: string; conflicts: string[]; files: number; commits: number; message: string } | null;
  pendingQueue: string[];
}

/** 隔离工作树状态（GET /api/sessions/{id}/worktree）。 */
export interface WorktreeStatus {
  exists: boolean;
  enabled?: boolean;
  branch?: string;
  path?: string;
  files?: { status: string; path: string }[];
  /** 尚未提交的改动（harvest 会自动提交到分支） */
  pending?: { status: string; path: string }[];
  adds?: number;
  dels?: number;
  /** 分支上已有的提交数（相对基点） */
  commits?: number;
  /** 合并目标：主工作区当前分支（worktree 的改动会 merge 到这里） */
  main_branch?: string;
  /** 主工作区当前 HEAD */
  main_head?: string;
  /** 本 worktree 分支的基点 */
  base?: string;
  /** 本 worktree 分支的 tip（画分支树用） */
  head?: string;
  /** 主分支是否已前进（其他会话合并过 → 本分支基于旧提交，合并可能冲突） */
  behind?: boolean;
}

// ---------------------------------------------------------------- 后台命令

export interface BackgroundTaskInfo {
  task_id: string;
  command: string;
  running: boolean;
  elapsed: number;
  exit_code: number | null;
}

// ---------------------------------------------------------------- Skills 管理与命令

export interface SkillInfo {
  name: string;
  description: string;
  dirName: string;
  path: string;
  scope: "workspace" | "user";
  writable: boolean;
  triggers: string;
  /** SKILL.md frontmatter 可选版本号（社区技能更新对比用，缺失为空） */
  version?: string;
  /** 权限动作：allow（默认）/ deny（对 Agent 隐藏）/ ask（使用前需确认） */
  permission?: "allow" | "deny" | "ask";
}

export interface SkillDepsReport {
  pip?: { ok?: boolean; stdout?: string; stderr?: string; error?: string } | null;
  npm?: { ok?: boolean; stdout?: string; stderr?: string; error?: string } | null;
  env?: { ok?: boolean; action?: string; error?: string } | null;
}

export interface CommandInfo {
  name: string;
  description: string;
  argsHint: string;
  kind: "builtin" | "skill";
}

// ---------------------------------------------------------------- MCP 配置

export interface MCPServerConfig {
  command: string;
  args?: string[];
  env?: Record<string, string>;
  enabled?: boolean;
}

export interface MCPServerStatus {
  name: string;
  command: string;
  args: string[];
  enabled: boolean;
  connected: boolean;
  error?: string | null;
  tools: string[];
}

export interface MCPStatus {
  servers: MCPServerStatus[];
}

// ---------------------------------------------------------------- Plugins 管理

export interface PluginInfo {
  name: string;
  path: string;
  is_dir: boolean;
  tools: string[];
  /** 插件声明移除的工具（内置或其他插件的） */
  removed_tools?: string[];
  description: string;
  version?: string;
  source?: string;
  /** 插件类别：collab=协作模式（进模式选择器，不注册工具）/ tool=工具插件 */
  kind?: "tool" | "collab";
  /** 加载失败原因（空/缺省=正常）；列表仍返回，前端显示"⚠ 加载失败"而非整页挂掉 */
  error?: string;
}

export interface BuiltinPluginInfo {
  name: string;
  description: string;
  tools: string[];
  version: string;
  builtin: true;
  /** 已被用户版（~/.lite-work/plugins/ 同名插件）覆盖 */
  overridden?: boolean;
  /** 本地版存在但版本落后，已被内置版旁路（可删除本地旧版） */
  stale_local?: boolean;
  /** stale_local 时本地旧版的版本号（清理提示展示用） */
  local_version?: string;
  /** 插件类别：collab=协作模式 / tool=工具插件 */
  kind?: "tool" | "collab";
}

export interface CommunityManifest {
  version: string;
  min_app_version: string;
  /** path 为插件目录（相对仓库根），如 "plugins/office-plugin"；kind=collab 为协作模式包，icon 为仓库内图标路径 */
  plugins: { name: string; version: string; description: string; path: string; tools?: string[]; kind?: string; icon?: string }[];
  skills: { name: string; version: string; description: string; path: string }[];
}
