// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import AboutModal from "./components/AboutModal";
import ChatView, { QuestionBar } from "./components/ChatView";
import Composer, { REASONING_EFFORT_OPTIONS } from "./components/Composer";
import ErrorBoundary from "./components/ErrorBoundary";
import PendingQueue from "./components/PendingQueue";
import FileViewer from "./components/FileViewer";
import ProjectPicker from "./components/ProjectPicker";
import SettingsModal from "./components/SettingsModal";
import Sidebar from "./components/Sidebar";
import TabBar from "./components/TabBar";
import ToolPanel from "./components/ToolPanel";
import { useResizable } from "./hooks/useResizable";
import type { JudgeOpinion, AgentInfo, AppConfig, BackgroundTaskInfo, ChatSessionState, CollabMode, ContextStats, ContextTaskStats, LLMConfig, LLMProviderMeta, MCPServerStatus, Msg, PendingApprovalInfo, ServerStatus, SessionInfo, SessionModel, SseConnState, SubAgentProgress, SubAgentStep, TabItem, ToolCardInfo, WorkItem } from "./types";
import { baseName } from "./lib/path";
import { isTextLikePath, resolveOpenTarget } from "./lib/fileOpen";

interface StreamingState {
  items: WorkItem[];
  turn?: number;
}

const TREE_TOUCH_TOOLS = new Set([
  "write_file",
  "apply_search_replace",
  "apply_unified_diff",
  "execute_command",
  "git_commit",
]);

// 办公产出工具：执行成功后刷新侧边栏「产出物」Tab
const OFFICE_TOUCH_TOOLS = new Set([
  "docx_create", "xlsx_create", "pptx_create", "pdf_create",
  "data_analyze", "chart_make", "write_file", "execute_command",
]);

// 审批兜底轮询间隔：审批卡到达前端依赖 SSE 推送，断线窗口丢失的靠该轮询补齐，
// 低频即可（实时路径仍是 SSE approval:request 事件）。
const PENDING_APPROVAL_POLL_MS = 30_000;

// 自动继续（/continue）：任务结束后 TODO 有未完成项时的续推上限与提示词
const AUTO_CONTINUE_MAX = 3;
const CONTINUE_PROMPT =
  "继续执行未完成的任务：对照 TODO 看板继续推进剩余项，不要重复已完成的部分。全部完成后输出最终总结。";

// Esc 停止任务的「确认窗口」：Esc 常被手滑按下，故要求连按两次才真正停止。
// 首次 Esc 只进入待确认态（提示行显示「再按一次 Esc 停止任务」），窗口内再按一次才停；
// 超时未再按则自动解除待确认态。窗口取 2s：够看清提示并补一次按键，又不至于长期挂着重型操作。
// 导出仅供 App.esc.test.tsx 断言窗口边界，业务代码请用常量名而非字面量。
export const ESC_STOP_CONFIRM_MS = 2000;

let tabSeq = 0;
const nextTabId = () => `tab_${++tabSeq}`;

// 「打开项目」（原生目录对话框）的落点意图：该路径会 `window.location.reload()`，
// 没法在旧页面里选会话，所以把「打开后进入该项目最近一条会话」写进 localStorage
// 带过重载边界，由启动时的 effect 消费并立即清除（不影响之后的普通启动）。
const OPEN_LATEST_SESSION_KEY = "litework.openLatestSession";

const EMPTY_CHAT: ChatSessionState = {
  messages: [],
  streaming: null,
  running: false,
  turn: 0,
  stats: null,
  contextStats: null,
  contextHistory: [],
  error: null,
  pendingApprovals: [],
  subAgentRecords: [],
  agentBoard: [],
  stalled: false,
  sseState: "idle",
  todos: [],
  pendingQuestions: [],
  pendingQueue: [],
  worktreeEnabled: false,
  worktreeStatus: null,
  worktreeConflicts: [],
  worktreeMerge: null,
};

/** Agents 看板 reducer：subagent 事件 → 看板卡片更新（右面板 Agents tab 数据源）。
 *
 * 竖排 kanban 语义：started 追加「运行中」卡片；progress 原位更新（turn/步骤/流式文本）；
 * completed 卡片流入「已完成」。匹配优先 subagentId，缺失时回退最近一张同角色运行卡
 * （兼容老后端事件无 id 的情况）。返回 null 表示无变化（引用不变，跳过 setState）。
 */
function reduceAgentBoard(
  board: SubAgentProgress[] | undefined,
  ev: { type: "started" | "progress" | "completed" | "closed"; data: Record<string, unknown> },
): SubAgentProgress[] | null {
  const list = board ? [...board] : [];
  const d = ev.data ?? {};
  const id = typeof d.subagentId === "string" ? d.subagentId : undefined;
  const role = typeof d.role === "string" ? d.role : undefined;

  // closed：从看板移除该 agent 卡片（close_agent 主动关闭，非交付，不流入已完成区）
  if (ev.type === "closed") {
    const aid = typeof d.agentId === "string" ? d.agentId : id;
    if (!aid) return list;
    const next = list.filter((a) => a.subagentId !== aid);
    return next.length === list.length ? null : next;
  }

  if (ev.type === "started") {
    const newId = id ?? `sa_${Date.now().toString(36)}`;
    // followup 唤醒同一 agent 会再次触发 started：同 id 卡片重置为 running
    // （保留任务/模式），而非追加重复卡片
    const idx = id ? list.findIndex((a) => a.subagentId === id) : -1;
    if (idx >= 0) {
      list[idx] = {
        ...list[idx],
        status: "running",
        task: typeof d.task === "string" ? d.task : list[idx].task,
        turn: 0, steps: [], summary: undefined,
        mode: typeof d.mode === "string" ? d.mode : list[idx].mode,
        startedAt: Date.now(),
      };
      return list;
    }
    list.push({
      subagentId: newId,
      role: role ?? "general",
      task: typeof d.task === "string" ? d.task : "",
      turn: 0, steps: [], status: "running", startedAt: Date.now(),
      mode: typeof d.mode === "string" ? d.mode : "orchestrate",
    });
    return list;
  }

  let idx = id ? list.findIndex((a) => a.subagentId === id) : -1;
  if (idx === -1) {
    for (let i = list.length - 1; i >= 0; i -= 1) {
      if (list[i].status === "running" && (!role || list[i].role === role)) { idx = i; break; }
    }
  }
  if (idx === -1) return null;
  const prev = list[idx];

  if (ev.type === "progress") {
    const kind = d.kind;
    if (kind === "llm:turn_start") {
      list[idx] = { ...prev, turn: (typeof d.turn === "number" ? d.turn : prev.turn + 1) };
    } else if (kind === "llm:stream") {
      const text = typeof d.text === "string" ? d.text : "";
      list[idx] = { ...prev, streaming_text: (prev.streaming_text ?? "") + text };
    } else if (kind === "tool:before_execute") {
      const step: SubAgentStep = {
        tool: typeof d.tool === "string" ? d.tool : "?",
        brief: typeof d.brief === "string" ? d.brief : undefined,
        status: "running",
      };
      list[idx] = { ...prev, steps: [...prev.steps, step].slice(-3) };
    } else if (kind === "tool:after_execute") {
      const steps = [...prev.steps];
      const tool = typeof d.tool === "string" ? d.tool : "";
      for (let i = steps.length - 1; i >= 0; i -= 1) {
        if (steps[i].tool === tool && steps[i].status === "running") {
          const st = d.status;
          steps[i] = {
            ...steps[i],
            status: st === "error" ? "error" : st === "cancelled" ? "cancelled" : "done",
            durationMs: typeof d.durationMs === "number" ? d.durationMs : undefined,
          };
          break;
        }
      }
      list[idx] = { ...prev, steps };
    }
    return list;
  }

  // completed：卡片流入「已完成」分区（changed_files 随通知送达 → review gate 数据源）
  const changedFiles = Array.isArray(d.changed_files)
    ? d.changed_files.map(String) : undefined;
  list[idx] = {
    ...prev,
    status: "done",
    summary: typeof d.summary === "string" ? d.summary : prev.summary,
    tokens: typeof d.tokens_used === "number" ? d.tokens_used : prev.tokens,
    turn: typeof d.turns === "number" ? d.turns : prev.turn,
    changedFiles: changedFiles && changedFiles.length > 0 ? changedFiles : prev.changedFiles,
  };
  return list;
}

