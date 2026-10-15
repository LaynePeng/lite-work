// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// P3 移动壳（手机 PWA）：两页结构——会话列表 + 聊天。
// ChatView / Composer / QuestionBar / NewTaskWizard / SettingsModal 全量复用：
// 只简化布局层级，不简化能力（审批卡、邮件草稿、worktree 评审与桌面同一通道）。

import { useEffect, useState } from "react";
import ChatView, { QuestionBar } from "./ChatView";
import Composer from "./Composer";
import NewTaskWizard, { type NewTaskConfig } from "./NewTaskWizard";
import PendingQueue from "./PendingQueue";
import ProjectPicker from "./ProjectPicker";
import SettingsModal from "./SettingsModal";
import { WatchToaster } from "./WatchToaster";
import { getPushState, isStandalone, subscribePush, unsubscribePush, type PushState } from "../lib/pwa";
import { apiUrl, authHeaders } from "../lib/connection";
import type { WatchNotification } from "../hooks/useWatchNotifications";
import type { AgentInfo, ChatSessionState, CollabMode, LLMConfig, LLMProviderMeta, RecentProject, SessionInfo, SessionModel } from "../types";

export interface MobileAppProps {
  // —— 服务器 / 项目 ——
  workspaceName: string;
  version: string;
  workspaceIsGit: boolean;
  /** 当前项目绝对路径（项目切换面板高亮用） */
  workspace: string;
  /** 最近打开的项目（移动端项目切换面板；手机上此前完全没有项目入口） */
  recentProjects: RecentProject[];
  /** 切换到某个项目（由 App 复用桌面同一管线） */
  onSwitchProject: (path: string) => void;
  // —— 会话列表页 ——
  sessions: SessionInfo[];
  activeSessionId: string | null;
  onSelectSession: (id: string) => void;
  onNewChat: () => void;
  onNewTask: () => void;
  onContinueSession: (id: string, title: string) => void;
  onRenameSession: (id: string, name: string) => void;
  onDeleteSession: (id: string) => void;
  onTogglePinSession: (id: string, pinned: boolean) => void;
  onRefreshSessions: () => void;
  // —— 聊天页（ChatView 透传） ——
  sessionTitle: string;
  currentChat: ChatSessionState;
  currentAgent: string;
  onSend: (prompt: string) => void;
  onStop: () => void;
  onApprove: (approvalId: string, approved: boolean) => void;
  onAnswerQuestion: (id: string, answer: string) => void;
  onContinueTask: () => void;
  onStickChange: (stick: boolean) => void;
  onDismissError: () => void;
  onWorktreeDiff: () => void;
  onWorktreeMerge: () => void;
  onWorktreeAiMerge: () => void;
  onWorktreeAbortMerge: () => void;
  onWorktreeDiscard: () => void;
  foldTurns?: number;
  foldMessages?: number;
  onQueueRemove: (idx: number) => void;
  onQueueReorder: (from: number, to: number) => void;
  onQueueSend: (idx: number) => void;
  // —— Composer 透传 ——
  agents: AgentInfo[];
  onSelectAgent: (id: string) => void;
  collabModes: CollabMode[];
  sessionCollabMode: string | null;
  onSessionCollabMode: (mode: string | null) => void;
  onCollabModesRefresh: () => void;
  llmConfig: LLMConfig | null;
  providerMeta?: LLMProviderMeta[];
  sessionModel: SessionModel | null;
  onSessionModelChange: (model: SessionModel | null) => void;
  reasoningEffort: string;
  onReasoningEffortChange: (v: string) => void;
  scrolledUp: boolean;
  stopArmed: boolean;
  // —— 全局横幅 ——
  success: string | null;
  onDismissSuccess: () => void;
  // —— 弹层 ——
  newTaskOpen: boolean;
  onNewTaskCancel: () => void;
  onNewTaskSubmit: (cfg: NewTaskConfig) => void;
  settingsOpen: boolean;
  onOpenSettings: () => void;
  onCloseSettings: () => void;
  onSettingsSaved: () => void;
  watchToasts: WatchNotification[];
  onDismissWatchToast: (id: number) => void;
}

