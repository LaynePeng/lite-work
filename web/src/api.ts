// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import type { AgentInfo, AppConfig, FilePreviewResponse, FileReadResponse, LLMConfig, LLMProviderMeta, MCPServerConfig, MCPStatus, MCPServerStatus, ServerStatus, SessionInfo, ToolDef, TreeResponse } from "./types";

const TIMEOUT = 15000;
// 联网类操作（社区清单拉取 / 插件·技能下载安装）走单独的长超时：
// 慢网下载 zipball 合法耗时远超 15s。后端已把网络调用移出事件循环并设了
// connect/read 超时兜底，这里给足余量，避免「后端还在下、前端先中断」。
const NETWORK_TIMEOUT = 120000;
// 手动压缩：同步的 LLM 摘要调用（head 最多 ~180K 字符），生成摘要耗时远超 15s，
// 用与联网类同级的余量，避免「后端还在摘要、前端先中断」。
const COMPACT_TIMEOUT = 180000;

/** 安装任务进度快照（/api/install/jobs/{id}）。 */
export interface InstallJobStatus {
  id: string;
  kind: "plugin" | "skill";
  steps: string[];
  status: "running" | "paused" | "cancelling" | "cancelled" | "done" | "error";
  step: number;
  message: string;
  files_done: number;
  files_total: number;
  bytes_done: number;
  bytes_total: number;
  percent: number;
  error: string;
  result?: unknown;
}

export interface InstallPluginPayload {
  source?: string;
  zip_base64?: string;
  name?: string;
  overwrite?: boolean;
  version?: string;
}

export interface InstallSkillPayload {
  source?: string;
  zip_base64?: string;
  scope?: string;
  name?: string;
  overwrite?: boolean;
}

/** 工具/技能指标聚合（GET /api/metrics/tools）。 */
export interface ToolMetricEntry {
  calls: number;
  errors: number;
  cancelled: number;
  success_rate: number;
  avg_ms: number | null;
}
export interface SkillMetricEntry {
  uses: number;
  auto: number;
  explicit: number;
  tools: Record<string, number>;
}
export interface ToolMetricsSummary {
  tools: Record<string, ToolMetricEntry>;
  skills: Record<string, SkillMetricEntry>;
  tool_count: number;
  skill_count: number;
  worst_tools: (ToolMetricEntry & { tool: string })[];
  events: number;
  path: string;
  enabled: boolean;
}

/** 把底层异常翻译成用户可读的中文提示（尤其 AbortError 的原始英文串）。 */
function friendlyError(err: unknown, timeoutMs: number): Error {
  if (err instanceof DOMException && err.name === "AbortError") {
    return new Error(`请求超时（${Math.round(timeoutMs / 1000)}s）：网络较慢或服务无响应，请检查网络后重试`);
  }
  if (err instanceof TypeError) {
    return new Error("网络连接失败：请检查网络后重试");
  }
  return err instanceof Error ? err : new Error(String(err));
}

async function req<T>(url: string, init?: RequestInit, timeoutMs = TIMEOUT): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
      ...init,
    });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const body = await res.json();
        detail = body.detail ?? JSON.stringify(body);
      } catch {}
      throw new Error(`${res.status} ${detail}`);
    }
    return res.json() as Promise<T>;
  } catch (err) {
    throw friendlyError(err, timeoutMs);
  } finally {
    clearTimeout(timer);
  }
}

