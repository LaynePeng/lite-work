// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import AboutModal from "./components/AboutModal";
import ChatView, { QuestionBar } from "./components/ChatView";
import Composer from "./components/Composer";
import ErrorBoundary from "./components/ErrorBoundary";
import PendingQueue from "./components/PendingQueue";
import FileViewer from "./components/FileViewer";
import ProjectPicker from "./components/ProjectPicker";
import SettingsModal from "./components/SettingsModal";
import Sidebar from "./components/Sidebar";
import TabBar from "./components/TabBar";
import ToolPanel from "./components/ToolPanel";
import { useResizable } from "./hooks/useResizable";
import type { AgentInfo, BackgroundTaskInfo, ChatSessionState, CollabMode, LLMConfig, LLMProviderMeta, MCPServerStatus, Msg, ServerStatus, SessionInfo, SessionModel, SseConnState, SubAgentProgress, SubAgentStep, TabItem, ToolCardInfo, WorkItem } from "./types";
import { baseName } from "./lib/path";

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

let tabSeq = 0;
const nextTabId = () => `tab_${++tabSeq}`;

const EMPTY_CHAT: ChatSessionState = {
  messages: [],
  streaming: null,
  running: false,
  turn: 0,
  stats: null,
  contextStats: null,
  error: null,
  pendingApprovals: [],
  subAgentRecords: [],
  agentBoard: [],
  stalled: false,
  sseState: "idle",
  todos: [],
  pendingQuestions: [],
  pendingQueue: [],
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
  const [success, setSuccess] = useState<string | null>(null);
  const [treeRevision, setTreeRevision] = useState(0);
  const [outputRevision, setOutputRevision] = useState(0);
  const [draftModels, setDraftModels] = useState<Record<string, SessionModel | null>>({});
  const [draftReasoning, setDraftReasoning] = useState<Record<string, string>>({});
  const [mcpServers, setMcpServers] = useState<MCPServerStatus[]>([]);
  const [registeredTools, setRegisteredTools] = useState<{ name: string; description: string }[]>([]);
  // 已安装协作模式（对话框选择器数据源；设置里安装新插件后随 refreshAll 更新）
  const [collabModes, setCollabModes] = useState<CollabMode[]>([]);
  const refreshCollabModes = useCallback(() => {
    api.collabModes().then((r) => setCollabModes(r.modes)).catch(() => {});
  }, []);
  const [backgroundTasks, setBackgroundTasks] = useState<BackgroundTaskInfo[]>([]);
  // 面板折叠状态：默认展开（false=展开）
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [toolPanelCollapsed, setToolPanelCollapsed] = useState(false);
  // 工具面板 tab 受控（聊天区 Agents 状态条可跳转）
  const [toolPanelTab, setToolPanelTab] = useState<"context" | "todos" | "agents" | "mcp" | "background" | "tools">("context");

  // 布局边界拖拽：侧边栏 / 右侧工具面板宽度（双击分隔条重置，localStorage 持久化）
  const sidebarResize = useResizable({
    axis: "col", initial: 280, min: 200,
    max: () => Math.min(520, Math.floor(window.innerWidth * 0.45)),
    storageKey: "litework.sidebarWidth.v2",
  });
  const toolPanelResize = useResizable({
    // 右侧工具面板默认约为窗口宽度的 25%（从中间聊天区压缩）
    axis: "col", initial: Math.round((typeof window !== "undefined" ? window.innerWidth : 1440) * 0.25), min: 260,
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
  // SSE 重连控制器（P1-5）：每会话一个 {taskId, attempts, timer}
  const streamCtlRef = useRef<Map<string, { taskId: string; attempts: number; timer: number | null }>>(new Map());
  const chatStatesRef = useRef<Record<string, ChatSessionState>>({});
  const tabsRef = useRef<TabItem[]>([]);
  const sessionsRequestRef = useRef(0);
  // 任务启动器 ref：供 handleSSEEvent 在 task:done 时自动发送下一条（避免循环依赖）
  const taskLauncherRef = useRef<(sid: string, prompt: string, effort?: string) => void>(() => {});

  useEffect(() => { chatStatesRef.current = chatStates; }, [chatStates]);
  useEffect(() => { tabsRef.current = tabs; }, [tabs]);

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

  const refreshSessions = useCallback(async (ws?: string) => {
    const requestId = ++sessionsRequestRef.current;
    try {
      const next = await api.sessions(ws ?? status?.workspace ?? undefined);
      // 多个任务结束/切换项目时请求可能乱序返回，旧响应不能覆盖新列表。
      if (requestId === sessionsRequestRef.current) setSessions(next);
    } catch {
      /* ignore */
    }
  }, [status?.workspace]);

  const refreshAll = useCallback(async () => {
    try {
      const [st, ag, llm, providers, mcp, tools, modes] = await Promise.all([
        api.status(), api.agents(), api.llmConfig(), api.llmProviders(), api.mcpStatus(),
        // 工具列表按当前 Agent 裁剪；workspace 未就绪时报 409，静默降级为空列表
        api.tools(currentAgent).catch(() => [] as { name: string; description: string }[]),
        api.collabModes().catch(() => ({ modes: [] as CollabMode[] })),
      ]);
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
      if (!sid) return;
      patchChat(sid, { collabMode: mode });
      void api.setSessionCollab(sid, mode).catch(() => {
        // 写回失败不回滚 UI：下次打开会话按服务端状态恢复
      });
    },
    [activeSessionId, patchChat]
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

  // Tab 键切换 agent
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      e.preventDefault();
      setCurrentAgent((prev) => {
        const primary = agents.filter((a) => a.mode !== "subagent");
        if (primary.length < 2) return prev;
        const idx = primary.findIndex((a) => a.id === prev);
        const next = idx < 0 || idx >= primary.length - 1 ? primary[0] : primary[idx + 1];
        return next.id;
      });
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [agents]);

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
    (sid: string, title?: string) => {
      setTabs((prev) => {
        const existing = prev.find((t) => t.kind === "chat" && t.sessionId === sid);
        if (existing) {
          setActiveTabId(existing.id);
          return prev.map((t) => t.id === existing.id ? { ...t, title: title || t.title } : t);
        }
        const tab: TabItem = { id: nextTabId(), kind: "chat", sessionId: sid, title: title || sid };
        setActiveTabId(tab.id);
        return [...prev, tab];
      });
    },
    []
  );

  // 代码/文本类扩展名：文件树点击 → 内置 FileViewer 查看；其余（办公/媒体/压缩包等）
  // → 调系统默认应用打开（覆盖不了那么多文件类型，交给系统）
  const TEXT_LIKE_EXT = new Set([
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".cpp", ".h", ".hpp",
    ".css", ".scss", ".less", ".html", ".htm", ".xml", ".json", ".yaml", ".yml", ".toml",
    ".md", ".markdown", ".txt", ".log", ".sh", ".bash", ".zsh", ".sql", ".rb", ".swift",
    ".kt", ".svelte", ".vue", ".astro", ".ini", ".cfg", ".conf", ".env", ".gitignore",
    ".puml", ".plantuml", ".mmd", ".mermaid", ".csv", ".tsv",
  ]);

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

  const openFileTab = useCallback(async (filePath: string) => {
    const ext = filePath.slice(filePath.lastIndexOf(".")).toLowerCase();
    const isTextLike = TEXT_LIKE_EXT.has(ext) || !filePath.includes(".");

    // 非代码/文本文件：桌面端调系统默认应用打开
    if (!isTextLike) {
      const bridge = window.liteWork;
      if (bridge?.openFile) {
        const r = await bridge.openFile(filePath);
        if (!r.ok) window.alert(`无法打开文件：${r.error ?? filePath}`);
        return;
      }
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

  const selectSession = useCallback(
    async (sid: string) => {
      const info = sessions.find((s) => s.session_id === sid);
      openSessionTab(sid, info?.title || sid);
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
            // 端点现携带会话生效模型与窗口，重开会话即可正确显示，不再置空等待 SSE
            patchChat(sid, {
              contextStats: {
                model: ctx.model || "",
                context_window: ctx.context_window || 0,
                session: ctx.session ?? {},
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

          // 切换项目后只刷新历史会话列表，会话由用户按需新建（首条消息才落盘）
          newChatTab();
          await refreshSessions(res.workspace);
        }
      } catch (e) {
        setErrorPublic((e as Error).message);
      }
    },
    [pushLog, refreshSessions, closeStream, newChatTab, patchActiveChat, changeSidebarTab, notifyElectronWorkspace, refreshRecentProjects, pickerMode]
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
        newChatTab();
        await refreshSessions(res.workspace);
      }
    } catch (e) {
      setErrorPublic((e as Error).message);
    }
  }, [changeSidebarTab, closeStream, newChatTab, notifyElectronWorkspace, refreshRecentProjects, refreshSessions]);

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

  const backToProjects = useCallback(() => setProjectsView("list"), []);

  // 首次启动：打开一个占位会话 tab
  useEffect(() => {
    if (loading || tabsRef.current.length > 0) return;
    newChatTab();
  }, [loading, newChatTab]);

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
            : [...cur.items, { type: "text" as const, id: `s${Date.now()}-${Math.random()}`, content: ev.data.chunk }];
          streamingRefs.current.set(sid, { ...cur, items });
          scheduleStreamFlush(sid);
          break;
        }
        case "llm:turn_start": {
          log(`⟳ 第 ${ev.data.turn} 轮`);
          const cur = streamingRefs.current.get(sid) ?? getChat(sid).streaming ?? { items: [] };
          streamingRefs.current.set(sid, { ...cur, turn: ev.data.turn });
          patchChat(sid, { streaming: { ...streamingRefs.current.get(sid)! } });
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
          if (TREE_TOUCH_TOOLS.has(ev.data.toolName)) setTreeRevision((v) => v + 1);
          if (OFFICE_TOUCH_TOOLS.has(ev.data.toolName) && ev.data.status !== "error") setOutputRevision((v) => v + 1);
          const cur = streamingRefs.current.get(sid) ?? getChat(sid).streaming;
          if (!cur) break;
          // callId 精确匹配（并行安全），缺失时回退 name+running 启发式（兼容旧后端）
          const status: ToolCardInfo["status"] = ev.data.status === "cancelled"
            ? "cancelled"
            : ev.data.status === "error" || (ev.data.result ?? "").startsWith("[Execution Exception]") || (ev.data.result ?? "").startsWith("[Error]")
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
              { id: ev.data.id, action: ev.data.action, reason: ev.data.reason },
            ],
          });
          break;
        }
        case "approval:resolved": {
          const cur = getChat(sid);
          patchChat(sid, {
            pendingApprovals: (cur.pendingApprovals ?? []).filter((p) => p.id !== ev.data.id),
          });
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
          patchChat(sid, { contextStats: ev.data });
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
          setTreeRevision((v) => v + 1);
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
          if (!afterQueue.loopEnabled) break;
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
          setTreeRevision((v) => v + 1);
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
                  status: evData.status === "error" ? "error" : evData.status === "cancelled" ? "cancelled" : "done",
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
                  status: "done",
                  summary: data.summary,
                  tokens: data.tokens_used,
                };
              }
              return {
                ...item,
                card: {
                  ...card,
                  status: "done" as const,
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
          setTreeRevision((v) => v + 1);
          log(`◈ 子 Agent ${String(data.role ?? "general")} 已完成`);
          pushAgentEvent(sid, "completed", data as unknown as Record<string, unknown>);
          break;
        }
        case "agent:closed": {
          // close_agent 主动关闭：从 Agents 看板移除卡片
          const closedData = ev.data as Record<string, unknown>;
          log(`◈ Agent ${String(closedData.agentId ?? "")} 已关闭`);
          pushAgentEvent(sid, "closed", closedData);
          break;
        }
        case "skill:loaded": {
          const names = ev.data.names ?? [];
          if (names.length > 0) {
            patchChat(sid, { skillLoaded: names });
          }
          break;
        }
        default:
          break;
      }
     },
    [getChat, patchChat, pushLog, scheduleSubagentRecordExpiry, scheduleStreamFlush, cancelStreamFlush, closeStream, refreshSessions]
  );

  // ------------------------------------------------------------ SSE 连接管理（P1-5）

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
    [closeStream, handleSSEEvent, patchChat, probeTaskAlive, pushLog, syncSessionAgents]
  );

  // 30s stall 检测（P1-5）：任务运行中超过 30s 无任何 SSE 事件（含 keepalive
  // 不触发 onmessage），提示用户可能卡住——长工具执行属正常场景，仅提示不阻断。
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

  // ------------------------------------------------------------ 发送


  const runCompact = useCallback(
    async (raw: string) => {
      const sid = activeSessionId;
      if (!sid) {
        window.alert("当前没有会话，无法压缩上下文。");
        return;
      }
      if (getChat(sid).running) {
        window.alert("任务运行中无法压缩，请先停止任务。");
        return;
      }
      const focus = raw.replace(/^\/compact\s*/i, "").trim();
      patchChat(sid, { messages: [...(getChat(sid).messages ?? []), { role: "user", content: raw }], error: null });
      pushLog("🗜️ 正在压缩会话上下文…");
      try {
        const r = await api.compact(sid, focus);
        patchChat(sid, {
          messages: [...getChat(sid).messages, {
            role: "assistant",
            content: `🗜️ 上下文已压缩：${r.before_tokens} → ${r.after_tokens} tokens（释放 ${r.removed_tokens}，折叠 ${r.turns_compacted} 轮、保留最近 ${r.keep_turns} 轮）。`,
          }],
        });
        // 用压缩后的会话历史替换本地消息 + 刷新上下文面板水位
        const [stats, snap] = await Promise.all([api.contextStats(sid), api.getSession(sid).catch(() => null)]);
        patchChat(sid, { contextStats: stats, messages: snap?.messages ?? getChat(sid).messages });
        pushLog(`🗜️ /compact 完成：${r.before_tokens} → ${r.after_tokens} tokens`);
      } catch (e) {
        patchChat(sid, { error: (e as Error).message });
        pushLog(`✗ /compact 失败: ${(e as Error).message}`);
      }
    },
    [activeSessionId, getChat, patchChat, pushLog]
  );

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
      let sid = activeTabId ? tabsRef.current.find((t) => t.id === activeTabId)?.sessionId : null;
      const selectedModel = activeSessionId
        ? currentChat.modelOverride ?? null
        : (activeTabId ? draftModels[activeTabId] ?? null : null);
      const reasoningEffort = activeSessionId
        ? (currentChat.reasoningEffort ?? "")
        : (activeTabId ? draftReasoning[activeTabId] ?? "" : "");
      let createdSession = false;
      if (!sid) {
        // 当前 tab 尚无 session（newChatTab 的占位），首次发送时创建并绑定到该 tab
        const { session_id } = await api.createSession();
        sid = session_id;
        createdSession = true;
        patchChat(session_id, { ...EMPTY_CHAT, messages: [] });
        setTabs((prev) => prev.map((t) =>
          t.id === activeTabId ? { ...t, sessionId: session_id, modelOverride: selectedModel } : t
        ));
        if (selectedModel) {
          // 必须把后端确认的 override 写回会话状态，否则 Composer 读 currentChat.modelOverride
          // 会回退显示“系统默认”（而实际后端一直在用 override 执行任务，显示与事实不符）
          const resp = await api.setSessionModel(session_id, selectedModel);
          patchChat(session_id, { modelOverride: resp.override, effectiveModel: resp.effective });
        }
      }
      const base = getChat(sid);
      // SSE 连接（新任务提交与排队竞态续接共用，统一走 connectTaskStream：
      // 含断线重连、连接状态指示与 [DONE] 收尾，见 P1-5）
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
    [activeTabId, activeSessionId, currentChat.modelOverride, draftModels, getChat, patchChat, refreshSessions, cancelStreamFlush, handleSSEEvent, pushLog, currentAgent, openProject, status?.workspace, runCompact, runGoalCommand, runLoopCommand, syncSessionAgents]
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
  taskLauncherRef.current = (targetSid, prompt, effort) => {
    const reasoning = effort ?? getChat(targetSid)?.reasoningEffort ?? "";
    patchChat(targetSid, {
      running: true,
      streaming: { items: [] },
      error: null,
    });
    cancelStreamFlush(targetSid);
    streamingRefs.current.set(targetSid, { items: [] });
    void (async () => {
      try {
        const resp = await api.chat(targetSid, prompt, currentAgent, reasoning);
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

  const approve = useCallback(
    async (approvalId: string, approved: boolean) => {
      patchActiveChat({ pendingApprovals: (currentChat.pendingApprovals ?? []).filter((p) => p.id !== approvalId) });
      try {
        await api.approve(approvalId, approved);
      } catch {
        /* ignore */
      }
    },
    [currentChat.pendingApprovals, patchActiveChat]
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
        onOpenProject={openProjectEntry}
        onOpenCode={openCode}
        onNewProject={newProjectEntry}
        onOpenRecent={(path) => void openRecentProject(path)}
        onRemoveRecent={(path) => void removeRecentProject(path)}
        onTogglePin={(path) => void togglePinProject(path)}
        onBackToProjects={backToProjects}
        onOpenProjectNewWindow={() => void openProjectNewWindow()}
        onOpenSettings={() => setShowSettings(true)}
        onOpenAbout={() => setShowAbout(true)}
        onFileOpen={(p) => void openFileTab(p)}
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
              sseState={currentChat.sseState}
              agents={agents}
              collabModes={collabModes}
              sessionCollabMode={currentChat.collabMode ?? null}
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
            onKillBackground={(id) => void api.killBackgroundTask(id).then(() => {
              setBackgroundTasks((prev) => prev.filter((t) => t.task_id !== id));
            }).catch(() => {})}
          />
          </ErrorBoundary>
        </>
      )}
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
          onClose={() => setShowPicker(false)}
          onSelect={(p) => void selectProject(p)}
        />
      )}
    </div>
  );
}