/** 相对时间：与 Sidebar.relTime 同款（组件内小工具，独立维护）。 */
function relTime(ts: number): string {
  if (!ts) return "";
  const diff = Date.now() - ts;
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  if (diff < 172_800_000) return "昨天";
  if (diff < 7 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`;
  return new Date(ts).toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
}

export default function MobileApp(props: MobileAppProps) {
  const { currentChat } = props;
  const [page, setPage] = useState<"list" | "chat">("list");
  const [sheetSession, setSheetSession] = useState<SessionInfo | null>(null);
  const [pushState, setPushState] = useState<PushState>("unsupported");
  // 项目面板：最近项目 / 浏览目录（此前手机端没有任何项目入口）
  const [projectOpen, setProjectOpen] = useState(false);
  const [browsing, setBrowsing] = useState(false);

  useEffect(() => {
    void getPushState().then(setPushState);
  }, []);

  const openSession = (id: string) => {
    props.onSelectSession(id);
    setPage("chat");
  };
  const newChat = () => {
    props.onNewChat();
    setPage("chat");
  };
  const backToList = () => {
    props.onRefreshSessions();
    setPage("list");
  };
  const submitTask = (cfg: NewTaskConfig) => {
    props.onNewTaskSubmit(cfg);
    setPage("chat");
  };

  const togglePush = async () => {
    if (pushState === "on") {
      await unsubscribePush();
      setPushState(await getPushState());
      return;
    }
    if (pushState === "insecure") {
      window.alert("当前是 HTTP 连接，浏览器不允许推送通知：需要 HTTPS（Tailscale 或云端部署）");
      return;
    }
    if (pushState === "denied") {
      window.alert("通知权限已被拒绝：请在系统设置 → 通知 中允许 lite-work");
      return;
    }
    // iOS 16.4+：推送仅在「添加到主屏幕」安装后可用
    const ua = navigator.userAgent;
    const isIOS = /iP(hone|ad|od)/.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    if (isIOS && !isStandalone()) {
      window.alert("iOS 推送通知需要先通过 Safari「分享 → 添加到主屏幕」安装应用后使用");
      return;
    }
    const r = await subscribePush();
    if (r.ok) setPushState("on");
    else window.alert(r.error ?? "订阅失败");
  };

  // 交付物 chip → 系统分享面板（预览/存文件/转发）。带令牌拉文件成 blob，
  // 再交 navigator.share——直接给 <a href> 会因无 Authorization 头而 401。
  const openDeliverable = async (name: string) => {
    try {
      const res = await fetch(apiUrl(`/api/files/download?path=${encodeURIComponent(name)}`), {
        headers: authHeaders(),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      // arrayBuffer 而非直接用 blob：某些环境（老 WebView/测试运行时）的
      // File 构造器不认异源 Blob 实例，裸字节最稳
      const buf = await res.arrayBuffer();
      const contentType = res.headers.get("Content-Type") || "application/octet-stream";
      const file = new File([buf], name, { type: contentType });
      const nav = navigator as Navigator & {
        canShare?: (data: { files?: File[] }) => boolean;
        share?: (data: { files?: File[]; title?: string }) => Promise<void>;
      };
      if (nav.canShare?.({ files: [file] }) && nav.share) {
        await nav.share({ files: [file], title: name });
      } else {
        window.open(URL.createObjectURL(new Blob([buf], { type: contentType })), "_blank");
      }
    } catch {
      window.alert(`打开「${name}」失败`);
    }
  };

  const agentBoard = currentChat.agentBoard ?? [];
  const runningAgents = agentBoard.filter((a) => a.status === "running").length;
  const queue = currentChat.pendingQueue ?? [];

  return (
    <div className="mobile-app">
      {page === "list" ? (
        <>
          <header className="mobile-header">
            <div className="mobile-brand">
              <img className="mobile-brand-icon" src="/icons/icon-192.png" alt="" />
              <div className="mobile-brand-text">
                <div className="mobile-workspace">{props.workspaceName}</div>
                <div className="mobile-version">lite-work {props.version}</div>
              </div>
            </div>
            <div className="mobile-header-actions">
              <button
                className="mobile-icon-btn"
                onClick={() => setProjectOpen(true)}
                title="切换项目"
              >
                📁
              </button>
              {pushState !== "unsupported" && (
                <button
                  className={`mobile-icon-btn ${pushState === "on" ? "active" : ""}`}
                  onClick={() => void togglePush()}
                  title={pushState === "on" ? "关闭推送通知" : pushState === "insecure" ? "推送不可用（HTTP 连接）" : "开启推送通知"}
                >
                  {pushState === "on" ? "🔔" : pushState === "insecure" || pushState === "denied" ? "🔕" : "🔔"}
                </button>
              )}
              <button className="mobile-icon-btn" onClick={props.onOpenSettings} title="设置">
                ⚙
              </button>
            </div>
          </header>

          <div className="mobile-list">
            {props.sessions.length === 0 ? (
              <div className="mobile-list-empty">
                <div className="mobile-list-empty-icon">🗒</div>
                <p>还没有会话</p>
                <p className="mobile-list-empty-sub">点右下角 ＋ 发起第一个任务</p>
              </div>
            ) : (
              props.sessions.map((s) => (
                <button
                  key={s.session_id}
                  className={`mobile-session-card ${s.session_id === props.activeSessionId ? "active" : ""}`}
                  onClick={() => openSession(s.session_id)}
                >
                  <div className="mobile-card-top">
                    {s.running && <span className="mobile-run-dot" />}
                    <span className="mobile-card-title">{s.title || "未命名会话"}</span>
                    <span
                      className="mobile-card-more"
                      role="button"
                      aria-label="会话操作"
                      onClick={(e) => {
                        e.stopPropagation();
                        setSheetSession(s);
                      }}
                    >
                      ⋯
                    </span>
                  </div>
                  {s.todo_progress && (
                    <div className="mobile-card-progress">
                      <div
                        className="mobile-card-progress-bar"
                        style={{ width: `${s.todo_progress.total ? (s.todo_progress.done / s.todo_progress.total) * 100 : 0}%` }}
                      />
                      <span className="mobile-card-progress-text">
                        {s.todo_progress.done}/{s.todo_progress.total}
                        {s.todo_progress.current ? ` · ${s.todo_progress.current}` : ""}
                      </span>
                    </div>
                  )}
                  {s.last_activity && (
                    <div className={`mobile-card-activity ${s.last_activity.ok === false ? "err" : ""}`}>
                      {s.last_activity.ok === false ? "⚠ " : ""}
                      {s.last_activity.summary}
                    </div>
                  )}
                  {s.deliverables && s.deliverables.length > 0 && (
                    <div className="mobile-card-files">
                      {s.deliverables.slice(0, 3).map((d) => (
                        <button
                          key={d.name}
                          className="mobile-file-chip"
                          onClick={(e) => {
                            e.stopPropagation();
                            void openDeliverable(d.name);
                          }}
                          title={d.name}
                        >
                          📄 {d.name}
                        </button>
                      ))}
                      {s.deliverables.length > 3 && (
                        <span className="mobile-file-chip more">+{s.deliverables.length - 3}</span>
                      )}
                    </div>
                  )}
                  <div className="mobile-card-meta">
                    {s.pinned && <span className="mobile-card-pin">📌</span>}
                    {relTime(s.updated_at)} · {s.message_count} 条
                    {typeof s.cost_usd === "number" && s.cost_usd > 0 ? ` · $${s.cost_usd.toFixed(2)}` : ""}
                  </div>
                </button>
              ))
            )}
          </div>

          <div className="mobile-list-actions">
            <button className="mobile-new-chat" onClick={newChat}>
              ✏️ 对话
            </button>
            <button className="mobile-fab" onClick={props.onNewTask} title="新建任务">
              ＋
            </button>
          </div>
        </>
      ) : (
        <>
          <header className="mobile-header">
            <button className="mobile-back" onClick={backToList} title="返回会话列表">
              ‹
            </button>
            <div className="mobile-chat-title">
              <div className="mobile-chat-name">{props.sessionTitle || "新对话"}</div>
              <div className="mobile-chat-sub">
                {currentChat.running
                  ? `执行中 · 第 ${currentChat.turn} 轮`
                  : `${currentChat.messages.length} 条消息`}
              </div>
            </div>
            {currentChat.running && (
              <span className={`mobile-sse-dot ${currentChat.sseState ?? "idle"}`} title="任务连接状态" />
            )}
          </header>

          {currentChat.stalled && currentChat.running && (
            <div className="mobile-banner warn">
              <span>⚠ 任务长时间无响应</span>
              <button onClick={props.onStop}>停止</button>
            </div>
          )}
          {currentChat.error && (
            <div className="mobile-banner err">
              <span>⚠ {currentChat.error}</span>
              <button onClick={props.onDismissError}>✕</button>
            </div>
          )}
          {props.success && (
            <div className="mobile-banner ok">
              <span>✅ {props.success}</span>
              <button onClick={props.onDismissSuccess}>✕</button>
            </div>
          )}
          {agentBoard.length > 0 && (
            <div className="mobile-agent-chip">
              🤖 Agents · {runningAgents > 0 ? `${runningAgents} 运行中` : "全部完成"} ·{" "}
              {agentBoard.length - runningAgents} 已完成
            </div>
          )}

          <div className="mobile-chat-body">
            <ChatView
              sessionId={props.activeSessionId ?? "（未选择）"}
              sessionTitle={props.sessionTitle || "新对话"}
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
              onSend={props.onSend}
              onStop={props.onStop}
              onApprove={props.onApprove}
              currentAgent={props.currentAgent}
              unfinished={currentChat.unfinished}
              onContinue={props.onContinueTask}
              worktreeEnabled={currentChat.worktreeEnabled}
              worktreeStatus={currentChat.worktreeStatus ?? null}
              worktreeConflicts={currentChat.worktreeConflicts ?? []}
              worktreeMerge={currentChat.worktreeMerge ?? null}
              onWorktreeDiff={props.onWorktreeDiff}
              onWorktreeMerge={props.onWorktreeMerge}
              onWorktreeAiMerge={props.onWorktreeAiMerge}
              onWorktreeAbortMerge={props.onWorktreeAbortMerge}
              onWorktreeDiscard={props.onWorktreeDiscard}
              foldTurns={props.foldTurns}
              foldMessages={props.foldMessages}
              onStickChange={props.onStickChange}
            />
          </div>

          <QuestionBar
            pendingQuestions={currentChat.pendingQuestions ?? []}
            onAnswerQuestion={props.onAnswerQuestion}
          />

          {queue.length > 0 && (
            <PendingQueue
              items={queue}
              onRemove={props.onQueueRemove}
              onReorder={props.onQueueReorder}
              onSend={props.onQueueSend}
            />
          )}

          <Composer
            running={currentChat.running}
            agents={props.agents}
            collabModes={props.collabModes}
            sessionCollabMode={props.sessionCollabMode}
            onSessionCollabMode={props.onSessionCollabMode}
            onCollabModesRefresh={props.onCollabModesRefresh}
            currentAgent={props.currentAgent}
            onSelectAgent={props.onSelectAgent}
            onSend={props.onSend}
            onStop={props.onStop}
            llmConfig={props.llmConfig}
            providerMeta={props.providerMeta}
            sessionModel={props.sessionModel}
            onSessionModelChange={props.onSessionModelChange}
            reasoningEffort={props.reasoningEffort}
            onReasoningEffortChange={props.onReasoningEffortChange}
            tabCount={1}
            scrolledUp={props.scrolledUp}
            stopArmed={props.stopArmed}
            autoFocus={false}
          />
        </>
      )}

      {sheetSession && (
        <div className="mobile-sheet-mask" onClick={() => setSheetSession(null)}>
          <div className="mobile-sheet" onClick={(e) => e.stopPropagation()}>
            <div className="mobile-sheet-title">{sheetSession.title || "未命名会话"}</div>
            <button
              className="mobile-sheet-item"
              onClick={() => {
                props.onContinueSession(sheetSession.session_id, sheetSession.title);
                setSheetSession(null);
                setPage("chat");
              }}
            >
              ▶ 继续会话
            </button>
            <button
              className="mobile-sheet-item"
              onClick={() => {
                const name = window.prompt("重命名会话", sheetSession.title);
                if (name && name.trim()) props.onRenameSession(sheetSession.session_id, name.trim());
                setSheetSession(null);
              }}
            >
              ✏️ 重命名
            </button>
            <button
              className="mobile-sheet-item"
              onClick={() => {
                props.onTogglePinSession(sheetSession.session_id, !sheetSession.pinned);
                setSheetSession(null);
              }}
            >
              {sheetSession.pinned ? "📌 取消置顶" : "📌 置顶"}
            </button>
            <button
              className="mobile-sheet-item danger"
              onClick={() => {
                if (window.confirm(`删除会话「${sheetSession.title || "未命名"}」？此操作不可恢复。`)) {
                  props.onDeleteSession(sheetSession.session_id);
                }
                setSheetSession(null);
              }}
            >
              🗑 删除
            </button>
            <button className="mobile-sheet-item cancel" onClick={() => setSheetSession(null)}>
              取消
            </button>
          </div>
        </div>
      )}

      {projectOpen && (
        <div className="mobile-sheet-mask" onClick={() => setProjectOpen(false)}>
          <div className="mobile-sheet mobile-project-sheet" onClick={(e) => e.stopPropagation()}>
            <div className="mobile-sheet-title">
              项目 · 当前：{props.workspaceName || "未打开"}
            </div>
            <div className="mobile-project-list">
              {props.recentProjects.length === 0 ? (
                <div className="mobile-sheet-item project-empty">还没有打开过的项目</div>
              ) : (
                props.recentProjects.map((p) => (
                  <button
                    key={p.path}
                    className={`mobile-sheet-item ${p.path === props.workspace ? "active" : ""}`}
                    onClick={() => {
                      setProjectOpen(false);
                      if (p.path !== props.workspace) props.onSwitchProject(p.path);
                    }}
                    title={p.path}
                  >
                    <span className="mobile-project-name">
                      {p.kind === "code" ? "💻" : "📁"} {p.name || p.path}
                    </span>
                    <span className="mobile-project-path">{p.path}</span>
                  </button>
                ))
              )}
            </div>
            <button
              className="mobile-sheet-item"
              onClick={() => {
                setProjectOpen(false);
                setBrowsing(true);
              }}
            >
              🗂 浏览其它目录…
            </button>
            <button className="mobile-sheet-item cancel" onClick={() => setProjectOpen(false)}>
              取消
            </button>
          </div>
        </div>
      )}

      {browsing && (
        <ProjectPicker
          initialPath={props.workspace || ""}
          onClose={() => setBrowsing(false)}
          onSelect={(p) => {
            setBrowsing(false);
            props.onSwitchProject(p);
          }}
        />
      )}

      {props.newTaskOpen && (
        <NewTaskWizard
          collabModes={props.collabModes}
          workspaceName={props.workspaceName}
          workspaceIsGit={props.workspaceIsGit}
          onCancel={props.onNewTaskCancel}
          onSubmit={submitTask}
        />
      )}
      {props.settingsOpen && (
        <SettingsModal onClose={props.onCloseSettings} onSaved={props.onSettingsSaved} />
      )}
      <WatchToaster items={props.watchToasts} onDismiss={props.onDismissWatchToast} />
    </div>
  );
}