export const api = {
  status: () => req<ServerStatus>("/api/status"),
  config: () => req<AppConfig>("/api/config"),
  updateConfig: (updates: Partial<AppConfig>) =>
    req<{ ok: boolean }>("/api/config", { method: "POST", body: JSON.stringify({ updates }) }),

  /** 通用插件 UI 协议：读插件设置项 schema 与当前值（secret 字段后端已打码） */
  pluginSettings: (name: string) =>
    req<{ name: string; schema: import("./types").PluginSettingSpec[]; values: Record<string, unknown> }>(
      `/api/plugins/${encodeURIComponent(name)}/settings`),
  /** 保存插件配置：复用 /api/config 的任意键透传（{"updates": {"<插件名>": {...}}}） */
  savePluginConfig: (name: string, values: Record<string, unknown>) =>
    req<{ ok: boolean }>("/api/config", {
      method: "POST",
      body: JSON.stringify({ updates: { [name]: values } }),
    }),
  /** 通用插件 UI 协议：读插件面板内容（Markdown） */
  pluginPanel: (name: string, panelId: string) =>
    req<{ name: string; panel: string; title: string; markdown: string }>(
      `/api/plugins/${encodeURIComponent(name)}/panel/${encodeURIComponent(panelId)}`),

  agents: () => req<AgentInfo[]>("/api/agents"),
  collabModes: () =>
    req<{ modes: import("./types").CollabMode[] }>("/api/collab/modes"),
  agentsTools: () => req<{ tools: { name: string; description: string }[]; agents: Record<string, import("./types").AgentInfo> }>("/api/agents/tools"),
  saveAgent: (profile: Record<string, unknown>) =>
    req<Record<string, unknown>>("/api/agents/save", { method: "POST", body: JSON.stringify({ profile }) }),
  deleteAgent: (id: string) =>
    req<{ ok: boolean }>(`/api/agents/${encodeURIComponent(id)}`, { method: "DELETE" }),

  sessions: (workspace?: string) => {
    const url = workspace ? `/api/sessions?workspace=${encodeURIComponent(workspace)}` : "/api/sessions";
    return req<SessionInfo[]>(url);
  },
  createSession: (name?: string) =>
    req<{ session_id: string }>("/api/sessions", { method: "POST", body: JSON.stringify(name ? { name } : {}) }),
  /** 派生会话：克隆源会话骨架（goal/协作模式/模型/隔离工作树/TODO 结构）到新会话。 */
  deriveSession: (sourceId: string, name?: string) =>
    req<{ session_id: string; workspace?: string; goal?: string | null; todos_copied: number; worktree: boolean }>(
      `/api/sessions/${encodeURIComponent(sourceId)}/derive`, {
        method: "POST", body: JSON.stringify(name ? { name } : {}),
      }),
  getSession: (id: string) => req<{ messages: import("./types").Msg[]; metadata?: Record<string, unknown> }>(`/api/sessions/${id}`),
  /** 任务卡重命名/置顶：字段缺省 = 不改；name 传空串 = 清除自定义名。 */
  patchSession: (id: string, patch: { name?: string; pinned?: boolean }) =>
    req<{ session_id: string; name: string | null; pinned: boolean }>(`/api/sessions/${id}`, {
      method: "PATCH", body: JSON.stringify(patch),
    }),
  sessionModel: (id: string) => req<import("./types").SessionModelResponse>(`/api/sessions/${id}/model`),
  setSessionModel: (id: string, model: import("./types").SessionModel | null) =>
    req<import("./types").SessionModelResponse>(`/api/sessions/${id}/model`, {
      method: "POST", body: JSON.stringify(model ?? {}),
    }),
  sessionAgents: (id: string) =>
    req<{ agents: import("./types").SubAgentStatus[] }>(`/api/sessions/${id}/agents`),
  setSessionGoal: (id: string, goal: string | null) =>
    req<{ ok: boolean; goal: string | null }>(`/api/sessions/${id}/goal`, {
      method: "POST", body: JSON.stringify({ goal }),
    }),
  /** 预置会话 TODO（新建任务向导「计划项」）：每行一个待办，状态全置 pending。 */
  seedTodos: (id: string, items: string[]) =>
    req<{ ok: boolean; seeded: number }>(`/api/sessions/${id}/todos`, {
      method: "POST", body: JSON.stringify({ items }),
    }),
  setSessionCollab: (id: string, mode: string | null) =>
    req<{ ok: boolean; mode: string | null }>(`/api/sessions/${id}/collab`, {
      method: "POST", body: JSON.stringify({ mode }),
    }),
  // 隔离工作树（/worktree）：任务在独立 worktree 执行，主工作区不受影响
  setSessionWorktree: (id: string, enabled: boolean) =>
    req<{ ok: boolean; enabled: boolean; worktree?: { branch?: string; path?: string } | null }>(
      `/api/sessions/${id}/worktree`, { method: "POST", body: JSON.stringify({ enabled }) }),
  worktreeStatus: (id: string) =>
    req<import("./types").WorktreeStatus>(`/api/sessions/${id}/worktree`),
  worktreeDiff: (id: string) =>
    req<{ diff: string }>(`/api/sessions/${id}/worktree/diff`),
  worktreeMerge: (id: string) =>
    req<{ ok: boolean; merged?: number; files?: string[]; conflicts?: string[] }>(
      `/api/sessions/${id}/worktree/merge`, { method: "POST" }),
  worktreeAbortMerge: () =>
    req<{ ok: boolean; reason?: string }>("/api/worktrees/abort-merge", { method: "POST" }),
  worktreeConflicts: () =>
    req<{ in_progress: boolean; conflicts: string[] }>("/api/worktrees/conflicts"),
  worktreeDiscard: (id: string) =>
    req<{ ok: boolean }>(`/api/sessions/${id}/worktree/discard`, { method: "POST" }),
  // 遗留 worktree：列出（含当前分支与落后状态）/ 清理（默认只清无改动的空壳）
  worktreeList: () =>
    req<{
      worktrees: (import("./types").WorktreeStatus & { name: string })[];
      main_branch?: string;
      main_head?: string;
    }>("/api/worktrees"),
  worktreeClean: (includeDirty = false, name?: string) =>
    req<{ removed: string[]; kept: string[] }>("/api/worktrees/clean", {
      method: "POST",
      body: JSON.stringify({ include_dirty: includeDirty, ...(name ? { name } : {}) }),
    }),
  deleteSession: (id: string) =>
    req<{ ok: boolean }>(`/api/sessions/${id}`, { method: "DELETE" }),
  deleteSessionsBatch: (ids: string[]) =>
    req<{ ok: boolean; deleted: number; failed: { id: string; error: string }[] }>("/api/sessions/delete-batch", {
      method: "POST", body: JSON.stringify({ ids }),
    }),
  cleanupSessions: () =>
    req<{ ok: boolean; deleted: number }>("/api/sessions/cleanup", { method: "DELETE" }),

  tools: (agentId?: string) => {
    const url = agentId ? `/api/tools?agent_id=${encodeURIComponent(agentId)}` : "/api/tools";
    return req<ToolDef[]>(url);
  },
  metricsTools: () => req<ToolMetricsSummary>("/api/metrics/tools"),
  workspaceTree: (path?: string) =>
    req<TreeResponse>(`/api/workspace/tree-json${path ? `?path=${encodeURIComponent(path)}` : ""}`),
  fsList: (path?: string, showHidden = false) =>
    req<{ path: string; parent: string | null; home: string; is_workspace: boolean; dirs: string[]; files: string[]; truncated: boolean; drives: string[] }>(
      `/api/fs/list?path=${encodeURIComponent(path ?? "")}${showHidden ? "&show_hidden=true" : ""}`
    ),
  readFile: (path: string) =>
    req<FileReadResponse>(`/api/fs/read?path=${encodeURIComponent(path)}`),
  setWorkspace: (path: string) =>
    req<{ ok: boolean; workspace: string }>("/api/workspace", {
      method: "POST", body: JSON.stringify({ path }),
    }),
  mcpStatus: () => req<MCPStatus>("/api/mcp"),
  updateMcpServers: (servers: Record<string, MCPServerConfig>) =>
    req<{ ok: boolean; servers: MCPServerStatus[] }>("/api/mcp", {
      method: "POST", body: JSON.stringify({ servers }),
    }),

  chat: (sessionId: string, prompt: string, agentId?: string, reasoningEffort?: string,
         mergeWorktree?: string) => {
    const body: Record<string, unknown> = { session_id: sessionId, prompt };
    // agent_id 始终发送：区分「显式选 build」与「未指定」，Agent 切换检测依赖它
    if (agentId) body.agent_id = agentId;
    if (reasoningEffort) body.reasoning_effort = reasoningEffort;
    // AI 合并任务：指定 worktree 名 → 后端在主工作区起任务执行 git merge + 解决冲突
    if (mergeWorktree) body.merge_worktree = mergeWorktree;
    return req<{ task_id: string; queued?: boolean }>("/api/chat", {
      method: "POST", body: JSON.stringify(body),
    });
  },
  stopTask: (taskId: string) =>
    req<{ ok: boolean }>(`/api/tasks/${taskId}/stop`, { method: "POST" }),
  backgroundTasks: () =>
    req<{ tasks: import("./types").BackgroundTaskInfo[] }>("/api/tasks/background"),
  killBackgroundTask: (taskId: string) =>
    req<{ ok: boolean }>(`/api/tasks/background/${encodeURIComponent(taskId)}/kill`, { method: "POST" }),
  /** Agents 看板手动取消子 Agent：取消 runner + 广播 agent:closed（by=user） */
  closeSubAgent: (sessionId: string, agentId: string) =>
    req<{ ok: boolean; agent_id: string; previous_status: string }>(
      `/api/sessions/${encodeURIComponent(sessionId)}/agents/${encodeURIComponent(agentId)}/close`,
      { method: "POST" }),
  approve: (approvalId: string, approved: boolean, opts?: { remember?: boolean; sessionId?: string | null }) =>
    req<{ ok: boolean; remembered?: { ok: boolean } | null }>("/api/approve", {
      method: "POST", body: JSON.stringify({
        approval_id: approvalId, approved,
        ...(opts?.remember ? { remember: true } : {}),
        ...(opts?.sessionId ? { session_id: opts.sessionId } : {}),
      }),
    }),
  pendingApprovals: () =>
    req<{ approvals: import("./types").PendingApprovalInfo[] }>("/api/approvals/pending"),
  answerQuestion: (questionId: string, answer: string) =>
    req<{ ok: boolean; answer: string }>("/api/question", {
      method: "POST", body: JSON.stringify({ question_id: questionId, answer }),
    }),

  llmProviders: () => req<LLMProviderMeta[]>("/api/llm/providers"),
  llmConfig: () => req<LLMConfig>("/api/llm/config"),
  updateLLMConfig: (active: string, providers: Record<string, Partial<import("./types").LLMProviderSettings>>) =>
    req<LLMConfig>("/api/llm/config", { method: "POST", body: JSON.stringify({ active, providers }) }),
  testLLM: (providerId: string, overrides?: Record<string, unknown>) =>
    req<{ ok: boolean; message: string; latency_ms: number }>("/api/llm/test", {
      method: "POST", body: JSON.stringify({ provider_id: providerId, overrides }),
    }),
  // 拉取供应商模型列表（长超时：外网 /models 接口可能较慢）
  llmModels: (providerId: string, overrides?: Record<string, unknown>) =>
    req<{ ok: boolean; models: string[]; message: string }>("/api/llm/models", {
      method: "POST", body: JSON.stringify({ provider_id: providerId, overrides }),
    }, 30000),

  // 定价数据源状态（models.dev + 定价插件各官方源）；同步为**逐源**调用，
  // 前端据此逐步呈现「同步中 → 成功/失败」（不自动联网）
  pricingStatus: () => req<import("./types").PricingStatus>("/api/model-meta"),
  syncPricing: (source: string) =>
    req<import("./types").PricingSourceStatus & { ok: boolean; error?: string; pricing: import("./types").ContextPricing }>(
      "/api/model-meta/refresh", { method: "POST", body: JSON.stringify({ source }) }
    ),

  contextStats: (sessionId: string) =>
    req<import("./types").ContextStats>(
      `/api/context/stats?session_id=${encodeURIComponent(sessionId)}`
    ),

  compact: (sessionId: string, focus: string) =>
    req<{
      ok: boolean;
      before_tokens: number;
      after_tokens: number;
      removed_tokens: number;
      turns_compacted: number;
      keep_turns: number;
      summary: string;
    }>("/api/compact", {
      method: "POST", body: JSON.stringify({ session_id: sessionId, focus }),
    }, COMPACT_TIMEOUT),

  getTodos: (sessionId: string) =>
    req<{ todos: import("./types").TodoItem[] }>(
      `/api/todos?session_id=${encodeURIComponent(sessionId)}`
    ),

  // ------------------------------------------------------------ Skills 管理与命令

  skills: () => req<{ skills: import("./types").SkillInfo[] }>("/api/skills"),
  readSkill: (name: string) =>
    req<{ name: string; content: string }>(`/api/skills/${encodeURIComponent(name)}`),
  createSkill: (name: string, description: string, scope: string) =>
    req<{ ok: boolean; name: string; path: string }>("/api/skills/create", {
      method: "POST", body: JSON.stringify({ name, description, scope }),
    }),
  updateSkill: (name: string, description: string, scope: string) =>
    req<{ ok: boolean }>(`/api/skills/${encodeURIComponent(name)}`, {
      method: "PUT", body: JSON.stringify({ description, scope }),
    }),
  deleteSkill: (name: string, scope: string) =>
    req<{ ok: boolean }>(`/api/skills/${encodeURIComponent(name)}?scope=${encodeURIComponent(scope)}`, {
      method: "DELETE",
    }),
  commands: () => req<{ commands: import("./types").CommandInfo[] }>("/api/commands"),

  // ------------------------------------------------------------ Plugins 管理

  plugins: () => req<{ plugins: import("./types").PluginInfo[] }>("/api/plugins"),
  pluginsBuiltin: () => req<{ plugins: import("./types").BuiltinPluginInfo[] }>("/api/plugins/builtin"),
  pluginsCommunity: (url?: string) =>
    req<import("./types").CommunityManifest>(
      `/api/plugins/community${url ? `?url=${encodeURIComponent(url)}` : ""}`, undefined, NETWORK_TIMEOUT),
  deletePlugin: (name: string) =>
    req<{ ok: boolean; name: string; path: string }>(`/api/plugins/${encodeURIComponent(name)}`, { method: "DELETE" }),

  // 后台安装任务（步骤 + 下载进度）：启动后返回 job_id，轮询 installJob 取进度
  startInstallPlugin: (payload: InstallPluginPayload) =>
    req<{ job_id: string }>("/api/install/plugin", { method: "POST", body: JSON.stringify(payload) }),
  startInstallSkill: (payload: InstallSkillPayload) =>
    req<{ job_id: string }>("/api/install/skill", { method: "POST", body: JSON.stringify(payload) }),
  installJob: (id: string) =>
    req<InstallJobStatus>(`/api/install/jobs/${encodeURIComponent(id)}`),
  // 安装任务控制：pause=暂停（断点保留） / resume=继续 / cancel=取消并清理下载缓存
  installJobControl: (id: string, action: "pause" | "resume" | "cancel") =>
    req<{ ok: boolean; status: string }>(
      `/api/install/jobs/${encodeURIComponent(id)}/${action}`, { method: "POST" }
    ),

  // ------------------------------------------------------------ 任务卡（Goals 视图）：W7 轨迹
  /** 会话轨迹列表（trajectory_enabled=false 时返回空列表 + enabled 标记） */
  trajectoryList: (sessionId: string) =>
    req<{
      session_id: string;
      trajectories: { task_id: string; path?: string; started_at?: number; events?: number }[];
      enabled: boolean;
    }>(`/api/trajectories/${encodeURIComponent(sessionId)}`),
  /** 单条轨迹事件（分页）+ 漏斗 findings */
  trajectoryEvents: (sessionId: string, taskId: string, offset = 0, limit = 200) =>
    req<{
      session_id: string; task_id: string;
      events: { type: string; [k: string]: unknown }[];
      count: number;
      findings: { kind: string; [k: string]: unknown }[];
    }>(`/api/trajectories/${encodeURIComponent(sessionId)}?task_id=${encodeURIComponent(taskId)}&offset=${offset}&limit=${limit}`),

  // ------------------------------------------------------------ 办公场景：文件上传 / 素材列表 / 文件下载（AGI 通用入口）

  uploadFile: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return req<{ path: string; name: string; size: number }>("/api/upload", {
      method: "POST",
      body: form,
      headers: {}, // 让浏览器自动设置 multipart boundary
    });
  },
  fileDownloadUrl: (path: string) =>
    `/api/files/download?path=${encodeURIComponent(path)}`,
  fileRawUrl: (path: string) =>
    `/api/files/raw?path=${encodeURIComponent(path)}`,
  /** 素材/ 文件列表（Composer 的 # 引用面板数据源；v2 结构：交付物走文件树，不再有收件箱） */
  uploads: () =>
    req<{ items: import("./types").UploadItem[]; total: number }>("/api/uploads"),
  filePreview: (path: string) =>
    req<FilePreviewResponse>(`/api/files/preview?path=${encodeURIComponent(path)}`),
  // 删除工作区文件/目录：目录必须显式 recursive（含内容一并删除，后端二次校验）
  deleteFile: (path: string, opts?: { recursive?: boolean }) =>
    req<{ ok: boolean; path: string; kind?: "file" | "dir" }>(
      `/api/files?path=${encodeURIComponent(path)}${opts?.recursive ? "&recursive=true" : ""}`,
      { method: "DELETE" }
    ),
  // 新建空文件 / 空目录（文件页签：目录右键「新建…」；parent 为工作区相对目录，"" = 根）
  createEntry: (parent: string, name: string, kind: "file" | "dir") =>
    req<{ ok: boolean; path: string; name: string; kind: string }>("/api/files/create", {
      method: "POST",
      body: JSON.stringify({ parent, name, kind }),
    }),
  // 批量删除工作区文件：单项失败不中断整批，失败项在 failed 里逐个返回
  deleteFilesBatch: (paths: string[]) =>
    req<{ ok: boolean; deleted: number; failed: { path: string; error: string }[] }>("/api/files/delete-batch", {
      method: "POST", body: JSON.stringify({ paths }),
    }),
  renameFile: (path: string, newName: string) =>
    req<{ ok: boolean; path: string; name: string }>("/api/files/rename", {
      method: "POST", body: JSON.stringify({ path, new_name: newName }),
    }),
  createProject: (parent: string, name: string, opts?: { git?: boolean; structure?: boolean; kind?: "code" | "project" }) =>
    req<{ ok: boolean; path: string; name: string; git_initialized: boolean; scaffolded?: boolean; kind?: string }>(
      "/api/projects/create",
      {
        method: "POST",
        body: JSON.stringify({
          parent, name,
          git: opts?.git ?? true,
          structure: opts?.structure,
          kind: opts?.kind,
        }),
      }
    ),
  setProjectKind: (path: string, kind: "code" | "project") =>
    req<{ ok: boolean; path: string; kind: string }>("/api/projects/recent/kind", {
      method: "POST", body: JSON.stringify({ path, kind }),
    }),
  recentProjects: () =>
    req<{ items: import("./types").RecentProject[] }>("/api/projects/recent"),
  openProject: (path: string) =>
    req<{ ok: boolean; workspace: string; kind: "code" | "project" }>("/api/projects/recent", {
      method: "POST", body: JSON.stringify({ path }),
    }),
  removeRecentProject: (path: string) =>
    req<{ ok: boolean }>(`/api/projects/recent?path=${encodeURIComponent(path)}`, {
      method: "DELETE",
    }),
  toggleProjectPin: (path: string) =>
    req<{ ok: boolean; path: string; pinned: boolean }>("/api/projects/pin", {
      method: "POST", body: JSON.stringify({ path }),
    }),
};