export default function App() {
  const [tabs, setTabs] = useState<TabItem[]>([]);
  const [activeTabId, setActiveTabId] = useState<string>("");
  const [chatStates, setChatStates] = useState<Record<string, ChatSessionState>>({});

  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [status, setStatus] = useState<ServerStatus | null>(null);
  const [llmConfig, setLlmConfig] = useState<LLMConfig | null>(null);
  const [providerMeta, setProviderMeta] = useState<LLMProviderMeta[]>([]);
  const [showSettings, setShowSettings] = useState(false);
  const [showAbout, setShowAbout] = useState(false);
  const [showPicker, setShowPicker] = useState(false);
  // 模型元数据提醒横幅：启动时检查一次，缓存缺失/过期（>7 天）就顶栏提示
  // 「立即同步」——过期缓存虽仍作兜底（不会静默掉回 128K），但窗口/定价可能
  // 失真，必须在设置页之外看得见的地方提醒，而不是查不到就默默用默认值
  const [metaNotice, setMetaNotice] = useState<{ cached: boolean; ageDays: number } | null>(null);
  const [metaNoticeClosed, setMetaNoticeClosed] = useState(false);
  const [metaSyncing, setMetaSyncing] = useState(false);
  const [metaSyncError, setMetaSyncError] = useState<string | null>(null);
  // 「项目」页签状态：最近项目列表 + 二级视图（list=项目列表 / sessions=项目内会话）
  const [recentProjects, setRecentProjects] = useState<import("./types").RecentProject[]>([]);
  const [projectsView, setProjectsView] = useState<"list" | "sessions">("list");
  // 目录选择器的打开模式：project=打开项目 / code=打开代码（校验 git）/
  // new-project / new-code（新建并展开表单）
  const [pickerMode, setPickerMode] = useState<"project" | "code" | "new-project" | "new-code">("project");
  const [sidebarTab, setSidebarTab] = useState<"sessions" | "files" | "terminal" | "outputs">(() => {
    try {
      const saved = localStorage.getItem("litework.sidebarTab");
      if (saved === "files" || saved === "terminal" || saved === "sessions" || saved === "outputs") return saved;
    } catch { /* ignore */ }
    return "sessions";
  });
  const changeSidebarTab = useCallback((t: "sessions" | "files" | "terminal" | "outputs") => {
    setSidebarTab(t);
    try { localStorage.setItem("litework.sidebarTab", t); } catch { /* ignore */ }
  }, []);
  const [loading, setLoading] = useState(true);
  const [showDebug, setShowDebug] = useState(false);
  const [debugLogs, setDebugLogs] = useState<string[]>([]);
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [currentAgent, setCurrentAgent] = useState<string>("build");
  // 当前任务的 agent 身份（发消息时记录）：llm:stream 流式 chunk 创建
  // 文本 WorkItem 时打标，让气泡图标按产生该内容的 agent 显示
  const currentAgentRef = useRef<string>("build");
  const [success, setSuccess] = useState<string | null>(null);
  const [treeRevision, setTreeRevision] = useState(0);
  const [outputRevision, setOutputRevision] = useState(0);
  // 刷新合并（400ms）：任务执行中每次 write_file/execute_command 都会命中
  // TREE_TOUCH/OFFICE_TOUCH，直接 setState 会让文件树/产出物面板反复全量刷新
  // （后端目录扫描 + worktree git 调用），形成"刷新风暴"。这里首个事件后
  // 400ms 才真正推进一次 revision，窗口内的后续事件合并进同一次刷新。
  const bumpTimersRef = useRef<{ tree: number | null; output: number | null }>({ tree: null, output: null });
  const bumpTree = useCallback(() => {
    if (bumpTimersRef.current.tree !== null) return;
    bumpTimersRef.current.tree = window.setTimeout(() => {
      bumpTimersRef.current.tree = null;
      setTreeRevision((v) => v + 1);
    }, 400);
  }, []);
  const bumpOutput = useCallback(() => {
    if (bumpTimersRef.current.output !== null) return;
    bumpTimersRef.current.output = window.setTimeout(() => {
      bumpTimersRef.current.output = null;
      setOutputRevision((v) => v + 1);
    }, 400);
  }, []);
  const [draftModels, setDraftModels] = useState<Record<string, SessionModel | null>>({});
  const [draftReasoning, setDraftReasoning] = useState<Record<string, string>>({});
  // draft（新会话 tab 未建 session）暂存的模型/推理强度：send 内读 ref 最新值，
  // 避免 useCallback 闭包依赖漏列导致「切换 effort/模型不生效」的回归
  const draftModelsRef = useRef<Record<string, SessionModel | null>>({});
  const draftReasoningRef = useRef<Record<string, string>>({});

  // 对话区是否被上翻（非贴底）：驱动 Composer 的状态化快捷键提示
  const [chatScrolledUp, setChatScrolledUp] = useState(false);
  const handleStickChange = useCallback((stick: boolean) => setChatScrolledUp(!stick), []);
  // 切换标签页后重置，避免上一个标签的「上翻」状态残留成错误提示
  useEffect(() => { setChatScrolledUp(false); }, [activeTabId]);
  // 新会话 tab（session 未创建）暂存的协作模式，首次发送创建 session 后写入
  const [draftCollabModes, setDraftCollabModes] = useState<Record<string, string | null>>({});
  const [mcpServers, setMcpServers] = useState<MCPServerStatus[]>([]);
  const [registeredTools, setRegisteredTools] = useState<{ name: string; description: string }[]>([]);
  // 已安装协作模式（对话框选择器数据源；设置里安装新插件后随 refreshAll 更新）
  const [collabModes, setCollabModes] = useState<CollabMode[]>([]);
  // 综合设置（AppConfig 子集）：聊天区折叠阈值等 UI 行为，保存设置后随 refreshAll 生效
  const [uiConfig, setUiConfig] = useState<AppConfig | null>(null);
  const refreshCollabModes = useCallback(() => {
    api.collabModes().then((r) => setCollabModes(r.modes)).catch(() => {});
  }, []);
  const [backgroundTasks, setBackgroundTasks] = useState<BackgroundTaskInfo[]>([]);
  // 面板折叠状态：默认展开（false=展开）
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [toolPanelCollapsed, setToolPanelCollapsed] = useState(false);
  // 工具面板 tab 受控（聊天区 Agents 状态条可跳转）
  const [toolPanelTab, setToolPanelTab] = useState<"context" | "todos" | "agents" | "mcp" | "background" | "tools">("context");
  // 手动压缩上下文进行中（按 session 记录，避免切 tab 状态串台）
  const [compactingSessions, setCompactingSessions] = useState<Record<string, boolean>>({});

  // 布局边界拖拽：侧边栏 / 右侧工具面板宽度（双击分隔条重置，localStorage 持久化）
  const sidebarResize = useResizable({
    axis: "col", initial: 300, min: 200,
    max: () => Math.min(520, Math.floor(window.innerWidth * 0.45)),
    storageKey: "litework.sidebarWidth.v2",
  });
  const toolPanelResize = useResizable({
    // 右侧工具面板默认约为窗口宽度的 28%（从中间聊天区压缩）。
    // 注：已经拖拽过的机器会优先用 localStorage 里的像素值；双击分隔条可回到本默认值。
    axis: "col", initial: Math.round((typeof window !== "undefined" ? window.innerWidth : 1440) * 0.28), min: 260,
    max: () => Math.min(720, Math.floor(window.innerWidth * 0.45)),
    invert: true, // 分隔条在面板左侧，向左拖 = 增大
    storageKey: "litework.toolPanelWidth.v3",
  });

  // 后台命令轮询：每 2s 拉取右侧工具面板「后台」tab 数据
  useEffect(() => {
    const timer = setInterval(() => {
      api.backgroundTasks().then((r) => setBackgroundTasks(r.tasks)).catch(() => {});
    }, 2000);
    return () => clearInterval(timer);
  }, []);

  const eventSourcesRef = useRef<Map<string, EventSource>>(new Map());
  const taskIdsRef = useRef<Map<string, string>>(new Map());
  const stopRequestedRef = useRef<Set<string>>(new Set());
  const streamingRefs = useRef<Map<string, StreamingState>>(new Map());
  const lastEventTimesRef = useRef<Map<string, number>>(new Map());
  const flushTimersRef = useRef<Map<string, number>>(new Map());
  // SSE 重连控制器：每会话一个 {taskId, attempts, timer}
  const streamCtlRef = useRef<Map<string, { taskId: string; attempts: number; timer: number | null }>>(new Map());
  const chatStatesRef = useRef<Record<string, ChatSessionState>>({});
  const tabsRef = useRef<TabItem[]>([]);
  const sessionsRequestRef = useRef(0);
  // 任务启动器 ref：供 handleSSEEvent 在 task:done 时自动发送下一条（避免循环依赖）
  const taskLauncherRef = useRef<(sid: string, prompt: string, effort?: string, mergeWorktree?: string) => void>(() => {});

  useEffect(() => { chatStatesRef.current = chatStates; }, [chatStates]);
  useEffect(() => { tabsRef.current = tabs; }, [tabs]);
  useEffect(() => { draftModelsRef.current = draftModels; }, [draftModels]);
  useEffect(() => { draftReasoningRef.current = draftReasoning; }, [draftReasoning]);

  // 应用菜单「关于」→ 打开设计版关于弹窗（仅桌面模式有 preload 桥）
  useEffect(() => {
    const bridge = window.liteWork;
    if (!bridge?.onShowAbout) return;
    return bridge.onShowAbout(() => setShowAbout(true));
  }, []);

  const pushLog = useCallback((msg: string) => {
    const line = `${new Date().toLocaleTimeString()} ${msg}`;
    setDebugLogs((prev) => [...prev.slice(-200), line]);
  }, []);

  // ------------------------------------------------------------ 会话状态读写

  const getChat = useCallback((sid: string): ChatSessionState => {
    return chatStatesRef.current[sid] ?? EMPTY_CHAT;
  }, []);

  const patchChat = useCallback((sid: string, patch: Partial<ChatSessionState>) => {
    setChatStates((prev) => {
      const next = { ...prev, [sid]: { ...(prev[sid] ?? EMPTY_CHAT), ...patch } };
      chatStatesRef.current = next;
      return next;
    });
  }, []);

  // ------------------------------------------------------------ 当前活跃 Tab

  // 会话 tab 的 SSE 连接状态点（名字右侧红绿黄）：任务运行中才有值
  const tabSseStates = useMemo(() => {
    const map: Record<string, SseConnState> = {};
    for (const [sid, chat] of Object.entries(chatStates)) {
      if (chat.running && chat.sseState && chat.sseState !== "idle") {
        map[sid] = chat.sseState;
      }
    }
    return map;
  }, [chatStates]);

  const activeTab = useMemo(
    () => tabs.find((t) => t.id === activeTabId) ?? null,
    [tabs, activeTabId]
  );
  const activeSessionId = activeTab?.kind === "chat" ? (activeTab.sessionId ?? null) : null;

  // 活跃会话的工作副本（读写都走 ref 缓存，避免竞态）
  const currentChat = activeSessionId ? getChat(activeSessionId) : EMPTY_CHAT;

  const patchActiveChat = useCallback(
    (patch: Partial<ChatSessionState>) => {
      const sid = activeSessionId;
      if (sid) patchChat(sid, patch);
    },
    [activeSessionId, patchChat]
  );

  // ------------------------------------------------------------ 子 Agent 完成记录自动消失

  // 完成记录是临时通知：显示 TTL 后自动移除，避免聊天底部无限累积
  const SUBAGENT_RECORD_TTL_MS = 20_000;
  const subagentTimersRef = useRef<Map<string, number>>(new Map());

  /** 为一批归档记录安排到期移除（按 subagentId/task 匹配）。 */
  const scheduleSubagentRecordExpiry = useCallback(
    (sid: string, records: SubAgentProgress[]) => {
      if (records.length === 0) return;
      const ids = new Set(records.map((r) => r.subagentId || r.task));
      const timerKey = `${sid}:${[...ids].join(",")}`;
      const t = window.setTimeout(() => {
        subagentTimersRef.current.delete(timerKey);
        const cur = chatStatesRef.current[sid];
        if (!cur?.subAgentRecords?.length) return;
        patchChat(sid, {
          subAgentRecords: cur.subAgentRecords.filter((r) => !ids.has(r.subagentId || r.task)),
        });
      }, SUBAGENT_RECORD_TTL_MS);
      subagentTimersRef.current.set(timerKey, t);
    },
    [patchChat]
  );

  /** Agents 看板事件入口：subagent/agent 事件 → reduceAgentBoard → 会话级看板状态。 */
  const pushAgentEvent = useCallback(
    (sid: string, type: "started" | "progress" | "completed" | "closed", data: Record<string, unknown>) => {
      setChatStates((cur) => {
        const chat = cur[sid];
        if (!chat) return cur;
        const next = reduceAgentBoard(chat.agentBoard, { type, data });
        if (!next) return cur;
        return { ...cur, [sid]: { ...chat, agentBoard: next } };
      });
    },
    []
  );

  /** 主任务结束后同步子 Agent 最终状态到 Agents 看板（SSE 已断，卡片不会自动更新）。
   *
   * 后台子 Agent 生命周期可能超出主任务 SSE 连接：当有 agent 仍在 running 时，
   * 每 5 秒轮询 API 直到全部终态（最多 20 次 = 100 秒，超时自动停止防泄漏）。 */
  const syncSessionAgents = useCallback(async (sid: string, retry = 0) => {
    const MAX_RETRY = 20;
    try {
      const resp = await api.sessionAgents(sid);
      if (!resp.agents?.length) return;
      let hasRunning = false;
      for (const agent of resp.agents) {
        if (agent.status === "completed" || agent.status === "errored" || agent.status === "closed") {
          pushAgentEvent(sid, "completed", {
            subagentId: agent.agent_id,
            role: agent.role,
            task: agent.task,
            turns: agent.turns,
            summary: agent.summary,
            tokens_used: agent.tokens,
            changed_files: agent.changed_files,
          } as Record<string, unknown>);
        } else if (agent.status === "running") {
          hasRunning = true;
        }
      }
      // 还有 running 的 agent：定时轮询（后台子 Agent 生命周期超出任务）
      if (hasRunning && retry < MAX_RETRY) {
        window.setTimeout(() => void syncSessionAgents(sid, retry + 1), 5000);
      }
    } catch {
      // API 调用失败不影响主流程
    }
  }, [pushAgentEvent]);

  // ------------------------------------------------------------ 技能注入气泡

  // 气泡随任务生命周期显示：skill:loaded 时出现，task:done / task:error 清空
  // （异常中断也清；skillLoaded 是内存态，刷新页面自然消失，无需 TTL）

  // 卸载时清理未触发的 subagent 定时器，避免泄漏
  useEffect(() => {
    const timers = subagentTimersRef.current;
    return () => {
      timers.forEach((t) => window.clearTimeout(t));
      timers.clear();
    };
  }, []);

  // ------------------------------------------------------------ 会话列表

  const refreshSessions = useCallback(async (ws?: string): Promise<SessionInfo[]> => {
    const requestId = ++sessionsRequestRef.current;
    try {
      const next = await api.sessions(ws ?? status?.workspace ?? undefined);
      // 多个任务结束/切换项目时请求可能乱序返回，旧响应不能覆盖新列表。
      if (requestId === sessionsRequestRef.current) setSessions(next);
      return next;
    } catch {
      // 拉取失败当作「无历史」：调用方退化为新建空对话，不阻塞「打开项目」主流程
      return [];
    }
  }, [status?.workspace]);

  const refreshAll = useCallback(async () => {
    try {
      const [st, ag, llm, providers, mcp, tools, modes, cfg] = await Promise.all([
        api.status(), api.agents(), api.llmConfig(), api.llmProviders(), api.mcpStatus(),
        // 工具列表按当前 Agent 裁剪；workspace 未就绪时报 409，静默降级为空列表
        api.tools(currentAgent).catch(() => [] as { name: string; description: string }[]),
        api.collabModes().catch(() => ({ modes: [] as CollabMode[] })),
        // 综合设置（聊天区折叠阈值等 UI 行为）：保存设置后经 refreshAll 立即生效
        api.config().catch(() => null),
      ]);
      if (cfg) setUiConfig(cfg);
      setStatus(st);
      setAgents(ag);
      setLlmConfig(llm);
      setProviderMeta(providers);
      setMcpServers(mcp.servers || []);
      setRegisteredTools(tools);
      setCollabModes(modes.modes || []);
      await refreshSessions(st.workspace ?? undefined); // 用刚取到的 workspace，避免 setState 异步时序
    } catch (e) {
      setErrorPublic((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [refreshSessions, currentAgent]);

  // 已注册工具（右面板「工具」Tab）：workspace 就绪且 Agent 切换时刷新
  useEffect(() => {
    if (status?.workspace) {
      api.tools(currentAgent).then(setRegisteredTools).catch(() => {});
    } else {
      setRegisteredTools([]);
    }
  }, [status?.workspace, currentAgent]);

  function setErrorPublic(msg: string | null) {
    patchActiveChat({ error: msg });
  }

  // 渲染进程直接通过 HTTP 切换工作区（双击历史会话 / Web 目录选择器）后，
  // 同步 Electron 主进程的窗口 workspace——终端启动的 cwd 与「请先打开项目」判定都在主进程。
  const notifyElectronWorkspace = useCallback((ws: string) => {
    try {
      window.liteWork?.workspaceChanged?.(ws);
    } catch {
      /* 浏览器模式无 bridge，忽略 */
    }
  }, []);

  // 会话协作模式（对话框选择器）：写入后端 metadata + 本地状态；
  // 本会话后续任务按该模式配方编排（TaskHandle 注入 system prompt）
  const setSessionCollabMode = useCallback(
    (mode: string | null) => {
      const sid = activeSessionId;
      if (!sid) {
        // 新会话 tab：暂存到 draft，session 创建后随首条消息写入
        if (activeTabId) {
          setDraftCollabModes((prev) => ({ ...prev, [activeTabId]: mode }));
        }
        return;
      }
      patchChat(sid, { collabMode: mode });
      void api.setSessionCollab(sid, mode).catch(() => {
        // 写回失败不回滚 UI：下次打开会话按服务端状态恢复
      });
    },
    [activeSessionId, activeTabId, patchChat]
  );

  const setSessionModel = useCallback(async (model: SessionModel | null) => {
    if (!activeSessionId) {
      if (activeTabId) setDraftModels((prev) => ({ ...prev, [activeTabId]: model }));
      return;
    }
    if (currentChat.running) return;
    try {
      const result = await api.setSessionModel(activeSessionId, model);
      patchChat(activeSessionId, {
        modelOverride: result.override,
        effectiveModel: result.effective,
      });
      // 切换后立即刷新「上下文情况」面板的模型名与窗口（端点按会话生效模型解析），
      // 不必等下一次任务的 SSE 推送才更新
      try {
        const ctx = await api.contextStats(activeSessionId);
        if (ctx) patchChat(activeSessionId, { contextStats: ctx });
      } catch {
        // 统计端点异常时面板保持现状
      }
    } catch (e) {
      patchActiveChat({ error: (e as Error).message });
    }
  }, [activeSessionId, activeTabId, currentChat.running, patchActiveChat, patchChat]);

  useEffect(() => {
    void refreshAll();
  }, [refreshAll]);

  // 模型元数据状态检查（启动一次）：缺失/过期 → 顶部横幅提醒。失败静默——
  // 后端不可达时崩溃屏已兜底，横幅不该抢优先级。
  useEffect(() => {
    let cancelled = false;
    api.pricingStatus()
      .then((ps) => {
        if (cancelled) return;
        const md = ps.models_dev;
        if (!md?.stale) return;
        setMetaNotice({
          cached: !!md.cached,
          ageDays: Math.floor((md.age_seconds ?? 0) / 86400),
        });
      })
      .catch(() => { /* ignore */ });
    return () => { cancelled = true; };
  }, []);

  const syncModelMeta = useCallback(async () => {
    setMetaSyncing(true);
    setMetaSyncError(null);
    try {
      const res = await api.syncPricing("models_dev");
      if (!res.ok) {
        setMetaSyncError(res.error || "同步失败，请检查网络后重试");
        return;
      }
      setMetaNotice(null);
    } catch (e) {
      setMetaSyncError((e as Error).message);
    } finally {
      setMetaSyncing(false);
    }
  }, []);

  // ------------------------------------------------------------ Tab 操作

  const closeStream = useCallback((sid?: string) => {
    const teardown = (s: string) => {
      eventSourcesRef.current.get(s)?.close();
      eventSourcesRef.current.delete(s);
      const ctl = streamCtlRef.current.get(s);
      if (ctl?.timer != null) window.clearTimeout(ctl.timer);
      streamCtlRef.current.delete(s);
    };
    if (sid) {
      teardown(sid);
      return;
    }
    for (const s of [...eventSourcesRef.current.keys()]) teardown(s);
  }, []);

  const openSessionTab = useCallback(
    (sid: string, title?: string, opts?: { reuseDraft?: boolean }) => {
      setTabs((prev) => {
        const existing = prev.find((t) => t.kind === "chat" && t.sessionId === sid);
        if (existing) {
          setActiveTabId(existing.id);
          return prev.map((t) => t.id === existing.id ? { ...t, title: title || t.title } : t);
        }
        // reuseDraft：把「还没绑定会话的空对话 tab」直接改造成该会话的 tab，而不是再多开一个
        // （用于「打开项目后自动进入最近会话」——否则会同时留下一个多余的「新会话」页签）
        const draft = opts?.reuseDraft
          ? prev.find((t) => t.kind === "chat" && !t.sessionId)
          : undefined;
        if (draft) {
          setActiveTabId(draft.id);
          return prev.map((t) => t.id === draft.id ? { ...t, sessionId: sid, title: title || sid } : t);
        }
        const tab: TabItem = { id: nextTabId(), kind: "chat", sessionId: sid, title: title || sid };
        setActiveTabId(tab.id);
        return [...prev, tab];
      });
    },
    []
  );

  // 双击目录/工作区根：在系统文件管理器中打开（Electron shell.openPath 对目录即打开 Finder/资源管理器）
  const openDirInSystem = useCallback(async (dirPath: string) => {
    const bridge = window.liteWork;
    if (bridge?.openFile) {
      const r = await bridge.openFile(dirPath);
      if (!r.ok) window.alert(`无法打开目录：${r.error ?? (dirPath || "工作区根目录")}`);
      return;
    }
    const ws = status?.workspace;
    window.alert(
      `浏览器模式无法打开本地目录。完整路径：${ws ? `${ws}${dirPath ? "/" + dirPath : ""}` : dirPath}`
    );
  }, [status?.workspace]);

  // 打开文件页签：目标判定见 lib/fileOpen.ts（markdown 优先系统默认程序，
  // 失败静默回退内置查看器；其余文本内置、非文本桌面走系统/浏览器提示下载）
  const openFileTab = useCallback(async (filePath: string, opts?: { forceBuiltin?: boolean }) => {
    const bridge = window.liteWork;
    const target = resolveOpenTarget(filePath, {
      hasBridge: !!bridge?.openFile,
      forceBuiltin: opts?.forceBuiltin,
    });
    if (target === "system") {
      const r = await bridge!.openFile!(filePath);
      if (r.ok) return;
      // markdown（文本类）：系统无默认程序 → 静默回退内置查看器
      if (!isTextLikePath(filePath)) {
        window.alert(`无法打开文件：${r.error ?? filePath}`);
        return;
      }
    } else if (target === "unsupported") {
      window.alert(
        `「${baseName(filePath)}」不是文本类文件。\n` +
        "桌面应用中将调用系统默认程序打开；当前浏览器模式不支持，请下载后查看" +
        `（下载入口见「产出物」面板）或到 ${api.fileDownloadUrl(filePath)} 下载。`
      );
      return;
    }

    let content = "", diff = "", language = "";
    try {
      const r = await api.readFile(filePath);
      content = r.content;
      language = r.language;
      diff = r.diff;
    } catch (e) {
      content = `// 无法读取文件：${(e as Error).message}`;
    }
    setTabs((prev) => {
      const existing = prev.find((t) => t.kind === "file" && t.filePath === filePath);
      if (existing) {
        setActiveTabId(existing.id);
        return prev.map((t) =>
          t.id === existing.id ? { ...t, fileContent: content, fileDiff: diff, fileLanguage: language } : t
        );
      }
      const tab: TabItem = {
        id: nextTabId(), kind: "file", title: baseName(filePath) || filePath,
        filePath, fileContent: content, fileDiff: diff, fileLanguage: language,
      };
      setActiveTabId(tab.id);
      return [...prev, tab];
    });
    setSidebarTab("files");
  }, []);

  const closeTab = useCallback(
    (id: string) => {
      setTabs((prev) => {
        if (prev.length <= 1) return prev; // 至少保留一个
        const idx = prev.findIndex((t) => t.id === id);
        if (idx < 0) return prev;
        const next = prev.filter((t) => t.id !== id);
        if (activeTabId === id) {
          const neighbor = next[Math.min(idx, next.length - 1)];
          setActiveTabId(neighbor.id);
        }
        return next;
      });
    },
    [activeTabId]
  );

  const newChatTab = useCallback(() => {
    // 占位 tab：不创建后端 session，首条消息发送时才创建并绑定（见 send）
    setTabs((prev) => {
      // 若已存在未绑定会话的空聊天 tab，直接切换过去，避免堆积大量"新会话" tab
      const existing = prev.find((t) => t.kind === "chat" && !t.sessionId);
      if (existing) {
        setActiveTabId(existing.id);
        return prev;
      }
      const tab: TabItem = { id: nextTabId(), kind: "chat", title: "新会话" };
      setActiveTabId(tab.id);
      return [...prev, tab];
    });
  }, []);

  // 推理强度（模型变体）循环切换：off → low → medium → high → max → off
  const cycleReasoningEffort = useCallback(() => {
    const current = activeSessionId
      ? (chatStatesRef.current[activeSessionId]?.reasoningEffort ?? "")
      : (activeTabId ? draftReasoning[activeTabId] ?? "" : "");
    // ""（跟随供应商默认）视为排在 "off" 之前，首次按下落到 "off"
    const idx = REASONING_EFFORT_OPTIONS.findIndex((o) => o.value === current);
    const next = REASONING_EFFORT_OPTIONS[(idx + 1) % REASONING_EFFORT_OPTIONS.length].value;
    if (activeSessionId) patchChat(activeSessionId, { reasoningEffort: next });
    else if (activeTabId) setDraftReasoning((p) => ({ ...p, [activeTabId]: next }));
  }, [activeSessionId, activeTabId, draftReasoning, patchChat]);

  // 全局快捷键（键位对齐 opencode v2）：
  //   Shift+Tab               循环切换 primary agent（Tab 让给命令面板补全）
  //   Alt+1..9                直接选中第 N 个 primary agent
  //   Ctrl+Tab / Alt+↓        下一个标签
  //   Ctrl+Shift+Tab / Alt+↑  上一个标签
  //   Ctrl+1..9 / Ctrl+0      切到第 N 个标签
  //   Alt+W                   关闭当前标签
  //   Alt+N                   新建会话
  //   Ctrl+T                  循环切换推理强度（模型变体）
  //   Esc                     连按两次停止当前任务（弹窗打开或该 Esc 已被其它交互占用时让位）
  // 注：macOS 上 Option(Alt)+字母/数字 会产出特殊字符（e.key 变成 "˜"/"¡"），
  // 故 Alt 组合一律按 e.code（物理键位）匹配；不用 Ctrl+W 是因为 Electron
  // 默认菜单的 Close 占用了它。
  useEffect(() => {
    const primaryAgents = agents.filter((a) => a.mode !== "subagent");

    const digit = (e: KeyboardEvent): number | null => {
      const m = /^(?:Digit|Numpad)([0-9])$/.exec(e.code);
      if (m) return Number(m[1]);
      return /^[0-9]$/.test(e.key) ? Number(e.key) : null;
    };

    const cycleAgent = () => {
      if (primaryAgents.length < 2) return;
      setCurrentAgent((prev) => {
        const idx = primaryAgents.findIndex((a) => a.id === prev);
        const next = idx < 0 || idx >= primaryAgents.length - 1 ? 0 : idx + 1;
        return primaryAgents[next].id;
      });
    };

    const cycleTab = (dir: 1 | -1) => {
      if (tabs.length < 2) return;
      const idx = tabs.findIndex((t) => t.id === activeTabId);
      const next = idx < 0 ? 0 : (idx + dir + tabs.length) % tabs.length;
      setActiveTabId(tabs[next].id);
    };

    const onKeyDown = (e: KeyboardEvent) => {
      const alt = e.altKey && !e.ctrlKey && !e.metaKey;
      const ctrl = e.ctrlKey && !e.altKey && !e.metaKey;

      // Ctrl+Tab / Ctrl+Shift+Tab：标签页前后切换
      if (ctrl && e.key === "Tab") {
        e.preventDefault();
        cycleTab(e.shiftKey ? -1 : 1);
        return;
      }
      // Alt+↓ / Alt+↑：标签页前后切换（同 opencode v2）
      if (alt && (e.code === "ArrowDown" || e.code === "ArrowUp")) {
        e.preventDefault();
        cycleTab(e.code === "ArrowDown" ? 1 : -1);
        return;
      }
      // Alt+N：新建会话
      if (alt && e.code === "KeyN") {
        e.preventDefault();
        newChatTab();
        return;
      }
      // Alt+W：关闭当前标签
      if (alt && e.code === "KeyW") {
        e.preventDefault();
        closeTab(activeTabId);
        return;
      }
      if (ctrl) {
        const n = digit(e);
        // Ctrl+1..9 / Ctrl+0：切到第 N 个标签
        if (n !== null) {
          const target = tabs[n === 0 ? 9 : n - 1];
          if (target) {
            e.preventDefault();
            setActiveTabId(target.id);
          }
          return;
        }
        // Ctrl+T：循环切换推理强度（模型变体）
        if (e.code === "KeyT") {
          e.preventDefault();
          cycleReasoningEffort();
          return;
        }
      }
      // Alt+1..9：直接选中第 N 个 primary agent（build/plan/office/research → 1/2/3/4）
      if (alt) {
        const n = digit(e);
        if (n !== null && n >= 1 && n <= primaryAgents.length) {
          e.preventDefault();
          setCurrentAgent(primaryAgents[n - 1].id);
          return;
        }
      }
      // Shift+Tab：循环切换 primary agent（opencode v2 键位）
      if (e.key === "Tab" && e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey) {
        e.preventDefault();
        cycleAgent();
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [agents, tabs, activeTabId, newChatTab, closeTab, cycleReasoningEffort]);

  const selectSession = useCallback(
    async (sid: string, title?: string, opts?: { reuseDraft?: boolean }) => {
      const info = sessions.find((s) => s.session_id === sid);
      // title 显式传入优先：调用方常常刚刷新过列表，sessions 闭包可能还是旧的（否则会闪成 session id）
      openSessionTab(sid, title || info?.title || sid, opts);
      if (!chatStatesRef.current[sid]) {
        const snap = await api.getSession(sid);
        const initial: Partial<ChatSessionState> = { messages: snap?.messages ?? [] };
        // 会话目标（/goal）：从 metadata 恢复（跨刷新/重启持续生效）
        if (typeof snap?.metadata?.goal === "string" && snap.metadata.goal) {
          initial.goal = snap.metadata.goal;
        }
        // 会话协作模式（对话框选择器）：从 metadata 恢复
        if (typeof snap?.metadata?.collab_mode === "string" && snap.metadata.collab_mode) {
          initial.collabMode = snap.metadata.collab_mode;
        }
        // 隔离工作树（/worktree）：从 metadata 恢复开关；无论开关与否都拉一次
        // 变更状态——关闭模式后遗留的 worktree 也要能看到评审卡（合并/丢弃）
        if (snap?.metadata?.worktree === true) {
          initial.worktreeEnabled = true;
        }
        void api.worktreeStatus(sid).then((st) => {
          if (st.exists) {
            patchChat(sid, { worktreeStatus: st });
            return;
          }
          // 恢复校验：元数据说"隔离中"，但工作树已不在（已合并/丢弃/被清理）→
          // 不静默降级，关闭模式并给出可见提示（对标 Claude Code 的 resume 校验）
          if (snap?.metadata?.worktree === true) {
            patchChat(sid, {
              worktreeEnabled: false,
              messages: [...(snap?.messages ?? []), {
                role: "assistant",
                content: "⚠ 本会话的隔离工作树已不存在（可能已合并 / 丢弃 / 被清理），已自动关闭隔离模式。\n\n如需继续在隔离工作树中工作，请重新 `/worktree on`。",
              }],
            });
            void api.setSessionWorktree(sid, false).catch(() => {});
          }
        }).catch(() => {});
        // 从会话 metadata 恢复子 Agent 归档卡片（跨页面刷新保留）
        if (snap?.metadata?.subagent_records) {
          const restored = (snap.metadata.subagent_records as any[]).map((r) => ({
            subagentId: r.subagentId ?? "",
            role: r.role ?? "general",
            task: r.task ?? "",
            turn: r.turns ?? 0,
            steps: [],
            status: "done" as const,
            summary: r.summary ?? "",
            tokens: r.tokens ?? 0,
            mode: typeof r.mode === "string" ? r.mode : "orchestrate",
            changedFiles: Array.isArray(r.changed_files)
              ? r.changed_files.map(String) : undefined,
            startedAt: typeof r.started_at === "number" ? r.started_at : undefined,
          }));
          initial.subAgentRecords = restored;
          // 同一批记录进入 Agents 看板（「已完成」分区，无 TTL：看板是会话级交接记录）
          initial.agentBoard = restored;
          // 恢复的完成记录同样按 TTL 自动消失（临时通知语义，仅聊天区归档卡）
          scheduleSubagentRecordExpiry(sid, initial.subAgentRecords);
        }
        patchChat(sid, { ...EMPTY_CHAT, ...initial });
        try {
          // 并行拉取模型覆盖、上下文统计与 TODO 看板，一次 patch 消除两次 patch 之间的显示闪烁
          const [model, ctx, todos] = await Promise.all([
            api.sessionModel(sid).catch(() => null),
            api.contextStats(sid).catch(() => null),
            api.getTodos(sid).catch(() => null),
          ]);
          if (todos && Array.isArray(todos.todos)) {
            patchChat(sid, { todos: todos.todos });
          }
          if (model) {
            patchChat(sid, { modelOverride: model.override, effectiveModel: model.effective });
          }
          if (ctx && (ctx.model || (ctx.session && Object.keys(ctx.session).length > 0))) {
            // 端点现携带会话生效模型、窗口与计费单价，重开会话即可正确显示，不再置空等待 SSE
            patchChat(sid, {
              contextStats: {
                model: ctx.model || "",
                context_window: ctx.context_window || 0,
                session: ctx.session ?? {},
                pricing: ctx.pricing,
              },
            });
          }
        } catch {
          // 旧会话或刚创建的会话没有模型覆盖时，沿用系统默认。
        }
      }
    },
    [openSessionTab, patchChat, sessions]
  );

  /**
   * 打开项目后的默认落点：该项目**最近一条**会话；没有历史则新建空对话。
   *
   * 为什么不做成「落空白新会话」：换项目后停在空白页会让人以为历史丢了、
   * 也要重新翻侧栏找上次的进度；落到最近一条即可接着上次的上下文继续，
   * 想开新对话直接发消息（新建会话那条路径不受影响）。
   *
   * 取「最近」不依赖接口顺序：后端虽按 updated_at 倒序返回，这里仍显式按
   * updated_at 取最大值，接口顺序若变也不会选错。
   */
  const openLatestSessionOrNewChat = useCallback(
    async (ws: string, opts?: { reuseDraft?: boolean }) => {
      const list = await refreshSessions(ws);
      const latest = [...list].sort((a, b) => (b.updated_at ?? 0) - (a.updated_at ?? 0))[0];
      if (latest?.session_id) {
        pushLog(`↻ 已打开最近会话：${latest.title || latest.session_id}`);
        await selectSession(latest.session_id, latest.title, opts);
        return;
      }
      newChatTab();
    },
    [refreshSessions, selectSession, newChatTab, pushLog]
  );

  // 双击会话：切到该会话关联的项目，再打开会话（便于接着原项目继续开发）
  const openSessionWithProject = useCallback(
    async (sid: string) => {
      const info = sessions.find((s) => s.session_id === sid);
      const targetWs = (info?.metadata?.workspace as string) || "";
      if (targetWs && status?.workspace !== targetWs) {
        try {
          const res = await api.setWorkspace(targetWs);
          if (res.ok) {
            setStatus((prev) => (prev ? { ...prev, workspace: res.workspace } : prev));
            notifyElectronWorkspace(res.workspace);
            pushLog(`📂 已切换到项目: ${res.workspace}`);
            closeStream();
            setChatStates({});
            chatStatesRef.current = {};
            await refreshSessions(res.workspace);
          }
        } catch (e) {
          setErrorPublic((e as Error).message);
          return;
        }
      }
      await selectSession(sid);
    },
    [sessions, status?.workspace, selectSession, closeStream, refreshSessions, pushLog, notifyElectronWorkspace]
  );

  const deleteSession = useCallback(
    async (id: string) => {
      try {
        await api.deleteSession(id);
        setTabs((prev) => {
          const next = prev.filter((t) => t.sessionId !== id);
          if (next.length === 0) {
            newChatTab();
            return prev;
          }
          if (activeTabId === id || prev.find((t) => t.id === activeTabId)?.sessionId === id) {
            const nb = next[next.length - 1];
            setActiveTabId(nb.id);
          }
          return next;
        });
        await refreshSessions();
      } catch {
        /* ignore */
      }
    },
    [activeTabId, refreshSessions, newChatTab]
  );

  /** 批量删除会话：后端批量删除后同步页签（被删会话的页签一并关闭）与会话列表。 */
  const deleteSessions = useCallback(
    async (ids: string[]) => {
      try {
        await api.deleteSessionsBatch(ids);
        const idSet = new Set(ids);
        setTabs((prev) => {
          const next = prev.filter((t) => !t.sessionId || !idSet.has(t.sessionId));
          if (next.length === 0) {
            newChatTab();
            return prev;
          }
          const cur = prev.find((t) => t.id === activeTabId);
          if (cur?.sessionId && idSet.has(cur.sessionId)) {
            setActiveTabId(next[next.length - 1].id);
          }
          return next;
        });
        await refreshSessions();
      } catch {
        /* ignore（与单删风格一致） */
      }
    },
    [activeTabId, refreshSessions, newChatTab]
  );

  const openProject = useCallback(async () => {
    if (window.liteWork) {
      try {
        const result = await window.liteWork.openProject();
        if (!result.ok && result.error !== "cancelled") {
          patchActiveChat({ error: result.error ?? "无法切换项目" });
        }
        if (result.ok) {
          // 重载后默认落在「文件」Tab，直接看到新项目目录树
          try { localStorage.setItem("litework.sidebarTab", "files"); } catch { /* ignore */ }
          // 「打开项目」后进入该项目最近一条会话：本条路径会整页重载，选不了会话，
          // 于是把意图写进 localStorage 带过重载边界，由启动 effect 消费并清除。
          try { localStorage.setItem(OPEN_LATEST_SESSION_KEY, result.workspace ?? ""); } catch { /* ignore */ }
          window.location.reload();
        }
      } catch (e) {
        patchActiveChat({ error: (e as Error).message });
      }
      return;
    }
    setShowPicker(true);
  }, [patchActiveChat]);

  const openProjectNewWindow = useCallback(async () => {
    if (!window.liteWork) {
      window.alert("新窗口打开项目仅支持桌面应用。");
      return;
    }
    const result = await window.liteWork.openProjectNewWindow();
    if (!result.ok && result.error !== "cancelled") {
      patchActiveChat({ error: result.error ?? "无法打开新项目窗口" });
    }
  }, [patchActiveChat]);

  const requestNewChat = useCallback(() => {
    if (status?.workspace) {
      newChatTab();
      return;
    }
    window.alert("请先打开项目后再新建对话。");
    void openProject();
  }, [newChatTab, openProject, status?.workspace]);

  // ------------------------------------------------------------ 项目页签：最近项目

  const refreshRecentProjects = useCallback(async () => {
    try {
      const r = await api.recentProjects();
      setRecentProjects(r.items);
    } catch {
      /* 最近列表加载失败不阻断主流程 */
    }
  }, []);

  // 启动时加载一次最近项目；已有工作区（桌面端记住上次项目）直接进项目内视图
  useEffect(() => {
    if (loading) return;
    void refreshRecentProjects();
    if (status?.workspace) setProjectsView("sessions");
    // 仅在初次完成加载时执行一次（用 loading 的下降沿）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading]);

  const selectProject = useCallback(
    async (path: string) => {
      setShowPicker(false);
      try {
        // 打开代码模式：校验是 git 仓库（非 git 仓库提示后按项目打开）。
        // 注意 fsList 默认过滤隐藏目录——检查 .git 必须传 showHidden
        if (pickerMode === "code") {
          const fs = await api.fsList(path, true).catch(() => null);
          if (fs && !fs.dirs.includes(".git")) {
            if (!window.confirm(
              `「${baseName(path)}」不是 git 仓库。\n\n仍作为普通项目打开？（如需 git 仓库，请先在目录内 git init）`
            )) {
              return;
            }
          }
        }
        const res = await api.setWorkspace(path);
        if (res.ok) {
          setStatus((prev) => (prev ? { ...prev, workspace: res.workspace } : prev));
          notifyElectronWorkspace(res.workspace);
          // 打开项目 → 「项目」页签进入项目内会话视图（先有项目，再有会话）
          changeSidebarTab("sessions");
          setProjectsView("sessions");
          void refreshRecentProjects();
          setSuccess(`已切换到项目: ${res.workspace}`);
          pushLog(`📂 已切换到项目: ${res.workspace}`);
          setTimeout(() => setSuccess(null), 4000);
          closeStream();
          setChatStates({});
          chatStatesRef.current = {};

          // 切换项目后自动进入该项目最近一条会话；没有历史才新建空对话
          await openLatestSessionOrNewChat(res.workspace);
        }
      } catch (e) {
        setErrorPublic((e as Error).message);
      }
    },
    [pushLog, closeStream, openLatestSessionOrNewChat, patchActiveChat, changeSidebarTab, notifyElectronWorkspace, refreshRecentProjects, pickerMode]
  );

  // ------------------------------------------------------------ 项目页签：最近项目

  const openCode = useCallback(() => {
    setPickerMode("code");
    setShowPicker(true);
  }, []);

  const openProjectEntry = useCallback(() => {
    setPickerMode("project");
    void openProject();
  }, [openProject]);

  const newProjectEntry = useCallback((git: boolean) => {
    setPickerMode(git ? "new-code" : "new-project");
    setShowPicker(true);
  }, []);

  const openRecentProject = useCallback(async (path: string) => {
    // 当前有任务运行时后端会 409；直接走 setWorkspace 相同的错误提示
    try {
      const res = await api.openProject(path);
      if (res.ok) {
        setStatus((prev) => (prev ? { ...prev, workspace: res.workspace } : prev));
        notifyElectronWorkspace(res.workspace);
        changeSidebarTab("sessions");
        setProjectsView("sessions");
        void refreshRecentProjects();
        setSuccess(`已打开: ${res.workspace}`);
        setTimeout(() => setSuccess(null), 4000);
        closeStream();
        setChatStates({});
        chatStatesRef.current = {};
        // 与其它「打开项目」入口一致：进入该项目最近一条会话（无历史才新建空对话）
        await openLatestSessionOrNewChat(res.workspace);
      }
    } catch (e) {
      setErrorPublic((e as Error).message);
    }
  }, [changeSidebarTab, closeStream, openLatestSessionOrNewChat, notifyElectronWorkspace, refreshRecentProjects]);

  /** 在隔离工作树中打开项目：切换项目 → 新建会话 → 开启 worktree 模式。 */
  const openWorktreeSession = useCallback(async (path: string) => {
    try {
      const res = await api.openProject(path);
      if (!res.ok) return;
      setStatus((prev) => (prev ? { ...prev, workspace: res.workspace } : prev));
      notifyElectronWorkspace(res.workspace);
      changeSidebarTab("sessions");
      setProjectsView("sessions");
      void refreshRecentProjects();
      closeStream();
      setChatStates({});
      chatStatesRef.current = {};
      // 新建会话并开启隔离工作树（任务将在独立分支+目录中执行）
      const { session_id } = await api.createSession();
      const r = await api.setSessionWorktree(session_id, true);
      if (!r.ok) {
        setErrorPublic("当前项目不是 git 仓库，隔离工作树不可用");
        setTimeout(() => setErrorPublic(null), 4000);
        newChatTab();
      } else {
        patchChat(session_id, { ...EMPTY_CHAT, worktreeEnabled: true });
        openSessionTab(session_id, "🛡️ 隔离工作树");
        setSuccess("已在新会话中开启隔离工作树");
        setTimeout(() => setSuccess(null), 4000);
      }
      await refreshSessions(res.workspace);
    } catch (e) {
      setErrorPublic((e as Error).message);
    }
  }, [changeSidebarTab, closeStream, newChatTab, notifyElectronWorkspace, openSessionTab,
      patchChat, refreshRecentProjects, refreshSessions]);

  const removeRecentProject = useCallback(async (path: string) => {
    try {
      await api.removeRecentProject(path);
      setRecentProjects((prev) => prev.filter((p) => p.path !== path));
    } catch {
      /* 忽略 */
    }
  }, []);

  const togglePinProject = useCallback(async (path: string) => {
    try {
      const r = await api.toggleProjectPin(path);
      // 后端重排（pinned 前置），直接刷新整表最简单且顺序正确
      const list = await api.recentProjects();
      setRecentProjects(list.items);
      setSuccess(r.pinned ? "已置顶" : "已取消置顶");
      setTimeout(() => setSuccess(null), 2000);
    } catch {
      /* 忽略 */
    }
  }, []);

  // 手动标记项目类型（code/project）：写入并锁定，启发式不再覆盖
  const toggleProjectKind = useCallback(async (path: string, kind: "code" | "project") => {
    try {
      await api.setProjectKind(path, kind);
      const list = await api.recentProjects();
      setRecentProjects(list.items);
      setSuccess(kind === "code" ? "已标记为代码项目" : "已标记为普通项目");
      setTimeout(() => setSuccess(null), 2000);
    } catch (e) {
      setErrorPublic((e as Error).message);
    }
  }, []);

  const backToProjects = useCallback(() => setProjectsView("list"), []);

  // 首次启动：打开一个占位会话 tab
  useEffect(() => {
    if (loading || tabsRef.current.length > 0) return;
    newChatTab();
  }, [loading, newChatTab]);

  // 「打开项目」（原生目录对话框）重载后的落点：进入该项目**最近一条**会话。
  // 该意图以一次性 localStorage 承载（见 openProject），消费即清除；用 ref 保证
  // StrictMode 双执行下不会重复开页签。没有历史会话时保留上面新建的空对话 tab。
  const openedLatestRef = useRef(false);
  useEffect(() => {
    if (loading || openedLatestRef.current) return;
    let ws = "";
    try {
      ws = localStorage.getItem(OPEN_LATEST_SESSION_KEY) ?? "";
      if (ws) localStorage.removeItem(OPEN_LATEST_SESSION_KEY);
    } catch { /* ignore */ }
    if (!ws) return;
    openedLatestRef.current = true;
    // reuseDraft：复用启动时建的空对话 tab，而不是再多开一个
    void openLatestSessionOrNewChat(ws, { reuseDraft: true });
  }, [loading, openLatestSessionOrNewChat]);

  // 切到某会话时刷新其 worktree 状态：多会话同项目下，别的会话合并后主分支会前进，
  // 本会话要能看到「主分支已前进 / 合并目标分支」的最新信息（不进则静默忽略）
  useEffect(() => {
    if (!activeSessionId) return;
    if (!getChat(activeSessionId).worktreeEnabled && !getChat(activeSessionId).worktreeStatus?.exists) return;
    void api.worktreeStatus(activeSessionId).then((st) => {
      if (st.exists) patchChat(activeSessionId, { worktreeStatus: st });
    }).catch(() => {});
  }, [activeSessionId, getChat, patchChat]);

  // 会话刷新后同步 chat Tab 标题（用会话名而非 session ID）
  useEffect(() => {
    if (sessions.length === 0 || tabsRef.current.length === 0) return;
    setTabs((prev) => {
      let changed = false;
      const next = prev.map((t) => {
        if (t.kind !== "chat" || !t.sessionId) return t;
        const info = sessions.find((s) => s.session_id === t.sessionId);
        if (!info || !info.title || info.title === t.title) return t;
        changed = true;
        return { ...t, title: info.title };
      });
      return changed ? next : prev;
    });
  }, [sessions]);

  // ------------------------------------------------------------ 流式节流

  const scheduleStreamFlush = useCallback((sid: string) => {
    if (flushTimersRef.current.has(sid)) return;
    const timer = window.setTimeout(() => {
      flushTimersRef.current.delete(sid);
      const cur = streamingRefs.current.get(sid) ?? { items: [] };
      patchChat(sid, { streaming: { ...cur } });
    }, 80);
    flushTimersRef.current.set(sid, timer);
  }, [patchChat]);

  const cancelStreamFlush = useCallback((sid: string) => {
    const timer = flushTimersRef.current.get(sid);
    if (timer !== undefined) {
      clearTimeout(timer);
      flushTimersRef.current.delete(sid);
    }
  }, []);

  // ------------------------------------------------------------ SSE 事件处理

  const handleSSEEvent = useCallback(
    (sid: string, ev: import("./types").SSEEvent) => {
      lastEventTimesRef.current.set(sid, Date.now());
      const st = () => patchChat(sid, { stalled: false });
      st();
      const log = (msg: string) => pushLog(msg);

      switch (ev.type) {
        case "llm:stream": {
          // SSE chunk 的到达速度高于 React state 提交速度。必须从 ref 读取
          // 已累积内容，否则连续 chunk 会基于旧 state 互相覆盖并缺字。
          const cur = streamingRefs.current.get(sid) ?? getChat(sid).streaming ?? { items: [] };
          const last = cur.items[cur.items.length - 1];
          const items: WorkItem[] = last?.type === "text"
            ? [...cur.items.slice(0, -1), { ...last, content: last.content + ev.data.chunk }]
            : [...cur.items, { type: "text" as const, id: `s${Date.now()}-${Math.random()}`, content: ev.data.chunk, agent: currentAgentRef.current }];
          streamingRefs.current.set(sid, { ...cur, items });
          scheduleStreamFlush(sid);
          break;
        }
        case "llm:turn_start": {
          log(`⟳ 第 ${ev.data.turn} 轮`);
          const cur = streamingRefs.current.get(sid) ?? getChat(sid).streaming ?? { items: [] };
          streamingRefs.current.set(sid, { ...cur, turn: ev.data.turn });
          // 新一轮开始：清掉上一轮的实时速度（本轮还没产生数据）
          patchChat(sid, { streaming: { ...streamingRefs.current.get(sid)! }, liveSpeed: null });
          break;
        }
        case "llm:progress": {
          // 流式生成中的实时速度（估算值，后端已按 ~400ms 节流）：
          // 只更新一个轻量字段，不触碰 streaming/messages，避免高频重渲染聊天区
          patchChat(sid, { liveSpeed: ev.data });
          break;
        }
        case "llm:retry": {
          log(`⏳ LLM 调用失败（${ev.data.reason}），${ev.data.wait}s 后第 ${ev.data.attempt}/${ev.data.max_retries} 次重试`);
          break;
        }
        case "tool:before_execute": {
          const card: ToolCardInfo = {
            id: ev.data.callId ?? `t${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
            callId: ev.data.callId,
            name: ev.data.toolName,
            args: ev.data.args,
            status: "running",
            startedAt: Date.now(), // 运行中卡片实时计时（卡死可观测）
            timeoutMs: ev.data.timeoutMs, // 后端超时下发：卡死看门狗依据
          };
          const cur = streamingRefs.current.get(sid) ?? getChat(sid).streaming ?? { items: [] };
          // spawn_sub_agent / spawn_agent 独立成卡（承载子 Agent 活动面板），其余工具进紧凑聚合
          if (ev.data.toolName === "spawn_sub_agent" || ev.data.toolName === "spawn_agent") {
            const items = [...cur.items, { type: "tool" as const, id: card.id, card }];
            streamingRefs.current.set(sid, { ...cur, items });
          } else {
            const lastItem = cur.items[cur.items.length - 1];
            const items = lastItem?.type === "activity"
              ? [...cur.items.slice(0, -1), { ...lastItem, tools: [...lastItem.tools, card] }]
              : [...cur.items, { type: "activity" as const, id: `a${Date.now()}`, tools: [card] }];
            streamingRefs.current.set(sid, { ...cur, items });
          }
          patchChat(sid, { streaming: { ...streamingRefs.current.get(sid)! } });
          break;
        }
        case "tool:after_execute": {
          if (TREE_TOUCH_TOOLS.has(ev.data.toolName)) bumpTree();
          if (OFFICE_TOUCH_TOOLS.has(ev.data.toolName) && ev.data.status !== "error") bumpOutput();
          const cur = streamingRefs.current.get(sid) ?? getChat(sid).streaming;
          if (!cur) break;
          // callId 精确匹配（并行安全），缺失时回退 name+running 启发式（兼容旧后端）
          const status: ToolCardInfo["status"] = ev.data.status === "cancelled"
            ? "cancelled"
            : ev.data.status === "error" || ev.data.status === "timeout"
              || (ev.data.result ?? "").startsWith("[Execution Exception]")
              || (ev.data.result ?? "").startsWith("[Error]")
              || (ev.data.result ?? "").startsWith("[Tool Timeout]")
              ? "error"
              : "done";
          const matchCard = (c: ToolCardInfo) =>
            (ev.data.callId ? c.callId === ev.data.callId : c.name === ev.data.toolName && c.status === "running");
          const items = cur.items.map((item) => {
            if (item.type === "tool" && item.card.name === ev.data.toolName && matchCard(item.card)) {
              return { ...item, card: { ...item.card, status, durationMs: ev.data.durationMs, ...(ev.data.result !== undefined ? { result: ev.data.result } : {}) } };
            }
            if (item.type !== "activity") return item;
            const idx = item.tools.findIndex(matchCard);
            if (idx < 0) return item;
            const tools = item.tools.map((c, j) => j !== idx ? c : {
              ...c, status, durationMs: ev.data.durationMs,
              ...(ev.data.result !== undefined ? { result: ev.data.result } : {}),
            });
            return { ...item, tools };
          });
          streamingRefs.current.set(sid, { ...cur, items });
          patchChat(sid, { streaming: { ...streamingRefs.current.get(sid)! } });
          break;
        }
        case "approval:request": {
          const cur = getChat(sid);
          patchChat(sid, {
            pendingApprovals: [
              ...(cur.pendingApprovals ?? []).filter((p) => p.id !== ev.data.id),
              { id: ev.data.id, action: ev.data.action, reason: ev.data.reason,
                rememberable: (ev.data as { rememberable?: boolean }).rememberable,
                // 判定类插件的风险意见 → 审批卡上展示
                judge_opinion: (ev.data as { judge_opinion?: JudgeOpinion | null }).judge_opinion ?? null },
            ],
          });
          break;
        }
        case "approval:resolved": {
          const cur = getChat(sid);
          patchChat(sid, {
            pendingApprovals: (cur.pendingApprovals ?? []).filter((p) => p.id !== ev.data.id),
          });
          if ((ev.data as { by?: string }).by === "timeout") {
            pushLog("⏰ 有一项审批因超时未确认已被自动拒绝（任务可能因此中断）");
          }
          break;
        }
        case "question:request": {
          const cur = getChat(sid);
          patchChat(sid, {
            pendingQuestions: [
              ...(cur.pendingQuestions ?? []).filter((q) => q.id !== ev.data.id),
              { id: ev.data.id, question: ev.data.question, options: ev.data.options ?? [] },
            ],
          });
          break;
        }
        case "question:resolved": {
          const cur = getChat(sid);
          patchChat(sid, {
            pendingQuestions: (cur.pendingQuestions ?? []).filter((q) => q.id !== ev.data.id),
          });
          break;
        }
        case "stats:update": {
          patchChat(sid, { stats: ev.data });
          break;
        }
        case "context:stats": {
          // 追加水位轨迹（最近 60 点）：面板趋势图的数据源；替换式更新当前统计。
          // 同时把本轮速度（已由 usage 校准）写进轨迹 → 速度趋势图。
          const cur = getChat(sid);
          const last = ev.data?.task?.last;
          const point = {
            p: ev.data?.task?.last_prompt_tokens ?? 0,
            tps: last?.tps_gen ?? null,
          };
          const history = [...(cur.contextHistory ?? []), point].slice(-60);
          // 本轮已出精确结果 → 清掉实时估算值（面板随之从「估算」切到「精确」）
          patchChat(sid, { contextStats: ev.data, contextHistory: history, liveSpeed: null });
          break;
        }
        case "chat:queued": {
          // 后端确认补充指令入队（乐观气泡已在发送时上屏）
          pushLog(`📥 补充指令已入队（待注入 ${ev.data.count} 条）`);
          break;
        }
        case "message:added": {
          // 后端注入的补充指令进入上下文：按内容精确匹配，清除本地消息的
          // "已入队"标记（注入消息原文为 [用户补充指令] <原文>）
          const msg = ev.data.message;
          if (msg?.role === "user" && typeof msg.content === "string") {
            const PREFIX = "[用户补充指令] ";
            const raw = msg.content.startsWith(PREFIX) ? msg.content.slice(PREFIX.length) : msg.content;
            const cur = getChat(sid);
            if (cur.messages.some((m) => m.queued && m.content === raw)) {
              patchChat(sid, {
                messages: cur.messages.map((m) =>
                  m.queued && m.content === raw ? { ...m, queued: false } : m
                ),
              });
              pushLog("📥 补充指令已注入当前任务");
            }
          }
          break;
        }
        case "todo:updated": {
          patchChat(sid, { todos: ev.data.todos ?? [] });
          break;
        }
        case "task:done": {
          bumpTree();
          const cur = streamingRefs.current.get(sid);
          // 归档本轮的子 Agent 活动卡到会话级记录（最终回复下方折叠展示）
          const finished = (cur?.items ?? [])
            .filter((i) => i.type === "tool" && i.card.subagent)
            .map((i) => (i.type === "tool" ? i.card.subagent! : null))
            .filter((s): s is SubAgentProgress => s !== null);
          const finalMsg: Msg = { role: "assistant", content: cur?.items.filter((i) => i.type === "text").map((i) => i.content).join("\n\n") || ev.data.content };
          patchChat(sid, {
            messages: [...getChat(sid).messages, finalMsg],
            stats: ev.data.stats,
            streaming: null,
            running: false,
            turn: 0,
            sseState: "idle",
            liveSpeed: null,
            subAgentRecords: [...(getChat(sid).subAgentRecords ?? []), ...finished],
            skillLoaded: undefined,
          });
          // 完成记录为临时通知：TTL 后自动消失
          scheduleSubagentRecordExpiry(sid, finished);
          cancelStreamFlush(sid);
          streamingRefs.current.delete(sid);
          closeStream(sid);
          taskIdsRef.current.delete(sid);
          stopRequestedRef.current.delete(sid);
          pushLog("■ 任务结束");
          void (async () => {
            const snap = await api.getSession(sid);
            if (snap) patchChat(sid, { messages: snap.messages ?? [] });
          })();
          // 隔离工作树：任务结束刷新变更状态 → 聊天区出现评审卡（合并/丢弃）
          if (getChat(sid).worktreeEnabled) {
            void api.worktreeStatus(sid)
              .then((st) => patchChat(sid, { worktreeStatus: st.exists ? st : null }))
              .catch(() => {});
          }
          void refreshSessions();
          // 竞态收口：入队成功但任务在注入前结束的补充指令（仍带 queued 标记），
          // 前插到待发送队列由下方重发兜底，避免丢消息；同时清掉标记（即将作为
          // 新任务重发，⏳ 已无意义）
          const done = getChat(sid);
          const unconsumed = (done.messages ?? [])
            .filter((m) => m.queued && typeof m.content === "string")
            .map((m) => m.content as string);
          let queue = done.pendingQueue ?? [];
          if (unconsumed.length > 0) {
            // 去重：提交失败的消息可能同时在 messages(queued) 与 pendingQueue
            queue = [...unconsumed, ...queue.filter((t) => !unconsumed.includes(t))];
            patchChat(sid, {
              pendingQueue: queue,
              messages: done.messages.map((m) => (m.queued ? { ...m, queued: false } : m)),
            });
            pushLog(`📋 ${unconsumed.length} 条补充指令未注入，转入待发送队列`);
          }
          // 自动发送待发送队列中的下一条
          if (queue.length > 0) {
            const next = queue[0];
            patchChat(sid, { pendingQueue: queue.slice(1) });
            pushLog(`📋 自动发送队列下一条（剩余 ${queue.length - 1} 条）`);
            taskLauncherRef.current(sid, next, done.reasoningEffort);
            break;
          }
          // 目标循环（/loop）：队列清空、目标未宣告完成且未达上限时自动续发推进指令
          const goalDone = /\[GOAL[ _-]?COMPLETE\]/i.test(ev.data.content || "");
          if (goalDone) pushLog("🎯 模型宣告目标完成");
          const afterQueue = getChat(sid);
          if (afterQueue.loopEnabled) {
            const maxN = afterQueue.loopMax ?? 10;
            if (goalDone || (afterQueue.loopCount ?? 0) >= maxN) {
              patchChat(sid, { loopEnabled: false });
              pushLog(goalDone ? "🔁 目标循环自动结束（目标已完成）" : `🔁 目标循环达到 ${maxN} 轮上限，已自动关闭`);
              break;
            }
            const n = (afterQueue.loopCount ?? 0) + 1;
            patchChat(sid, { loopCount: n });
            pushLog(`🔁 目标循环：第 ${n}/${maxN} 轮自动推进`);
            taskLauncherRef.current(
              sid,
              `[目标循环 第 ${n}/${maxN} 轮] 继续推进当前会话目标。基于已有进展继续工作；` +
              "若目标已经完成，请直接输出最终总结并在回复中包含 [GOAL_COMPLETE] 标记，不要再调用工具。",
              afterQueue.reasoningEffort,
            );
            break;
          }
          // 自动继续（/continue）：TODO 有未完成项 → 自动续推或展示「继续」按钮。
          // 模型长任务常提前收尾（说"接下来我将…"就停），TODO 看板是最可靠的未完成信号。
          const unfinishedTodos = (afterQueue.todos ?? []).filter((t) => t.status !== "completed");
          if (unfinishedTodos.length === 0) {
            patchChat(sid, { unfinished: false });
            break;
          }
          if (afterQueue.autoContinue) {
            const used = afterQueue.autoContinueCount ?? 0;
            if (used >= AUTO_CONTINUE_MAX) {
              patchChat(sid, { autoContinue: false, unfinished: true });
              pushLog(`⏸ 自动继续达到 ${AUTO_CONTINUE_MAX} 轮上限，已停止（TODO 仍有 ${unfinishedTodos.length} 个未完成项）`);
              break;
            }
            patchChat(sid, { autoContinueCount: used + 1, unfinished: false });
            pushLog(`⏭ 自动继续：TODO 有 ${unfinishedTodos.length} 个未完成项，第 ${used + 1}/${AUTO_CONTINUE_MAX} 轮续推`);
            taskLauncherRef.current(sid, CONTINUE_PROMPT, afterQueue.reasoningEffort);
          } else {
            patchChat(sid, { unfinished: true });
            pushLog(`⏸ TODO 有 ${unfinishedTodos.length} 个未完成项：可点击「继续」推进，或 /continue on 开启自动续推`);
          }
          break;
        }
        case "task:error": {
          pushLog(`✗ 任务错误: ${ev.data.message}`);
          const curChat = getChat(sid);
          // 任务出错/被手动停止：目标循环一并终止（错误循环会烧 token）
          if (curChat.loopEnabled) pushLog("🔁 任务终止，目标循环已自动关闭");
          patchChat(sid, {
            error: ev.data.message,
            running: false,
            sseState: "idle",
            skillLoaded: undefined,
            ...(curChat.loopEnabled ? { loopEnabled: false } : {}),
          });
          cancelStreamFlush(sid);
          streamingRefs.current.delete(sid);
          closeStream(sid);
          taskIdsRef.current.delete(sid);
          stopRequestedRef.current.delete(sid);
          bumpTree();
          void refreshSessions();
          break;
        }
        case "subagent:started": {
          // 关联到对应 spawn_sub_agent 卡片（callId 匹配，缺失时取最近一张 running 卡）
          const cur = streamingRefs.current.get(sid) ?? getChat(sid).streaming;
          if (cur) {
            let matched = false;
            const items = cur.items.map((item) => {
              if (matched) return item;
              const card = item.type === "tool" ? item.card : null;
              if (!card || (card.name !== "spawn_sub_agent" && card.name !== "spawn_agent") || card.status !== "running") return item;
              if (ev.data.callId && card.callId && card.callId !== ev.data.callId) return item;
              matched = true;
              return {
                ...item,
                card: {
                  ...card,
                  subagent: {
                    subagentId: ev.data.subagentId ?? card.id,
                    role: ev.data.role,
                    task: ev.data.task,
                    turn: 0,
                    steps: [],
                    status: "running" as const,
                  },
                },
              };
            });
            if (matched) {
              streamingRefs.current.set(sid, { ...cur, items });
              patchChat(sid, { streaming: { ...streamingRefs.current.get(sid)! } });
            }
          }
          log(`◈ 启动子 Agent ${ev.data.role}`);
          pushAgentEvent(sid, "started", ev.data as unknown as Record<string, unknown>);
          break;
        }
        case "subagent:progress": {
          pushAgentEvent(sid, "progress", ev.data as unknown as Record<string, unknown>);
          const cur = streamingRefs.current.get(sid) ?? getChat(sid).streaming;
          if (!cur) break;
          const evData = ev.data;
          const items = cur.items.map((item) => {
            const card = item.type === "tool" ? item.card : null;
            if (!card || (card.name !== "spawn_sub_agent" && card.name !== "spawn_agent") || card.status !== "running") return item;
            const sa = card.subagent;
            if (!sa) return item;
            if (evData.callId && card.callId && evData.callId !== card.callId) return item;
            if (evData.subagentId && sa.subagentId && evData.subagentId !== sa.subagentId) return item;
            if (evData.kind === "llm:turn_start") {
              return { ...item, card: { ...card, subagent: { ...sa, turn: evData.turn ?? sa.turn + 1 } } };
            }
            if (evData.kind === "llm:stream") {
              // 子 Agent 流式文本：累积显示（节流后按批次追加）
              return {
                ...item,
                card: { ...card, subagent: { ...sa, streaming_text: (sa.streaming_text ?? "") + (evData.text ?? "") } },
              };
            }
            if (evData.kind === "tool:before_execute") {
              const step: SubAgentStep = {
                tool: evData.tool ?? "?", brief: evData.brief, status: "running",
              };
              // 只保留最近 4 步，防长任务刷屏
              return { ...item, card: { ...card, subagent: { ...sa, steps: [...sa.steps, step].slice(-4) } } };
            }
            // tool:after_execute：把最近一条同名 running 步骤置为终态
            const steps = [...sa.steps];
            for (let i = steps.length - 1; i >= 0; i -= 1) {
              if (steps[i].tool === evData.tool && steps[i].status === "running") {
                steps[i] = {
                  ...steps[i],
                  status: evData.status === "error" || evData.status === "timeout" ? "error"
                    : evData.status === "cancelled" ? "cancelled" : "done",
                  durationMs: evData.durationMs,
                };
                break;
              }
            }
            return { ...item, card: { ...card, subagent: { ...sa, steps } } };
          });
          streamingRefs.current.set(sid, { ...cur, items });
          patchChat(sid, { streaming: { ...streamingRefs.current.get(sid)! } });
          break;
        }
        case "subagent:completed": {
          const data = ev.data;
          // errored（LLM 失败/超时/步数耗尽等）→ 卡片置失败态，summary 显示错误摘要；
          // 否则才显示「已完成」。避免子 Agent 秒死时前端误报完成。
          const errored = data.status === "errored";
          const cur = streamingRefs.current.get(sid) ?? getChat(sid).streaming;
          if (cur) {
            let finalized: SubAgentProgress | null = null;
            const items = cur.items.map((item) => {
              const card = item.type === "tool" ? item.card : null;
              if (!card || (card.name !== "spawn_sub_agent" && card.name !== "spawn_agent") || card.status !== "running") return item;
              if (data.callId && card.callId && data.callId !== card.callId) return item;
              if (!finalized) {
                const sa = card.subagent;
                finalized = {
                  subagentId: data.subagentId ?? sa?.subagentId ?? card.id,
                  role: data.role,
                  task: sa?.task ?? data.task,
                  turn: data.turns ?? sa?.turn ?? 0,
                  steps: sa?.steps ?? [],
                  status: errored ? "error" : "done",
                  summary: errored ? (data.error ?? data.summary) : data.summary,
                  tokens: data.tokens_used,
                };
              }
              return {
                ...item,
                card: {
                  ...card,
                  status: errored ? ("error" as const) : ("done" as const),
                  durationMs: card.durationMs,
                  subagent: finalized ?? undefined,
                },
              };
            });
            if (finalized) {
              streamingRefs.current.set(sid, { ...cur, items });
              patchChat(sid, { streaming: { ...streamingRefs.current.get(sid)! } });
            }
          }
          bumpTree();
          if (errored) {
            const brief = String(data.error ?? data.summary ?? "未知错误").slice(0, 120);
            log(`◈ 子 Agent ${String(data.role ?? "general")} 已失败：${brief}`);
          } else {
            log(`◈ 子 Agent ${String(data.role ?? "general")} 已完成`);
          }
          pushAgentEvent(sid, "completed", data as unknown as Record<string, unknown>);
          break;
        }
        case "agent:closed": {
          // 关闭分流：by=agent（close_agent 工具）→ 从看板移除卡片；
          // by=user（看板手动取消）→ 置终态保留卡片（乐观更新已先行，此处幂等兜底）
          const closedData = ev.data as Record<string, unknown>;
          const byUser = closedData.by === "user";
          log(`◈ Agent ${String(closedData.agentId ?? "")} 已${byUser ? "被用户取消" : "关闭"}`);
          if (byUser) {
            const aid = String(closedData.agentId ?? "");
            const chat = getChat(sid);
            const board = chat.agentBoard ?? [];
            if (board.some((a) => a.subagentId === aid && a.status === "running")) {
              patchChat(sid, {
                agentBoard: board.map((a) => a.subagentId === aid && a.status === "running"
                  ? { ...a, status: "done" as const, summary: a.summary ?? "已被用户取消" }
                  : a),
              });
            }
          } else {
            pushAgentEvent(sid, "closed", closedData);
          }
          break;
        }
        case "skill:loaded": {
          const names = ev.data.names ?? [];
          if (names.length > 0) {
            patchChat(sid, { skillLoaded: names });
          }
          break;
        }
        case "worktree:merge": {
          // 合并阶段进度：started → conflicts → merged / failed
          const d = ev.data as {
            phase: string; branch: string; conflicts: string[];
            files: number; commits: number; message: string;
          };
          patchChat(sid, { worktreeMerge: {
            phase: d.phase, conflicts: d.conflicts ?? [],
            files: d.files ?? 0, commits: d.commits ?? 0, message: d.message ?? "",
          } });
          if (d.phase === "conflicts") {
            patchChat(sid, { worktreeConflicts: d.conflicts ?? [] });
          } else if (d.phase === "merged") {
            // 合并完成：清卡片 + 关闭隔离开关（后端已同步 metadata）；
            // 进度条保留几秒再收（否则用户看不到"已合并"）
            patchChat(sid, { worktreeStatus: null, worktreeConflicts: [], worktreeEnabled: false });
            setSuccess("隔离工作树已合并到主工作区");
            setTimeout(() => setSuccess(null), 3000);
            window.setTimeout(() => patchChat(sid, { worktreeMerge: null }), 3500);
            bumpTree();
          } else if (d.phase === "failed") {
            void api.worktreeStatus(sid).then((st) => {
              if (st.exists) patchChat(sid, { worktreeStatus: st });
            }).catch(() => {});
          }
          break;
        }
        default:
          break;
      }
     },
    [getChat, patchChat, pushLog, scheduleSubagentRecordExpiry, scheduleStreamFlush, cancelStreamFlush, closeStream, refreshSessions]
  );

  // ------------------------------------------------------------ SSE 连接管理

  const MAX_SSE_RECONNECT = 8;
  const SSE_BACKOFF_CAP_MS = 15_000;

  /** 探测任务是否仍存在：200=可重连 / 404=已被服务端清理（按正常结束收尾）。 */
  const probeTaskAlive = useCallback((taskId: string) => new Promise<number>((resolve) => {
    const ac = new AbortController();
    const t = window.setTimeout(() => ac.abort(), 5000);
    fetch(`/api/tasks/${taskId}/events`, { signal: ac.signal })
      .then((r) => {
        window.clearTimeout(t);
        r.body?.cancel().catch(() => {});
        resolve(r.status);
      })
      .catch(() => {
        window.clearTimeout(t);
        resolve(0);
      });
  }), []);

  // ------------------------------------------------------------ 审批兜底同步

  /** 把 /api/approvals/pending 拉到的一批审批按 id 去重并入指定会话的审批卡。
   *
   * 过滤口径与 SSE 重连兜底一致：`a.session_id === sid`（子 Agent 的审批在后端
   * 以 root_session_id=主会话上报，故主会话 id 能命中）。返回新增条数。
   * 抽成公共函数供「SSE 重连 onopen」与「低频轮询」两处复用，避免逻辑漂移。
   */
  const mergePendingApprovals = useCallback(
    (sid: string, approvals: PendingApprovalInfo[]) => {
      const mine = approvals.filter((a) => a.session_id === sid);
      if (mine.length === 0) return 0;
      const cur = chatStatesRef.current[sid];
      const known = new Set((cur?.pendingApprovals ?? []).map((p) => p.id));
      const missing = mine.filter((a) => !known.has(a.id));
      if (missing.length === 0) return 0;
      patchChat(sid, {
        pendingApprovals: [
          ...(cur?.pendingApprovals ?? []),
          ...missing.map((a) => ({ id: a.id, action: a.action, reason: a.reason,
            rememberable: a.rememberable })),
        ],
      });
      return missing.length;
    },
    [patchChat]
  );

  /** 拉取并合并某会话当前挂起的审批（SSE 重连兜底 / 低频轮询共用）。 */
  const pullPendingApprovals = useCallback((sid: string) => {
    void api.pendingApprovals().then((r) => {
      const n = mergePendingApprovals(sid, r?.approvals ?? []);
      if (n > 0) pushLog(`🛡️ 同步到 ${n} 条待确认审批`);
    }).catch(() => { /* 同步失败不影响主流程 */ });
  }, [mergePendingApprovals, pushLog]);

  /** 统一的任务 SSE 生命周期：连接、断开自动重连（指数退避）、[DONE] 收尾。

   * - 网络抖动：浏览器原生重连（readyState=CONNECTING），状态指示转黄；
   * - 服务端拒绝/不可达（readyState=CLOSED）：手动重建连接，1s 起指数退避
   *   （上限 15s，至多 8 次），重连前先探测任务存活（404 → 视作正常结束）；
   * - 服务端断连期间事件保留在任务队列，重连后续读，不丢事件。
   */
  const connectTaskStream = useCallback(
    (sid: string, taskId: string) => {
      closeStream(sid);
      const ctl: { taskId: string; attempts: number; timer: number | null } = {
        taskId, attempts: 0, timer: null,
      };
      streamCtlRef.current.set(sid, ctl);
      patchChat(sid, { sseState: "connecting" });

      const scheduleReconnect = (targetSid: string) => {
        const c = streamCtlRef.current.get(targetSid);
        if (!c) return;
        const chat = chatStatesRef.current[targetSid];
        if (!chat?.running) {
          patchChat(targetSid, { sseState: "idle" });
          return;
        }
        if (c.attempts >= MAX_SSE_RECONNECT) {
          patchChat(targetSid, { sseState: "lost" });
          pushLog(`✗ SSE 重连失败（${MAX_SSE_RECONNECT} 次），任务仍在后端运行，可刷新页面恢复`);
          return;
        }
        const delay = Math.min(1000 * 2 ** c.attempts, SSE_BACKOFF_CAP_MS);
        c.attempts += 1;
        patchChat(targetSid, { sseState: "reconnecting" });
        pushLog(`⚠ 连接断开，${Math.round(delay / 1000)}s 后重连（第 ${c.attempts}/${MAX_SSE_RECONNECT} 次）`);
        c.timer = window.setTimeout(() => {
          c.timer = null;
          void probeTaskAlive(c.taskId).then((status) => {
            if (!streamCtlRef.current.get(targetSid)) return; // 连接已被正常收尾
            if (status === 404) {
              closeStream(targetSid);
              patchChat(targetSid, { sseState: "idle" });
              void syncSessionAgents(targetSid);
              return;
            }
            open();
          });
        }, delay);
      };

      const open = () => {
        const es = new EventSource(`/api/tasks/${taskId}/events`);
        eventSourcesRef.current.set(sid, es);
        es.onopen = () => {
          ctl.attempts = 0;
          lastEventTimesRef.current.set(sid, Date.now());
          patchChat(sid, { sseState: "connected" });
          pushLog(`🔗 已连接任务 ${taskId}`);
          // 重连后兜底同步挂起审批：SSE 断线窗口里发出的 approval:request
          // 不回放（tasks.subscribe 重连不回放历史），错过即审批卡永不出现，
          // 600s 后静默超时拒绝——这里拉一次 pending 补齐
          pullPendingApprovals(sid);
        };
        es.onmessage = (e) => {
          if (e.data === "[DONE]") {
            closeStream(sid);
            patchChat(sid, { sseState: "idle" });
            // 主任务结束：后台子 Agent 可能仍在运行或刚完成，SSE 已断。
            // 调用 API 同步最终状态到 Agents 看板（否则卡片永久卡在 running）。
            void syncSessionAgents(sid);
            return;
          }
          try {
            handleSSEEvent(sid, JSON.parse(e.data));
          } catch {
            /* ignore */
          }
        };
        es.onerror = () => {
          if (es.readyState === EventSource.CLOSED) {
            // 服务端拒绝（404）或持续不可达：浏览器已放弃，手动接管重连
            eventSourcesRef.current.delete(sid);
            scheduleReconnect(sid);
          } else {
            // 网络抖动：浏览器原生自动重连中（约 3s）
            patchChat(sid, { sseState: "reconnecting" });
            pushLog("⚠ SSE 连接中断，等待重连…");
          }
        };
      };

      open();
    },
    [closeStream, handleSSEEvent, patchChat, probeTaskAlive, pullPendingApprovals, pushLog, syncSessionAgents]
  );

  // 审批兜底轮询（30s）：审批卡到达前端原本只依赖 SSE approval:request 事件，
  // 断线/刷新窗口内错过即永久丢失（→ 600s 超时静默拒绝，表现为「卡死在不问权限」）。
  // 低频拉取挂起审批，按当前活跃会话 id 过滤补齐（与 SSE 重连兜底同一口径）；
  // 无活跃会话时不起轮询，会话切换/卸载时清 interval。
  useEffect(() => {
    if (!activeSessionId) return;
    const sid = activeSessionId;
    pullPendingApprovals(sid);
    const timer = setInterval(() => pullPendingApprovals(sid), PENDING_APPROVAL_POLL_MS);
    return () => clearInterval(timer);
  }, [activeSessionId, pullPendingApprovals]);

  // 30s stall 检测：任务运行中超时无事件仅提示不阻断
  useEffect(() => {
    const STALL_MS = 30_000;
    const timer = setInterval(() => {
      const now = Date.now();
      for (const [sid, chat] of Object.entries(chatStatesRef.current)) {
        if (!chat.running || chat.stalled) continue;
        const last = lastEventTimesRef.current.get(sid);
        if (last && now - last > STALL_MS) {
          patchChat(sid, { stalled: true });
          pushLog(`⏳ 会话 ${sid.slice(0, 12)}… 已 ${Math.round((now - last) / 1000)}s 无事件`);
        }
      }
    }, 5000);
    return () => clearInterval(timer);
  }, [patchChat, pushLog]);

  // ------------------------------------------------------------ 会话目标与目标循环（/goal · /loop）

  /** /goal：<目标描述> 设置 · 无参查看 · clear 清除。目标持久化于会话 metadata，
   *  后端在每个任务的 system prompt 注入（跨任务持续生效）。 */
  const runGoalCommand = useCallback(
    async (raw: string) => {
      const sid = activeSessionId;
      if (!sid) {
        window.alert("当前没有会话，请先打开或新建会话。");
        return;
      }
      const arg = raw.replace(/^\/goal\s*/i, "").trim();
      const say = (content: string) => {
        const cur = getChat(sid);
        patchChat(sid, { messages: [...(cur.messages ?? []), { role: "user", content: raw }, { role: "assistant", content }] });
      };
      if (!arg) {
        const cur = getChat(sid).goal;
        say(cur ? `🎯 当前会话目标：${cur}` : "当前未设置目标。用法：`/goal <目标描述>` 设置 · `/goal clear` 清除");
        return;
      }
      if (arg.toLowerCase() === "clear" || arg === "清除") {
        await api.setSessionGoal(sid, null).catch(() => {});
        patchChat(sid, { goal: null, loopEnabled: false });
        say("🎯 会话目标已清除（关联的自动循环一并关闭）。");
        pushLog("🎯 会话目标已清除");
        return;
      }
      try {
        const r = await api.setSessionGoal(sid, arg);
        patchChat(sid, { goal: r.goal ?? arg });
        say(`🎯 会话目标已设置：${arg}\n\n之后每个任务都会带着该目标执行；配合 \`/loop\` 可自动循环推进直至完成。`);
        pushLog("🎯 会话目标已设置");
      } catch (e) {
        patchChat(sid, { error: (e as Error).message });
      }
    },
    [activeSessionId, getChat, patchChat, pushLog]
  );

  /** /loop：开启目标自动循环（任务结束→自动续发推进指令）。 */
  const runLoopCommand = useCallback(
    (raw: string) => {
      const sid = activeSessionId;
      if (!sid) {
        window.alert("当前没有会话，请先打开或新建会话。");
        return;
      }
      const arg = raw.replace(/^\/loop\s*/i, "").trim().toLowerCase();
      const chat = getChat(sid);
      const say = (content: string) => {
        patchChat(sid, { messages: [...(chat.messages ?? []), { role: "user", content: raw }, { role: "assistant", content }] });
      };
      if (arg === "off" || arg === "stop" || arg === "关闭") {
        patchChat(sid, { loopEnabled: false, loopCount: 0 });
        say("🔁 目标自动循环已关闭。");
        pushLog("🔁 目标循环已关闭");
        return;
      }
      if (!chat.goal) {
        say("请先用 `/goal <目标描述>` 设置会话目标，再开启 `/loop` 自动推进。");
        return;
      }
      let max = 10;
      if (/^\d+$/.test(arg)) max = Math.max(1, Math.min(50, parseInt(arg, 10)));
      patchChat(sid, { loopEnabled: true, loopMax: max, loopCount: 0 });
      say(`🔁 目标自动循环已开启（最多 ${max} 轮）：当前任务结束后自动续发推进指令，模型宣告完成（[GOAL_COMPLETE]）、出错或达到上限时自动停止；\`/loop off\` 可随时手动停止。`);
      pushLog(`🔁 目标循环开启（上限 ${max} 轮）`);
    },
    [activeSessionId, getChat, patchChat, pushLog]
  );

  /** /continue：自动继续开关（任务结束后 TODO 有未完成项 → 自动续推）。 */
  const runContinueCommand = useCallback(
    (raw: string) => {
      const sid = activeSessionId;
      if (!sid) {
        window.alert("当前没有会话，请先打开或新建会话。");
        return;
      }
      const arg = raw.replace(/^\/continue\s*/i, "").trim().toLowerCase();
      const chat = getChat(sid);
      const say = (content: string) => {
        patchChat(sid, { messages: [...(chat.messages ?? []), { role: "user", content: raw }, { role: "assistant", content }] });
      };
      if (arg === "off" || arg === "stop" || arg === "关闭") {
        patchChat(sid, { autoContinue: false, autoContinueCount: 0 });
        say("⏭ 自动继续已关闭。");
        pushLog("⏭ 自动继续已关闭");
        return;
      }
      if (arg === "" || arg === "status" || arg === "状态") {
        say(chat.autoContinue
          ? `⏭ 自动继续已开启（本任务已续推 ${chat.autoContinueCount ?? 0}/${AUTO_CONTINUE_MAX} 轮）。关闭用 \`/continue off\`。`
          : "⏭ 自动继续未开启。开启用 `/continue on`：任务结束后若 TODO 看板仍有未完成项，自动续发推进指令（避免模型提前收尾后需要手动输入「继续」）。");
        return;
      }
      if (arg === "on" || arg === "开启") {
        if (chat.loopEnabled) {
          say("目标循环（/loop）运行中，无需叠加自动继续；先用 `/loop off` 关闭。");
          return;
        }
        patchChat(sid, { autoContinue: true, autoContinueCount: 0 });
        say(`⏭ 自动继续已开启（上限 ${AUTO_CONTINUE_MAX} 轮）：任务结束后若 TODO 有未完成项，自动续发推进指令；达到上限或 /continue off 停止。`);
        pushLog(`⏭ 自动继续开启（上限 ${AUTO_CONTINUE_MAX} 轮）`);
        return;
      }
      say("用法：`/continue on` 开启 · `/continue off` 关闭 · `/continue` 查看状态。");
    },
    [activeSessionId, getChat, patchChat, pushLog]
  );

  // ------------------------------------------------------------ 隔离工作树（/worktree）

  /** /worktree on|off|status：切换隔离工作树（任务在独立 worktree 执行）。 */
  const runWorktreeCommand = useCallback(
    async (raw: string) => {
      const arg = raw.replace(/^\/worktree\s*/i, "").trim().toLowerCase();
      const [sub, ...restParts] = arg.split(/\s+/);
      const rest = restParts.join(" ").trim();

      /** 输出命令结果：有会话 → 聊天区可见消息；无会话 → 顶部提示条 + 日志。
       *  项目级命令（list/clean）不依赖会话，但不能"静默"——用户必须看到结果。 */
      const emitOut = (text: string) => {
        const sidNow = activeSessionId;
        if (sidNow) {
          const chat = getChat(sidNow);
          patchChat(sidNow, {
            messages: [...(chat.messages ?? []), { role: "assistant", content: text }],
          });
        } else {
          setSuccess(text);
          setTimeout(() => setSuccess(null), 5000);
        }
        pushLog(text);
      };

      /** 列出遗留 worktree（项目级，无需会话）。 */
      const doList = async () => {
        const listed = await api.worktreeList().catch(() => null);
        if (!listed) {
          emitOut("✗ 读取隔离工作树列表失败（后端未响应），请稍后重试。");
          return;
        }
        const worktrees = listed.worktrees;
        const sid = activeSessionId;
        const cur = sid ? await api.worktreeStatus(sid).catch(() => null) : null;
        const curLine = cur?.exists
          ? `\n当前会话：已有工作树（${cur.files?.length ?? 0} 个文件变更，分支 ${cur.branch ?? "-"}）`
          : (sid ? "\n当前会话：尚未创建工作树（/worktree on 开启）" : "");
        if (worktrees.length === 0) {
          emitOut(`🛡️ 当前项目没有隔离工作树。${curLine}`);
          return;
        }
        const names = worktrees.map((w) => `\n· ${w.name}：${w.files?.length ?? 0} 个文件变更（${w.branch ?? "-"}）`).join("");
        emitOut(`🛡️ 隔离工作树 ${worktrees.length} 个：${names}${curLine}\n\n清理用 /worktree clean <名字>`);
      };

      // list / clean 是**项目级**操作（不依赖某个会话）：留到会话分支之前，
      // 空 tab（还没有会话）时也能用——否则遗留 worktree 无从清理
      if (sub === "list" || sub === "列表") {
        await doList();
        return;
      }
      if (sub === "clean" || sub === "清理") {
        // clean            → 清所有无改动的
        // clean list       → 等价 list（防误输，别当成名为 list 的 worktree）
        // clean all        → 全清（含改动）
        // clean <name>     → 只清指定那一个
        if (!rest) {
          const r = await api.worktreeClean().catch(() => null);
          if (r === null) {
            emitOut("✗ 清理失败：后端未响应，请稍后重试。");
            return;
          }
          const kept = r.kept.length ? `\n保留 ${r.kept.length} 个有改动的：${r.kept.join("、")}（用 /worktree clean <名字> 单独处理）` : "";
          emitOut(`🛡️ 已清理 ${r.removed.length} 个无改动的隔离工作树${r.removed.length ? `：${r.removed.join("、")}` : ""}。${kept}`);
          bumpTree();
          return;
        }
        if (rest === "list" || rest === "列表") {
          await doList();
          return;
        }
        if (rest === "all" || rest === "全部") {
          if (!window.confirm("确定丢弃所有遗留隔离工作树的改动？此操作不可恢复（主工作区不受影响）。")) return;
          const r = await api.worktreeClean(true).catch(() => null);
          if (r === null) {
            emitOut("✗ 清理失败：后端未响应，请稍后重试。");
            return;
          }
          emitOut(`🛡️ 已清理全部 ${r.removed.length} 个隔离工作树（含改动）：${r.removed.join("、") || "无"}`);
          bumpTree();
          return;
        }
        const name = rest;
        const st = await api.worktreeStatus(name).catch(() => null);
        if (!st?.exists) {
          const listed = await api.worktreeList().catch(() => null);
          const hint = listed && listed.worktrees.length
            ? `现有：${listed.worktrees.map((w) => w.name).join("、")}`
            : "当前没有隔离工作树（/worktree list 查看）";
          emitOut(`✗ 未找到名为「${name}」的隔离工作树。${hint}`);
          return;
        }
        const hasChanges = (st.files?.length ?? 0) > 0;
        if (hasChanges && !window.confirm(
          `「${name}」有 ${st.files?.length} 个文件变更，确定丢弃？此操作不可恢复（主工作区不受影响）。`)) return;
        await api.worktreeClean(true, name).catch(() => {});
        emitOut(`🛡️ 已清理隔离工作树「${name}」${hasChanges ? "（含改动）" : ""}`);
        bumpTree();
        return;
      }

      const sid = activeSessionId;
      if (!sid) {
        window.alert("当前没有会话，请先打开或新建会话。");
        return;
      }
      const chat = getChat(sid);
      const say = (content: string) => {
        patchChat(sid, { messages: [...(chat.messages ?? []), { role: "user", content: raw }, { role: "assistant", content }] });
      };
      if (sub === "off" || sub === "关闭") {
        await api.setSessionWorktree(sid, false).catch(() => {});
        patchChat(sid, { worktreeEnabled: false });
        // 关闭模式不等于丢弃改动：拉一次状态，让遗留 worktree 的评审卡继续可操作
        const st = await api.worktreeStatus(sid).catch(() => null);
        if (st?.exists && (st.files?.length ?? 0) > 0) {
          patchChat(sid, { worktreeStatus: st });
          say(`🛡️ 隔离工作树已关闭（后续任务回到主工作区）。\n\n⚠️ 仍有未处理的 worktree 变更（${st.files?.length ?? 0} 个文件），下方评审卡可继续「合并 / 丢弃」；不处理则一直保留在磁盘上。`);
        } else {
          patchChat(sid, { worktreeStatus: null });
          say("🛡️ 隔离工作树已关闭（后续任务回到主工作区）。");
        }
        pushLog("🛡️ 隔离工作树已关闭");
        bumpTree();
        return;
      }
      if (sub === "" || sub === "status" || sub === "状态") {
        const st = await api.worktreeStatus(sid).catch(() => null);
        if (!st || !st.exists) {
          say(chat.worktreeEnabled
            ? "🛡️ 隔离工作树已开启（本会话任务将在独立工作树执行）。关闭用 `/worktree off`。"
            : "🛡️ 隔离工作树未开启。开启用 `/worktree on`：任务在独立分支 + 目录中执行，主工作区不受影响，结束后你可审查变更再决定合并或丢弃。");
          return;
        }
        say(`🛡️ 隔离工作树：分支 ${st.branch} · 变更 ${st.files?.length ?? 0} 个文件（+${st.adds ?? 0} / -${st.dels ?? 0}）。可在下方评审卡「合并 / 丢弃」。`);
        return;
      }
      if (sub === "on" || sub === "开启") {
        // 先看有没有遗留的 worktree：有则复用（保留上次未处理的改动），提示用户
        const prev = await api.worktreeStatus(sid).catch(() => null);
        const r = await api.setSessionWorktree(sid, true).catch((e) => ({ ok: false, reason: (e as Error).message }));
        if (!(r as { ok?: boolean }).ok) {
          say(`⚠️ 无法开启隔离工作树：${(r as { reason?: string }).reason || "当前项目不是 git 仓库"}`);
          return;
        }
        patchChat(sid, { worktreeEnabled: true });
        // 开启后立刻拉一次状态：让评审卡/横幅马上带出分支名（否则要等任务结束才出现）
        const after = await api.worktreeStatus(sid).catch(() => null);
        if (after?.exists) patchChat(sid, { worktreeStatus: after });
        const wtBranch = (r as { worktree?: { branch?: string } }).worktree?.branch;
        const branchNote = wtBranch ? `分支 ${wtBranch}，` : "";
        if (prev?.exists) {
          if (prev.files && prev.files.length > 0) {
            patchChat(sid, { worktreeStatus: prev });
            say(`🛡️ 隔离工作树已开启（**复用上次遗留的工作树**：${branchNote}已有 ${prev.files.length} 个文件变更）。\n\n后续任务在同一分支继续；下方评审卡可合并或丢弃。`);
          } else {
            say(`🛡️ 隔离工作树已开启（复用上次的工作树${wtBranch ? `，分支 ${wtBranch}` : ""}）。`);
          }
        } else {
          say(`🛡️ 隔离工作树已开启${wtBranch ? `（分支 ${wtBranch}）` : ""}：本会话的任务将在独立目录（.lite-work/worktrees/）中执行，**主工作区不受影响**。任务结束后，聊天区会浮出评审卡：可「查看 diff / 直接合并 / 🤖 让 AI 合并 / 丢弃」。`);
        }
        pushLog(`🛡️ 隔离工作树已开启${wtBranch ? ` · ${wtBranch}` : ""}`);
        bumpTree();
        return;
      }
      say("用法：`/worktree on` 开启 · `/worktree off` 关闭 · `/worktree` 查状态 · `/worktree list` 列出遗留 · `/worktree clean` 清无改动的 · `/worktree clean <名字>` 清单个 · `/worktree clean all` 全清。");
    },
    [activeSessionId, getChat, patchChat, pushLog]
  );

  /** 评审卡：查看 diff（日志面板输出）。 */
  const handleWorktreeDiff = useCallback(async (sid: string) => {
    try {
      const { diff } = await api.worktreeDiff(sid);
      if (!diff.trim()) {
        pushLog("🛡️ 隔离工作树无改动");
        return;
      }
      const preview = diff.length > 4000 ? `${diff.slice(0, 4000)}\n…（已截断，完整 diff 见 .lite-work/worktrees/_patches/）` : diff;
      pushLog(`🛡️ 隔离工作树变更 diff:\n${preview}`);
    } catch (e) {
      pushLog(`✗ 读取 diff 失败: ${(e as Error).message}`);
    }
  }, [pushLog]);

  /** 评审卡：合并回主工作区（`git merge --no-ff`；冲突则保留现场并展示冲突文件）。 */
  const handleWorktreeMerge = useCallback(async (sid: string) => {
    try {
      const r = await api.worktreeMerge(sid);
      patchChat(sid, { worktreeStatus: null, worktreeConflicts: [] });
      pushLog(`🛡️ 已合并 ${r.merged ?? 0} 个文件到主工作区（worktree 已清理）`);
      setSuccess("隔离工作树已合并到主工作区");
      bumpTree();
      setTimeout(() => setSuccess(null), 2500);
    } catch (e) {
      const msg = (e as Error).message;
      // 冲突：后端 409 + detail；冲突文件列表单独拉一次状态展示
      const st = await api.worktreeStatus(sid).catch(() => null);
      patchChat(sid, { worktreeConflicts: [] });
      pushLog(`⚠ 合并未完成：${msg}\n可让 AI 解决冲突（点「🤖 让 AI 合并」），或点「放弃合并」回退。`);
      if (st) patchChat(sid, { worktreeStatus: st });
      void refreshWorktreeConflicts(sid);
      bumpTree();
    }
  }, [patchChat, pushLog]);

  /** 拉取主工作区的冲突文件列表（合并中状态下）。 */
  const refreshWorktreeConflicts = useCallback(async (sid: string) => {
    try {
      const r = await api.worktreeConflicts();
      patchChat(sid, { worktreeConflicts: r.conflicts });
    } catch {
      /* 无冲突接口失败不阻塞 */
    }
  }, [patchChat]);

  /** 评审卡：让 AI 合并（在主工作区起任务：git merge + 冲突解决 + 完成提交）。 */
  const handleWorktreeAiMerge = useCallback((sid: string, name: string) => {
    if (getChat(sid).running) {
      window.alert("任务运行中，请先等待或停止后再执行合并。");
      return;
    }
    const cur = getChat(sid);
    const branch = `worktree-${name}`;
    // 可见的用户气泡：让用户知道接下来这段是"合并任务"，而不是 Agent 自说自话
    patchChat(sid, {
      worktreeConflicts: [],
      messages: [...(cur.messages ?? []), {
        role: "user",
        content: `🤖 让 AI 合并隔离工作树分支 \`${branch}\`（执行 git merge，遇冲突逐个解决后完成合并提交）。`,
      }],
    });
    pushLog("🤖 已交给 AI 合并：Agent 将执行 git merge 并（如有冲突）逐个解决后完成合并提交");
    taskLauncherRef.current(sid, `请合并隔离工作树分支 ${branch}。`, undefined, name);
  }, [getChat, patchChat, pushLog]);

  /** 评审卡：放弃进行中的合并（git merge --abort）。 */
  const handleWorktreeAbortMerge = useCallback(async (sid: string) => {
    try {
      await api.worktreeAbortMerge();
      patchChat(sid, { worktreeConflicts: [] });
      pushLog("🛡️ 已放弃合并，主工作区回到合并前状态");
      bumpTree();
      const st = await api.worktreeStatus(sid).catch(() => null);
      if (st?.exists) patchChat(sid, { worktreeStatus: st });
    } catch (e) {
      pushLog(`✗ 放弃合并失败: ${(e as Error).message}`);
    }
  }, [patchChat, pushLog]);

  /** 评审卡：丢弃 worktree（删目录 + 分支，主工作区毫发无伤）。 */
  const handleWorktreeDiscard = useCallback(async (sid: string) => {
    if (!window.confirm("确定丢弃隔离工作树的全部改动？此操作不可恢复（主工作区不受影响）。")) return;
    try {
      await api.worktreeDiscard(sid);
      patchChat(sid, { worktreeStatus: null });
      pushLog("🛡️ 隔离工作树已丢弃");
      bumpTree();
      const t = tabsRef.current.find((t) => t.sessionId === sid);
      if (t) setSuccess("隔离工作树已丢弃");
      setTimeout(() => setSuccess(null), 2500);
    } catch (e) {
      pushLog(`✗ 丢弃失败: ${(e as Error).message}`);
    }
  }, [patchChat, pushLog]);

  // ------------------------------------------------------------ 手动压缩（面板环形 / /compact 命令）

  const compactNow = useCallback(async (sid: string, focus: string) => {
    if (!sid) {
      window.alert("当前没有会话，无法压缩上下文。");
      return null;
    }
    if (getChat(sid).running) {
      window.alert("任务运行中无法压缩，请先停止任务。");
      return null;
    }
    setCompactingSessions((m) => ({ ...m, [sid]: true }));
    try {
      const r = await api.compact(sid, focus);
      const cur = getChat(sid);
      const history = [...(cur.contextHistory ?? []), { p: r.after_tokens }].slice(-60);
      const [stats, snap] = await Promise.all([
        api.contextStats(sid).catch(() => null),
        api.getSession(sid).catch(() => null),
      ]);
      patchChat(sid, {
        contextStats: {
          ...(stats ?? cur.contextStats ?? { model: "", context_window: 0, session: {} }),
          task: { ...(cur.contextStats?.task ?? {}), last_prompt_tokens: r.after_tokens } as ContextTaskStats,
        } as ContextStats,
        contextHistory: history,
        messages: snap?.messages ?? cur.messages,
      });
      pushLog(`🗜️ 手动压缩完成：${r.before_tokens} → ${r.after_tokens} tokens（释放 ${r.removed_tokens}，折叠 ${r.turns_compacted} 轮）`);
      return r;
    } catch (e) {
      patchChat(sid, { error: (e as Error).message });
      pushLog(`✗ 手动压缩失败: ${(e as Error).message}`);
      return null;
    } finally {
      setCompactingSessions((m) => { const n = { ...m }; delete n[sid]; return n; });
    }
  }, [getChat, patchChat, pushLog]);

  const runCompact = useCallback(
    async (raw: string) => {
      const sid = activeSessionId;
      if (!sid) return;
      if (getChat(sid).running) {
        window.alert("任务运行中无法压缩，请先停止任务。");
        return;
      }
      const focus = raw.replace(/^\/compact\s*/i, "").trim();
      patchChat(sid, { messages: [...(getChat(sid).messages ?? []), { role: "user", content: raw }], error: null });
      pushLog("🗜️ 正在压缩会话上下文…");
      const r = await compactNow(sid, focus);
      if (r) {
        const cur = getChat(sid);
        patchChat(sid, {
          messages: [...cur.messages, {
            role: "assistant",
            content: `🗜️ 上下文已压缩：${r.before_tokens} → ${r.after_tokens} tokens（释放 ${r.removed_tokens}，折叠 ${r.turns_compacted} 轮、保留最近 ${r.keep_turns} 轮）。`,
          }],
        });
        pushLog(`🗜️ /compact 完成：${r.before_tokens} → ${r.after_tokens} tokens`);
      }
    },
    [activeSessionId, getChat, patchChat, pushLog, compactNow]
  );

  const handlePanelCompact = useCallback(() => {
    if (!activeSessionId || compactingSessions[activeSessionId]) return;
    pushLog("🗜️ 正在压缩会话上下文…");
    void compactNow(activeSessionId, "");
  }, [activeSessionId, compactingSessions, compactNow, pushLog]);

  const send = useCallback(
    async (prompt: string) => {
      if (!status?.workspace) {
        window.alert("请先打开项目后再开始对话。");
        void openProject();
        return;
      }
      if (prompt.trim().toLowerCase().startsWith("/compact")) {
        // 本地命令：不走任务链路，直接压缩会话
        await runCompact(prompt);
        return;
      }
      const trimmedCmd = prompt.trim().toLowerCase();
      if (trimmedCmd === "/goal" || trimmedCmd.startsWith("/goal ")) {
        await runGoalCommand(prompt);
        return;
      }
      if (trimmedCmd === "/loop" || trimmedCmd.startsWith("/loop ")) {
        runLoopCommand(prompt);
        return;
      }
      if (trimmedCmd === "/continue" || trimmedCmd.startsWith("/continue ")) {
        runContinueCommand(prompt);
        return;
      }
      if (trimmedCmd === "/worktree" || trimmedCmd.startsWith("/worktree ")) {
        void runWorktreeCommand(prompt);
        return;
      }
      let sid = activeTabId ? tabsRef.current.find((t) => t.id === activeTabId)?.sessionId : null;
      // 会话态走 getChat（chatStatesRef 最新）/ draft 走对应 ref：避免 useCallback
      // 闭包捕获旧值导致「切换模型/推理强度后发送仍带旧档位」的回归
      const selectedModel = activeSessionId
        ? (getChat(activeSessionId).modelOverride ?? null)
        : (activeTabId ? draftModelsRef.current[activeTabId] ?? null : null);
      const reasoningEffort = activeSessionId
        ? (getChat(activeSessionId).reasoningEffort ?? "")
        : (activeTabId ? draftReasoningRef.current[activeTabId] ?? "" : "");
      let createdSession = false;
      if (!sid) {
        // 当前 tab 尚无 session（newChatTab 的占位），首次发送时创建并绑定到该 tab
        const { session_id } = await api.createSession();
        sid = session_id;
        createdSession = true;
        patchChat(session_id, { ...EMPTY_CHAT, messages: [] });
        // 暂存的推理强度随 session 创建写入（同模型/协作模式的处理），
        // 否则首条消息后档位丢失、会话内后续消息回到供应商默认
        if (reasoningEffort) patchChat(session_id, { reasoningEffort });
        setTabs((prev) => prev.map((t) =>
          t.id === activeTabId ? { ...t, sessionId: session_id, modelOverride: selectedModel } : t
        ));
        if (selectedModel) {
          // 必须把后端确认的 override 写回会话状态，否则 Composer 读 currentChat.modelOverride
          // 会回退显示“系统默认”（而实际后端一直在用 override 执行任务，显示与事实不符）
          const resp = await api.setSessionModel(session_id, selectedModel);
          patchChat(session_id, { modelOverride: resp.override, effectiveModel: resp.effective });
        }
        // 暂存的协作模式随 session 创建写入（首条消息即按该模式编排）
        const draftCollab = draftCollabModes[activeTabId];
        if (draftCollab) {
          try {
            await api.setSessionCollab(session_id, draftCollab);
            patchChat(session_id, { collabMode: draftCollab });
          } catch {
            // 写入失败：不加 UI 状态，后端按默认模式执行
          }
        }
      }
      const base = getChat(sid);
      // SSE 连接（新任务提交与排队竞态续接共用，统一走 connectTaskStream：
      // 含断线重连与 [DONE] 收尾）
      const connect = (taskId: string) => connectTaskStream(sid, taskId);

      if (base.running) {
        // 任务运行中：只加入本地待发送队列（不调用后端），由用户点击队列项 ➤
        // 单条发送，或任务结束后自动逐条发送
        patchChat(sid, { pendingQueue: [...(base.pendingQueue ?? []), prompt] });
        pushLog(`➥ 已加入待发送队列（${(base.pendingQueue ?? []).length + 1} 条）`);
        return;
      } else {
        patchChat(sid, {
          messages: [...(base.messages ?? []), { role: "user", content: prompt }],
          error: null,
          running: true,
        });
        cancelStreamFlush(sid);
        streamingRefs.current.set(sid, { items: [] });
        patchChat(sid, { streaming: { items: [] } });
      }

      try {
        pushLog("➤ 提交任务…");
        currentAgentRef.current = currentAgent;
        const resp = await api.chat(sid, prompt, currentAgent, reasoningEffort);
        if (resp.queued) {
          // 后端确认入队：当前任务继续跑，不覆盖 taskIds/SSE 连接
          pushLog("➥ 补充指令已入队，将在当前任务下一回合生效");
          return;
        }
        const { task_id } = resp;
        taskIdsRef.current.set(sid, task_id);
        if (stopRequestedRef.current.has(sid)) {
          stopRequestedRef.current.delete(sid);
          pushLog("■ 任务已提交，立即请求停止…");
          await api.stopTask(task_id).catch(() => {});
        }
        connect(task_id);
      } catch (e) {
        // 创建 session 后提交失败时不留下空历史记录
        if (createdSession && sid) {
          await api.deleteSession(sid).catch(() => {});
          setTabs((prev) => prev.map((tab) =>
            tab.sessionId === sid ? { ...tab, sessionId: undefined, title: "新会话" } : tab
          ));
          void refreshSessions();
        }
        patchChat(sid, { error: (e as Error).message, running: false, streaming: null });
        pushLog(`✗ 提交失败: ${(e as Error).message}`);
      }
    },
    [activeTabId, activeSessionId, draftCollabModes, getChat, patchChat, refreshSessions, cancelStreamFlush, pushLog, currentAgent, openProject, status?.workspace, runCompact, runGoalCommand, runLoopCommand, runContinueCommand, runWorktreeCommand]
  );

  // 发送队列中的单条指令到当前运行任务（queue_input 注入下一回合）
  const sendQueuedItem = useCallback((sid: string, item: string) => {
    const q = getChat(sid);
    if (!q) return;
    // 乐观上屏（带 queued 标记）
    patchChat(sid, {
      messages: [...(q.messages ?? []), { role: "user", content: item, queued: true }],
      error: null,
    });
    void api.chat(sid, item, currentAgent, q.reasoningEffort ?? "").then((resp) => {
      if (!resp.queued) {
        // 竞态：提交瞬间任务恰好结束，后端把它作为新任务启动 → 接上 SSE
        pushLog("➥ 当前任务已结束，补充指令作为新任务启动");
        const cur = getChat(sid);
        patchChat(sid, {
          messages: cur.messages.map((m) =>
            m.queued && m.content === item ? { ...m, queued: false } : m
          ),
          running: true,
          streaming: { items: [] },
        });
        cancelStreamFlush(sid);
        streamingRefs.current.set(sid, { items: [] });
        taskIdsRef.current.set(sid, resp.task_id);
        connectTaskStream(sid, resp.task_id);
      }
    }).catch(() => {
      pushLog("⚠ 补充指令提交失败，请重试");
    });
  }, [cancelStreamFlush, connectTaskStream, currentAgent, getChat, handleSSEEvent, patchChat, pushLog]);

  // 更新 taskLauncherRef：供 handleSSEEvent 在 task:done 时自动发送下一条
  taskLauncherRef.current = (targetSid, prompt, effort, mergeWorktree) => {
    const reasoning = effort ?? getChat(targetSid)?.reasoningEffort ?? "";
    patchChat(targetSid, {
      running: true,
      streaming: { items: [] },
      error: null,
      unfinished: false, // 新任务启动：清除上一任务的未完成提示（自动继续/手动继续均已接管）
    });
    cancelStreamFlush(targetSid);
    streamingRefs.current.set(targetSid, { items: [] });
    void (async () => {
      try {
        currentAgentRef.current = currentAgent;
        const resp = await api.chat(targetSid, prompt, currentAgent, reasoning, mergeWorktree);
        const { task_id } = resp;
        taskIdsRef.current.set(targetSid, task_id);
        connectTaskStream(targetSid, task_id);
      } catch (e) {
        patchChat(targetSid, { error: (e as Error).message, running: false, streaming: null });
        pushLog(`✗ 自动发送失败: ${(e as Error).message}`);
      }
    })();
  };

  const stop = useCallback(async () => {
    const sid = activeSessionId;
    if (!sid) return;
    const tid = taskIdsRef.current.get(sid);
    stopRequestedRef.current.add(sid);
    pushLog("■ 请求停止任务…");
    if (!tid) {
      patchChat(sid, { running: false, streaming: null });
      pushLog("■ 任务尚未提交，已取消发送");
      return;
    }
    try {
      await api.stopTask(tid);
      patchChat(sid, { running: false, streaming: null, pendingApprovals: [] });
      pushLog("■ 停止请求已发送");
    } catch (e) {
      pushLog(`✗ 停止失败: ${(e as Error).message}`);
    }
  }, [activeSessionId, patchChat, pushLog]);

  // Esc：停止当前任务（对齐 opencode 的中断键位，与输入区「停止」按钮同一路径）。
  // 防误触：要求连按两次——首次 Esc 只进入「待确认」态（提示行出现「再按一次 Esc
  // 停止任务」），ESC_STOP_CONFIRM_MS 窗口内再按一次才真正停止；超时自动解除。
  // 让位规则（避免与其它 Esc 交互打架）：
  //   · 设置 / 关于弹窗打开时——交给弹窗自身的 Esc 关闭；
  //   · 事件已被 preventDefault（命令面板、输入历史、右键菜单、行内重命名等
  //     已占用 Esc 的交互）——那不计数、也不清掉待确认态；
  //   · 当前会话没有运行中的任务——无操作。
  // 注：Composer / Sidebar 的 Esc 分支都在 React 合成事件里 preventDefault，
  // 原生事件冒泡到 window 时仍是 defaultPrevented=true，故此处可靠。
  const [escStopArmed, setEscStopArmed] = useState(false);
  // 待确认态同时存一份 ref：判定「这是第几次 Esc」必须用 ref 而非 state——
  // 两次按键可能极快，window 监听器的闭包未必来得及随 state 重新注册（effect 是异步的）。
  // state 只负责驱动提示行文案。
  const escStopArmedRef = useRef(false);
  const escStopTimerRef = useRef<number | null>(null);
  const disarmEscStop = useCallback(() => {
    if (escStopTimerRef.current !== null) {
      window.clearTimeout(escStopTimerRef.current);
      escStopTimerRef.current = null;
    }
    escStopArmedRef.current = false;
    setEscStopArmed(false);
  }, []);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.ctrlKey || e.altKey || e.metaKey || e.shiftKey) return;
      if (e.defaultPrevented) return;
      if (showSettings || showAbout) return;
      if (!currentChat.running) return;
      e.preventDefault();
      if (escStopArmedRef.current) {
        disarmEscStop();
        void stop();
        return;
      }
      // 首次 Esc：只武装，不停止
      escStopArmedRef.current = true;
      setEscStopArmed(true);
      pushLog(`■ 再按一次 Esc 停止任务（${ESC_STOP_CONFIRM_MS / 1000}s 内）`);
      if (escStopTimerRef.current !== null) window.clearTimeout(escStopTimerRef.current);
      escStopTimerRef.current = window.setTimeout(() => {
        escStopTimerRef.current = null;
        disarmEscStop();
      }, ESC_STOP_CONFIRM_MS);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [showSettings, showAbout, currentChat.running, stop, disarmEscStop, pushLog]);

  // 待确认态跟着「运行中 + 当前会话」走：任务结束或切了会话/标签就解除，
  // 避免在 A 会话按过一次 Esc，切到 B 会话后一次按键就把 B 的任务停掉。
  useEffect(() => {
    disarmEscStop();
  }, [currentChat.running, activeSessionId, disarmEscStop]);

  /** Agents 看板手动取消子 Agent：调 REST 关闭（后端广播 agent:closed by=user），
   *  本地乐观置终态——SSE 只在主任务运行时存在，不能依赖事件到达。 */
  const handleCloseAgent = useCallback(async (agentId: string) => {
    const sid = activeSessionId;
    if (!sid) return;
    const chat = getChat(sid);
    patchChat(sid, {
      agentBoard: (chat.agentBoard ?? []).map((a) => a.subagentId === agentId && a.status === "running"
        ? { ...a, status: "done" as const, summary: a.summary ?? "已被用户取消" }
        : a),
    });
    pushLog(`■ 已请求取消子 Agent ${agentId}`);
    try {
      await api.closeSubAgent(sid, agentId);
    } catch (e) {
      pushLog(`✗ 取消失败: ${(e as Error).message}`);
    }
  }, [activeSessionId, getChat, patchChat, pushLog]);

  const approve = useCallback(
    async (approvalId: string, approved: boolean, remember = false) => {
      patchActiveChat({ pendingApprovals: (currentChat.pendingApprovals ?? []).filter((p) => p.id !== approvalId) });
      try {
        const r = await api.approve(approvalId, approved, {
          remember,
          sessionId: activeSessionId,
        });
        if (r?.remembered?.ok) {
          pushLog("⚡ 已记住：本会话内同类操作将自动允许");
        }
      } catch {
        /* ignore */
      }
    },
    [currentChat.pendingApprovals, patchActiveChat, activeSessionId, pushLog]
  );

  const answerQuestion = useCallback(
    async (questionId: string, answer: string) => {
      // 先本地移除（快速反馈），再通知后端 resolve
      patchActiveChat({
        pendingQuestions: (currentChat.pendingQuestions ?? []).filter((q) => q.id !== questionId),
      });
      try {
        await api.answerQuestion(questionId, answer);
      } catch {
        /* ignore */
      }
    },
    [currentChat.pendingQuestions, patchActiveChat]
  );

  const activeSessionTitle = useMemo(() => {
    if (!activeSessionId) return "";
    return sessions.find((s) => s.session_id === activeSessionId)?.title ?? "新会话";
  }, [activeSessionId, sessions]);

  if (loading) {
    return (
      <div className="app">
        <div className="drag-region" />
        <div className="loading-screen">
          <div className="loading-spinner" />
          <p>正在连接后端服务…</p>
        </div>
      </div>
    );
  }

  if (!status && currentChat.error) {
    return (
      <div className="app">
        <div className="drag-region" />
        <div className="crash-screen">
          <div className="crash-icon">⚠️</div>
          <h2>无法连接后端服务</h2>
          <p className="crash-message">{currentChat.error}</p>
          <div className="crash-actions">
            <button onClick={() => { patchActiveChat({ error: null }); setLoading(true); void refreshAll(); }}>
              🔄 重试
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div
      className="app"
      style={
        {
          "--sidebar-w": sidebarCollapsed ? "0px" : `${sidebarResize.size}px`,
          "--tool-w": toolPanelCollapsed ? "0px" : `${toolPanelResize.size}px`,
        } as React.CSSProperties
      }
    >
      <div className="drag-region" />
      {metaNotice && !metaNoticeClosed && (
        <div className="meta-notice" role="status">
          <span className="meta-notice-icon">ⓘ</span>
          <span className="meta-notice-text">
            {metaNotice.cached
              ? `模型元数据已 ${metaNotice.ageDays} 天未同步（超过 7 天保鲜期），上下文窗口与定价可能失真。`
              : "模型元数据尚未同步，上下文窗口将按供应商默认值（通常 128K）估算。"}
          </span>
          <button
            className="meta-notice-sync"
            disabled={metaSyncing}
            onClick={() => void syncModelMeta()}
          >
            {metaSyncing ? "同步中…" : "立即同步"}
          </button>
          {metaSyncError && <span className="meta-notice-error">{metaSyncError}</span>}
          <button
            className="meta-notice-close"
            onClick={() => setMetaNoticeClosed(true)}
            title="本次运行不再提醒"
          >
            ✕
          </button>
        </div>
      )}
      {/* 三栏横排容器：与顶部横幅分开层级——横幅独占一行，不再参与横向宽度分配
          （此前横幅是 .app 的横向 flex 项，会吃掉数百 px 行宽把聊天区挤成窄条） */}
      <div className="app-row">
      {sidebarCollapsed && (
        <button className="panel-restore-bar left" onClick={() => setSidebarCollapsed(false)} title="展开侧边栏">
          <span className="restore-icon">▶</span>
        </button>
      )}
      {!sidebarCollapsed && (
      <ErrorBoundary name="侧边栏" compact>
      <Sidebar
        sessions={sessions}
        activeSessionId={activeSessionId}
        workspace={status?.workspace ?? "未打开项目"}
        tab={sidebarTab}
        treeRevision={treeRevision}
        outputRevision={outputRevision}
        version={status?.version ?? "?"}
        recentProjects={recentProjects}
        projectsView={projectsView}
        projectKind={
          (recentProjects.find((p) => p.path === status?.workspace)?.kind) ?? "project"
        }
        collapsed={sidebarCollapsed}
        onToggleCollapsed={() => setSidebarCollapsed((v) => !v)}
        onTabChange={changeSidebarTab}
        onSelectSession={(id) => void selectSession(id)}
        onOpenSessionWithProject={(id) => void openSessionWithProject(id)}
        onNewSession={requestNewChat}
        onDeleteSession={(id) => void deleteSession(id)}
        onDeleteSessions={(ids) => void deleteSessions(ids)}
        onOpenProject={openProjectEntry}
        onOpenCode={openCode}
        onNewProject={newProjectEntry}
        onOpenRecent={(path) => void openRecentProject(path)}
        onRemoveRecent={(path) => void removeRecentProject(path)}
        onTogglePin={(path) => void togglePinProject(path)}
        onToggleKind={(path, kind) => void toggleProjectKind(path, kind)}
        onOpenWorktree={(path) => void openWorktreeSession(path)}
        onOpenWorktreeSession={(sessionId) => void selectSession(sessionId)}
        onBackToProjects={backToProjects}
        onOpenProjectNewWindow={() => void openProjectNewWindow()}
        onOpenSettings={() => setShowSettings(true)}
        onOpenAbout={() => setShowAbout(true)}
        onFileOpen={(p, opts) => void openFileTab(p, opts)}
        onDirOpen={(p) => void openDirInSystem(p)}
      />
      </ErrorBoundary>
      )}
      {!sidebarCollapsed && (
      <div
        className="resizer col"
        title="拖拽调整侧边栏宽度（双击重置）"
        onPointerDown={sidebarResize.startDrag}
        onDoubleClick={sidebarResize.reset}
      />
      )}
      <main className="main">
        <ErrorBoundary name="聊天区">
        <TabBar
          tabs={tabs}
          activeTabId={activeTabId}
          onSelect={(id) => setActiveTabId(id)}
          onClose={closeTab}
          sseStates={tabSseStates}
        />
        {activeTab?.kind === "file" ? (
          <FileViewer tab={activeTab} />
        ) : (
          <>
            {/* 多 Agent 协作状态条（有派生记录时出现）：点击跳转右侧 Agents 看板 */}
            {(currentChat.agentBoard?.length ?? 0) > 0 && (() => {
              const board = currentChat.agentBoard ?? [];
              const running = board.filter((a) => a.status === "running").length;
              const done = board.length - running;
              const hasReview = board.some((a) => a.changedFiles && a.changedFiles.length > 0);
              return (
                <button
                  className="agent-status-bar"
                  onClick={() => { setToolPanelTab("agents"); setToolPanelCollapsed(false); }}
                  title="打开 Agents 协作看板（运行中/已完成/待审查）"
                >
                  <span className={`agent-status-dot ${running > 0 ? "live" : "idle"}`} />
                  🤖 Agents · {running > 0 ? `${running} 运行中` : "全部完成"}
                  {done > 0 && ` · ${done} 已完成`}
                  {hasReview && <span className="agent-status-review">⚠ 待过目</span>}
                  <span className="agent-status-open">看板 →</span>
                </button>
              );
            })()}
            <ChatView
              sessionId={activeSessionId ?? "（未选择）"}
              sessionTitle={activeSessionTitle}
              messages={currentChat.messages}
              streaming={currentChat.streaming}
              running={currentChat.running}
              turn={currentChat.turn}
              goal={currentChat.goal}
              loop={
                currentChat.loopEnabled
                  ? { count: currentChat.loopCount ?? 0, max: currentChat.loopMax ?? 10 }
                  : null
              }
              pendingApprovals={currentChat.pendingApprovals}
              subAgentRecords={currentChat.subAgentRecords}
              skillLoaded={currentChat.skillLoaded}
              onSend={(p) => void send(p)}
              onStop={stop}
              onApprove={(id, a) => void approve(id, a)}
              currentAgent={currentAgent}
              unfinished={currentChat.unfinished}
              onContinue={() => void send(CONTINUE_PROMPT)}
              worktreeEnabled={currentChat.worktreeEnabled}
              worktreeStatus={currentChat.worktreeStatus ?? null}
              worktreeConflicts={currentChat.worktreeConflicts ?? []}
              worktreeMerge={currentChat.worktreeMerge ?? null}
              onWorktreeDiff={() => activeSessionId && void handleWorktreeDiff(activeSessionId)}
              onWorktreeMerge={() => activeSessionId && void handleWorktreeMerge(activeSessionId)}
              onWorktreeAiMerge={() => activeSessionId && handleWorktreeAiMerge(activeSessionId, activeSessionId)}
              onWorktreeAbortMerge={() => activeSessionId && void handleWorktreeAbortMerge(activeSessionId)}
              onWorktreeDiscard={() => activeSessionId && void handleWorktreeDiscard(activeSessionId)}
              foldTurns={uiConfig?.chat_fold_turns}
              foldMessages={uiConfig?.chat_fold_messages}
              onStickChange={handleStickChange}
            />
            <QuestionBar
              pendingQuestions={currentChat.pendingQuestions ?? []}
              onAnswerQuestion={(id, answer) => void answerQuestion(id, answer)}
            />
            {currentChat.pendingQueue && currentChat.pendingQueue.length > 0 && (
              <PendingQueue
                items={currentChat.pendingQueue}
                onRemove={(idx) => {
                  const q = getChat(activeSessionId!);
                  patchChat(activeSessionId!, { pendingQueue: q.pendingQueue.filter((_, i) => i !== idx) });
                }}
                onReorder={(from, to) => {
                  const q = getChat(activeSessionId!);
                  const next = [...q.pendingQueue];
                  const [moved] = next.splice(from, 1);
                  next.splice(to, 0, moved);
                  patchChat(activeSessionId!, { pendingQueue: next });
                }}
                onSend={(idx) => {
                  const q = getChat(activeSessionId!);
                  const item = q.pendingQueue[idx];
                  if (!item) return;
                  patchChat(activeSessionId!, { pendingQueue: q.pendingQueue.filter((_, i) => i !== idx) });
                  if (currentChat.running) {
                    sendQueuedItem(activeSessionId!, item);
                  } else {
                    void send(item);
                  }
                }}
              />
            )}
            <Composer
              running={currentChat.running}
              agents={agents}
              collabModes={collabModes}
              sessionCollabMode={activeSessionId ? currentChat.collabMode ?? null : (activeTabId ? draftCollabModes[activeTabId] ?? null : null)}
              onSessionCollabMode={setSessionCollabMode}
              onCollabModesRefresh={refreshCollabModes}
              currentAgent={currentAgent}
              onSelectAgent={setCurrentAgent}
              onSend={(p) => void send(p)}
              onStop={stop}
              llmConfig={llmConfig}
              providerMeta={providerMeta}
              sessionModel={activeSessionId ? currentChat.modelOverride ?? null : (draftModels[activeTabId] ?? null)}
              onSessionModelChange={(model) => void setSessionModel(model)}
              reasoningEffort={activeSessionId ? (currentChat.reasoningEffort ?? "") : (activeTabId ? draftReasoning[activeTabId] ?? "" : "")}
              onReasoningEffortChange={(v) => {
                if (activeSessionId) patchChat(activeSessionId, { reasoningEffort: v });
                else if (activeTabId) setDraftReasoning((p) => ({ ...p, [activeTabId]: v }));
              }}
              tabCount={tabs.length}
              scrolledUp={chatScrolledUp}
              stopArmed={escStopArmed}
            />
            <button className="debug-toggle" onClick={() => setShowDebug(!showDebug)} title="调试日志">
              {showDebug ? "隐藏日志" : "日志"}
            </button>
          </>
        )}
        {currentChat.stalled && currentChat.running && (
          <div className="error-banner stalled">
            <span>⚠ 任务长时间无响应（可能 LLM 超时或网络问题）</span>
            <button onClick={stop}>■ 停止任务</button>
          </div>
        )}
        {currentChat.error && (
          <div className="error-banner">
            <span>⚠ {currentChat.error}</span>
            <button onClick={() => patchActiveChat({ error: null })}>✕</button>
          </div>
        )}
        {success && (
          <div className="success-banner">
            <span>✅ {success}</span>
            <button onClick={() => setSuccess(null)}>✕</button>
          </div>
        )}
        {showDebug && (
          <div className="debug-panel">
            <div className="debug-title">调试日志</div>
            <div className="debug-body">
              {debugLogs.length === 0 ? (
                <div className="debug-empty">暂无日志</div>
              ) : (
                debugLogs.map((l, i) => <div key={i} className="debug-line">{l}</div>)
              )}
            </div>
          </div>
        )}
        </ErrorBoundary>
      </main>
      {activeTab?.kind === "chat" && toolPanelCollapsed && (
        <button className="panel-restore-bar right" onClick={() => setToolPanelCollapsed(false)} title="展开工具面板">
          <span className="restore-icon">◀</span>
        </button>
      )}
      {activeTab?.kind === "chat" && !toolPanelCollapsed && (
        <>
          <div
            className="resizer col"
            title="拖拽调整面板宽度（双击重置）"
            onPointerDown={toolPanelResize.startDrag}
            onDoubleClick={toolPanelResize.reset}
          />
          <ErrorBoundary name="工具面板" compact>
          <ToolPanel
            contextStats={currentChat.contextStats}
            contextHistory={currentChat.contextHistory}
            liveSpeed={currentChat.liveSpeed ?? null}
            running={currentChat.running}
            mcpServers={mcpServers}
            tools={registeredTools}
            todos={currentChat.todos}
            agentBoard={currentChat.agentBoard}
            orchestrator={{ agentId: currentAgent, running: currentChat.running }}
            activeTab={toolPanelTab}
            onTabChange={setToolPanelTab}
            backgroundTasks={backgroundTasks}
            collapsed={toolPanelCollapsed}
            onToggleCollapsed={() => setToolPanelCollapsed((v) => !v)}
            onCloseAgent={(id) => void handleCloseAgent(id)}
            onKillBackground={(id) => void api.killBackgroundTask(id).then(() => {
              setBackgroundTasks((prev) => prev.filter((t) => t.task_id !== id));
            }).catch(() => {})}
            onCompact={activeSessionId && !currentChat.running ? handlePanelCompact : undefined}
            compacting={activeSessionId ? !!compactingSessions[activeSessionId] : false}
          />
          </ErrorBoundary>
        </>
      )}
      </div>
      {showSettings && (
        <SettingsModal
          onClose={() => setShowSettings(false)}
          onSaved={() => { void refreshAll(); }}
        />
      )}
      {showAbout && (
        <AboutModal
          onClose={() => setShowAbout(false)}
          serverVersion={status?.version ?? null}
        />
      )}
      {showPicker && (
        <ProjectPicker
          initialPath={status?.workspace ?? ""}
          initialCreate={pickerMode === "new-code" || pickerMode === "new-project"}
          createMode={pickerMode === "new-code" ? "code" : "project"}
          onClose={() => setShowPicker(false)}
          onSelect={(p) => void selectProject(p)}
        />
      )}
    </div>
  );
}
