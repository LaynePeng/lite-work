import type { AgentInfo, AppConfig, FileDiffResponse, FilePreviewResponse, FileReadResponse, LLMConfig, LLMProviderMeta, MCPServerConfig, MCPStatus, MCPServerStatus, OutputItem, ServerStatus, SessionInfo, ToolDef, TreeResponse } from "./types";

const TIMEOUT = 15000;

async function req<T>(url: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT);
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
  } finally {
    clearTimeout(timer);
  }
}

export const api = {
  status: () => req<ServerStatus>("/api/status"),
  config: () => req<AppConfig>("/api/config"),
  updateConfig: (updates: Partial<AppConfig>) =>
    req<{ ok: boolean }>("/api/config", { method: "POST", body: JSON.stringify({ updates }) }),

  agents: () => req<AgentInfo[]>("/api/agents"),
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
  getSession: (id: string) => req<{ messages: import("./types").Msg[]; metadata?: Record<string, unknown> }>(`/api/sessions/${id}`),
  sessionModel: (id: string) => req<import("./types").SessionModelResponse>(`/api/sessions/${id}/model`),
  setSessionModel: (id: string, model: import("./types").SessionModel | null) =>
    req<import("./types").SessionModelResponse>(`/api/sessions/${id}/model`, {
      method: "POST", body: JSON.stringify(model ?? {}),
    }),
  sessionAgents: (id: string) =>
    req<{ agents: import("./types").SubAgentStatus[] }>(`/api/sessions/${id}/agents`),
  deleteSession: (id: string) =>
    req<{ ok: boolean }>(`/api/sessions/${id}`, { method: "DELETE" }),
  cleanupSessions: () =>
    req<{ ok: boolean; deleted: number }>("/api/sessions/cleanup", { method: "DELETE" }),

  tools: (agentId?: string) => {
    const url = agentId ? `/api/tools?agent_id=${encodeURIComponent(agentId)}` : "/api/tools";
    return req<ToolDef[]>(url);
  },
  workspaceTree: (path?: string) =>
    req<TreeResponse>(`/api/workspace/tree-json${path ? `?path=${encodeURIComponent(path)}` : ""}`),
  fsList: (path?: string, showHidden = false) =>
    req<{ path: string; parent: string | null; home: string; is_workspace: boolean; dirs: string[]; files: string[]; truncated: boolean }>(
      `/api/fs/list?path=${encodeURIComponent(path ?? "")}${showHidden ? "&show_hidden=true" : ""}`
    ),
  readFile: (path: string) =>
    req<FileReadResponse>(`/api/fs/read?path=${encodeURIComponent(path)}`),
  fileDiff: (path: string) =>
    req<FileDiffResponse>(`/api/workspace/diff?path=${encodeURIComponent(path)}`),
  setWorkspace: (path: string) =>
    req<{ ok: boolean; workspace: string }>("/api/workspace", {
      method: "POST", body: JSON.stringify({ path }),
    }),
  security: () => req<Record<string, unknown>>("/api/security"),
  mcpStatus: () => req<MCPStatus>("/api/mcp"),
  updateMcpServers: (servers: Record<string, MCPServerConfig>) =>
    req<{ ok: boolean; servers: MCPServerStatus[] }>("/api/mcp", {
      method: "POST", body: JSON.stringify({ servers }),
    }),

  chat: (sessionId: string, prompt: string, agentId?: string, reasoningEffort?: string) => {
    const body: Record<string, unknown> = { session_id: sessionId, prompt };
    if (agentId && agentId !== "build") body.agent_id = agentId;
    if (reasoningEffort) body.reasoning_effort = reasoningEffort;
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
  approve: (approvalId: string, approved: boolean) =>
    req<{ ok: boolean }>("/api/approve", {
      method: "POST", body: JSON.stringify({ approval_id: approvalId, approved }),
    }),
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
    }),

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
  importSkill: (payload: { source?: string; zip_base64?: string; scope: string; name?: string; overwrite?: boolean }) =>
    req<{ skills: import("./types").SkillInfo[] }>("/api/skills/import", {
      method: "POST", body: JSON.stringify(payload),
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
    req<import("./types").CommunityManifest>(`/api/plugins/community${url ? `?url=${encodeURIComponent(url)}` : ""}`),
  importPlugin: (payload: { source?: string; zip_base64?: string; name?: string; overwrite?: boolean; version?: string }) =>
    req<{ plugins: import("./types").PluginInfo[] }>("/api/plugins/import", {
      method: "POST", body: JSON.stringify(payload),
    }),
  deletePlugin: (name: string) =>
    req<{ ok: boolean; name: string; path: string }>(`/api/plugins/${encodeURIComponent(name)}`, { method: "DELETE" }),

  // ------------------------------------------------------------ 办公场景：文件上传 / 产出物下载（AGI 通用入口）

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
  outputs: () =>
    req<{ items: OutputItem[] }>("/api/outputs"),
  filePreview: (path: string) =>
    req<FilePreviewResponse>(`/api/files/preview?path=${encodeURIComponent(path)}`),
  outputsZipUrl: (includeUploads = false) =>
    `/api/outputs/zip${includeUploads ? "?include_uploads=true" : ""}`,
  clearOutputs: (scope: "outputs" | "uploads" | "all" = "outputs") =>
    req<{ ok: boolean; scope: string; deleted: number }>(`/api/outputs?scope=${scope}`, {
      method: "DELETE",
    }),
  deleteFile: (path: string) =>
    req<{ ok: boolean; path: string }>(`/api/files?path=${encodeURIComponent(path)}`, {
      method: "DELETE",
    }),
  createProject: (parent: string, name: string, git = true) =>
    req<{ ok: boolean; path: string; name: string; git_initialized: boolean }>(
      "/api/projects/create",
      { method: "POST", body: JSON.stringify({ parent, name, git }) }
    ),
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
