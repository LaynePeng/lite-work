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

/** 社区清单中的协作模式条目（kind === "collab"） */
export interface CommunityCollabEntry {
  name: string;
  version?: string;
  description?: string;
  path?: string;
  kind?: string;
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
}

/** 效率机制节省台账（本任务累计） */
export interface ContextMechanisms {
  /** 观察打包：占位符替换省下的 tokens */
  obs_saved_tokens: number;
  /** 被打包的结果条数 */
  obs_packed: number;
  /** 证据收据：压缩省下的 tokens */
  reducer_saved_tokens: number;
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
  | { type: "tool:before_execute"; data: { toolName: string; args: unknown; callId?: string } }
  | { type: "tool:after_execute"; data: { toolName: string; durationMs: number; status: string; result?: string; callId?: string } }
  | { type: "approval:request"; data: { id: string; action: string; reason: string; rememberable?: boolean } }
  | { type: "approval:resolved"; data: { id: string; approved: boolean; by?: string } }
  | { type: "task:start"; data: { session_id: string } }
  | { type: "task:done"; data: { content: string; stats: Stats } }
  | { type: "task:error"; data: { message: string } }
  | { type: "stats:update"; data: Stats }
  | { type: "context:stats"; data: ContextStats }
  | { type: "subagent:started"; data: { task: string; role: string; subagentId?: string; callId?: string | null } }
  | { type: "subagent:progress"; data: SubAgentProgressEvent }
  | { type: "subagent:completed"; data: SubAgentCompletedData }
  | { type: "skill:loaded"; data: { names: string[] } }
  | { type: "chat:queued"; data: { text: string; count: number } }
  | { type: "todo:updated"; data: { todos: TodoItem[] } }
  | { type: "question:request"; data: { id: string; question: string; options: string[] } }
  | { type: "question:resolved"; data: { id: string; answer: string } }
  | { type: "agent:closed"; data: { agentId: string } };

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

// 文件 diff 响应（/api/workspace/diff）
export interface FileDiffResponse {
  path: string;
  diff: string;
  additions: number;
  deletions: number;
}

// 产出物列表项（/api/outputs）
export interface OutputItem {
  name: string;
  path: string;
  source: "outputs" | "uploads";
  size: number;
  mtime: string;
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
  pendingQueue: string[];
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

export interface SkillImportResult {
  ok: boolean;
  name: string;
  path: string;
  scope: string;
  deps?: SkillDepsReport;
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
}

export interface BuiltinPluginInfo {
  name: string;
  description: string;
  tools: string[];
  version: string;
  builtin: true;
  /** 已被用户版（~/.lite-work/plugins/ 同名插件）覆盖 */
  overridden?: boolean;
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
