import { useEffect, useRef, useState } from "react";
import type { BackgroundTaskInfo, ContextStats, ContextTaskStats, MCPServerStatus, SubAgentProgress, TodoItem } from "../types";

// ---------------------------------------------------------------- 上下文情况面板

function fmt(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : n.toLocaleString();
}

function pct(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return `${(n * 100).toFixed(1)}%`;
}

function ContextBlock({ label, value }: { label: string; value: string }) {
  return (
    <div className="ctx-row">
      <span className="ctx-row-label">{label}</span>
      <b className="ctx-row-value">{value}</b>
    </div>
  );
}

function ContextPanel({ stats }: { stats: ContextStats | null }) {
  if (!stats) {
    return <div className="tool-panel-empty">暂无上下文数据（发起对话后显示）</div>;
  }
  const window = stats.context_window || 0;
  const prompt = stats.task?.last_prompt_tokens ?? stats.task?.prompt_tokens ?? 0;
  const ratio = window > 0 ? prompt / window : 0;
  const pctWidth = Math.min(100, Math.max(0, ratio * 100));
  const danger = ratio >= 0.9;
  const task = stats.task ?? ({} as ContextTaskStats);
  const session = stats.session ?? {};

  return (
    <div className="ctx-panel">
      <div className="ctx-title" title="模型上下文窗口（models.dev 同步/内置表/手动覆盖）">
        {stats.model || "模型"} · 窗口 {window ? window.toLocaleString() : "?"} tokens
      </div>

      <div className="ctx-progress">
        <div className={`ctx-progress-track ${danger ? "danger" : ""}`}>
          <div
            className="ctx-progress-fill"
            style={{ width: `${pctWidth}%` }}
          />
        </div>
        <div className="ctx-progress-meta">
          <span>
            本次 {fmt(prompt)} · {pct(ratio)}
          </span>
          {danger && <span className="ctx-danger">≥90%，已自动压缩</span>}
        </div>
      </div>

      <div className="ctx-section-label">本次调用（模型准确返回）</div>
      <ContextBlock label="Prompt tokens" value={fmt(task.prompt_tokens)} />
      <ContextBlock label="输出 tokens" value={fmt(task.output_tokens)} />
      <ContextBlock label="Cache 命中 / 未命中" value={`${fmt(task.cache_hit_tokens)} / ${fmt(task.cache_miss_tokens)}`} />
      <ContextBlock label="Cache 命中率" value={pct(task.cache_hit_rate)} />
      <ContextBlock label="上下文压缩" value={`${task.compression_count ?? 0} 次`} />
      <ContextBlock label="累计节省 tokens" value={fmt(task.compressed_tokens)} />
      <ContextBlock label="工具调用" value={`${task.tool_calls ?? 0} 次`} />
      <ContextBlock label="安全拦截" value={`${task.blocked ?? 0} 次`} />
      <ContextBlock label="预估成本" value={task.cost_estimate != null ? `$${task.cost_estimate.toFixed(4)}` : "—"} />

      <div className="ctx-section-label">会话累计</div>
      <ContextBlock label="Prompt tokens" value={fmt(session.prompt_tokens)} />
      <ContextBlock label="输出 tokens" value={fmt(session.output_tokens)} />
      <ContextBlock label="Cache 命中率" value={pct(session.cache_hit_rate)} />
      <ContextBlock label="压缩次数 / 节省" value={`${session.compression_count ?? 0} 次 / ${fmt(session.compressed_tokens)}`} />
      <ContextBlock label="工具调用" value={`${session.tool_calls ?? 0} 次`} />
      <ContextBlock label="安全拦截" value={`${session.blocked ?? 0} 次`} />
      <ContextBlock label="预估成本" value={session.cost_estimate != null ? `$${session.cost_estimate.toFixed(4)}` : "—"} />
    </div>
  );
}

// ---------------------------------------------------------------- MCP 状态面板

function McpPanel({ servers }: { servers: MCPServerStatus[] }) {
  if (!servers || servers.length === 0) {
    return <div className="tool-panel-empty">暂未配置 MCP</div>;
  }
  return (
    <div className="mcp-status-list">
      {servers.map((s) => (
        <div className="mcp-status-item" key={s.name} title={s.error || s.tools.join(", ")}>
          <span className={`mcp-dot ${s.connected ? "on" : "off"}`} />
          <div className="mcp-status-main">
            <div className="mcp-status-name">{s.name}</div>
            <div className="mcp-status-meta">
              {s.connected
                ? `${s.tools.length} 个工具`
                : s.enabled
                  ? (s.error ? "连接失败" : "未连接")
                  : "已禁用"}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- 工具面板

function ToolsPanel({ tools }: { tools: { name: string; description: string }[] }) {
  if (!tools || tools.length === 0) {
    return <div className="tool-panel-empty">暂无已注册工具（发起对话后显示）</div>;
  }
  return (
    <div className="tools-panel">
      <div className="tools-panel-count">已注册工具（{tools.length}）</div>
      <ul className="tools-list">
        {tools.map((t) => (
          <li key={t.name} title={t.description}>
            <span className="tool-dot" />
            {t.name}
          </li>
        ))}
      </ul>
    </div>
  );
}

// ---------------------------------------------------------------- TODOs 面板

const TODO_MARKS: Record<TodoItem["status"], string> = {
  pending: "☐",
  in_progress: "▶",
  completed: "✔",
};

function TodosPanel({ todos }: { todos: TodoItem[] }) {
  if (!todos || todos.length === 0) {
    return <div className="tool-panel-empty">暂无 TODO（Agent 规划多步骤任务时自动生成）</div>;
  }
  const done = todos.filter((t) => t.status === "completed").length;
  const doing = todos.find((t) => t.status === "in_progress");
  return (
    <div className="todos-panel">
      <div className="tools-panel-count">
        任务进度 {done}/{todos.length}
        {doing ? ` · 当前：${doing.content}` : ""}
      </div>
      <ul className="todos-list">
        {todos.map((t, i) => (
          <li key={i} className={`todo-item todo-${t.status}`}>
            <span className="todo-mark" aria-hidden>{TODO_MARKS[t.status]}</span>
            <span className="todo-content">{t.content}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ---------------------------------------------------------------- 后台命令面板

function BackgroundPanel({ tasks, onKill }: { tasks: BackgroundTaskInfo[]; onKill: (taskId: string) => void }) {
  if (!tasks || tasks.length === 0) {
    return <div className="tool-panel-empty">暂无后台命令（Agent 异步执行时显示）</div>;
  }
  return (
    <div className="bg-panel">
      <div className="tools-panel-count">运行中 {tasks.filter((t) => t.running).length} / 共 {tasks.length}</div>
      <ul className="bg-list">
        {tasks.map((t) => (
          <li key={t.task_id} className={`bg-item ${t.running ? "running" : "done"}`}>
            <span className="bg-item-text" title={t.command}>{t.command}</span>
            <span className="bg-item-meta">
              {t.running ? `⏱ ${t.elapsed}s` : `✅ ${t.exit_code} (${t.elapsed}s)`}
            </span>
            {t.running && (
              <button className="bg-item-kill" onClick={() => onKill(t.task_id)} title="杀掉">■</button>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

// ---------------------------------------------------------------- Agents 看板（竖排 kanban）

// agent 配色板（对齐 Claude Code per-agent color）：按 role+nickname 稳定哈希取色
const AGENT_PALETTE = ["#f97583", "#f97316", "#eab308", "#22c55e", "#06b6d4", "#4f8cff", "#8b5cf6", "#ec4899"];
function agentColor(key: string): string {
  let h = 0;
  for (let i = 0; i < key.length; i += 1) h = (h * 31 + key.charCodeAt(i)) >>> 0;
  return AGENT_PALETTE[h % AGENT_PALETTE.length];
}

/** 单张 agent 卡片：运行中显示实时步骤/流式尾部；完成显示可展开的 summary + 交付标记。 */
function AgentCard({ agent, delivered }: { agent: SubAgentProgress; delivered?: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const [reviewed, setReviewed] = useState(false);
  const running = agent.status === "running";
  const elapsed = agent.startedAt ? Math.floor((Date.now() - agent.startedAt) / 1000) : null;
  const current = [...agent.steps].reverse().find((s) => s.status === "running");
  const doneSteps = agent.steps.filter((s) => s.status !== "running").length;
  const color = agentColor(`${agent.role}/${agent.subagentId || agent.task}`);
  return (
    <div
      className={`agent-card ${running ? "running" : "done"}`}
      style={{ borderLeftColor: running ? color : undefined }}
    >
      <div className="agent-card-head">
        <span className="agent-role" style={{ color }}>{agent.role || "general"}</span>
        {running
          ? <span className="agent-meta">{elapsed != null ? `${elapsed}s` : ""}{agent.turn ? ` · turn ${agent.turn}` : ""}</span>
          : <span className="agent-meta">
              {agent.tokens ? `${(agent.tokens / 1000).toFixed(1)}k tok` : ""}
              {delivered && <span className="agent-delivered"> · ↩ 已交付</span>}
            </span>}
      </div>
      <div className="agent-task" title={agent.task}>{agent.task || "（未描述任务）"}</div>
      {running ? (
        <div className="agent-live">
          {current
            ? <span className="agent-step" title={current.brief}>▸ {current.tool}</span>
            : (doneSteps > 0 || agent.turn > 0
                ? <span className="agent-step">已执行 {doneSteps} 步</span>
                : <span className="agent-step">启动中…</span>)}
          {agent.streaming_text && (
            <span className="agent-stream">{agent.streaming_text.slice(-100)}</span>
          )}
        </div>
      ) : (
        <div className="agent-result">
          {agent.changedFiles && agent.changedFiles.length > 0 && (
            <div
              className={`agent-review-badge ${reviewed ? "done" : "pending"}`}
              onClick={() => setReviewed((v) => !v)}
              title={reviewed ? "已人工过目（点击恢复待审查）" : "有改动文件待人工过目（点击标记已审查）"}
            >
              {reviewed ? "✔ 已审查" : `⚠ 待审查（${agent.changedFiles.length} 文件）`}
            </div>
          )}
          <div
            className={`agent-summary ${expanded ? "expanded" : ""}`}
            onClick={() => setExpanded((v) => !v)}
            title="点击展开/收起总结"
          >
            {agent.summary || "（无总结）"}
          </div>
          {expanded && agent.changedFiles && agent.changedFiles.length > 0 && (
            <div className="agent-files">
              {agent.changedFiles.map((f) => <span className="agent-file" key={f} title={f}>{f}</span>)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** Agents 看板：按合作模式分组的多视图交接看板（对齐 docs/multi-agent-design.md §9）。
 *
 * - orchestrate（编排-工人，默认）：竖排 kanban——运行中/已完成分区，卡片跨区流动
 * - brainstorm（头脑风暴）：视角提案墙——多视角卡片平铺（无状态分区，提案并列对比）
 * - debate（辩论/互批）：对抗泳道——proposer 与 critic 分组对垒
 * - pipeline（流水线）：顺序接力链——按派生序编号，箭头串联交接
 * 主 Agent 根节点置顶；子 Agent 卡片经树形连接线挂在各分组下。 */
function AgentsPanel({ agents, orchestrator }: {
  agents: SubAgentProgress[];
  orchestrator?: { agentId: string; running: boolean };
}) {
  const running = agents.filter((a) => a.status === "running");
  // 运行时长计时：有 running 卡片时每秒重渲染
  const [, tick] = useState(0);
  useEffect(() => {
    if (running.length === 0) return;
    const t = window.setInterval(() => tick((v) => v + 1), 1000);
    return () => window.clearInterval(t);
  }, [running.length]);

  const orch = orchestrator ?? { agentId: "build", running: false };

  // 按 mode 分组（保持派生顺序）
  const groups = new Map<string, SubAgentProgress[]>();
  for (const a of agents) {
    const m = a.mode && a.mode !== "orchestrate" ? a.mode : "orchestrate";
    if (!groups.has(m)) groups.set(m, []);
    groups.get(m)!.push(a);
  }

  return (
    <div className="agents-panel">
      <div className={`agent-orchestrator ${orch.running ? "running" : "idle"}`}>
        <span className="agent-orch-icon">🤖</span>
        <div className="agent-orch-main">
          <div className="agent-orch-name">{orch.agentId}</div>
          <div className="agent-orch-meta">
            {orch.running ? "编排运行中" : "空闲"}
            {agents.length > 0 && ` · 派生 ${agents.filter((a) => a.status !== "running").length}/${agents.length}`}
          </div>
        </div>
      </div>
      {agents.length === 0 ? (
        <div className="tool-panel-empty">暂无派生的子 Agent（主 Agent 派生任务时在此显示看板）</div>
      ) : (
        <div className="agent-branches">
          {[...groups.entries()].map(([mode, list]) => (
            <ModeSection key={mode} mode={mode} agents={list} />
          ))}
        </div>
      )}
    </div>
  );
}

/** 模式分组标题 */
const MODE_META: Record<string, { icon: string; label: string }> = {
  orchestrate: { icon: "⚙", label: "编排 · 并行派发" },
  pipeline: { icon: "⛓", label: "流水线 · 顺序交接" },
  brainstorm: { icon: "💡", label: "头脑风暴 · 多视角提案" },
  debate: { icon: "⚔", label: "辩论 · 对抗评审" },
};

function ModeSection({ mode, agents }: { mode: string; agents: SubAgentProgress[] }) {
  const meta = MODE_META[mode] ?? MODE_META.orchestrate;
  return (
    <div className="agent-mode-group">
      <div className="agent-mode-head">
        <span>{meta.icon}</span>{meta.label}
        <span className="agent-mode-count">({agents.length})</span>
      </div>
      {mode === "brainstorm" ? (
        // 视角提案墙：并列平铺（运行中脉冲点标识未完）
        <div className="agent-brainstorm-wall">
          {agents.map((a) => <AgentCard key={a.subagentId || a.task} agent={a} />)}
        </div>
      ) : mode === "debate" ? (
        // 对抗泳道：proposer（提案/修订） vs critic（批判）
        <DebateLanes agents={agents} />
      ) : mode === "pipeline" ? (
        // 接力链：按派生序编号 + 箭头串联
        <div className="agent-pipeline-chain">
          {[...agents].sort((a, b) => (a.startedAt ?? 0) - (b.startedAt ?? 0)).map((a, i) => (
            <div key={a.subagentId || a.task} className="agent-pipeline-node">
              {i > 0 && <span className="agent-pipeline-arrow">↓</span>}
              <div className="agent-pipeline-step">
                <span className="agent-pipeline-no">{i + 1}</span>
                <AgentCard agent={a} />
              </div>
            </div>
          ))}
        </div>
      ) : (
        // orchestrate：竖排 kanban（运行中/已完成分区，卡片跨区流动）
        <>
          <div className="agent-section-head">
            <span className="agent-dot running" />运行中（{agents.filter((a) => a.status === "running").length}）
          </div>
          {agents.filter((a) => a.status === "running").map((a) => (
            <AgentCard key={a.subagentId || a.task} agent={a} />
          ))}
          {agents.some((a) => a.status !== "running") && (
            <>
              <div className="agent-section-head">
                <span className="agent-dot done" />已完成（{agents.filter((a) => a.status !== "running").length}）
              </div>
              {agents.filter((a) => a.status !== "running").map((a) => (
                <AgentCard key={a.subagentId || a.task} agent={a} delivered />
              ))}
            </>
          )}
        </>
      )}
    </div>
  );
}

/** 辩论泳道：proposer vs critic 对垒（角色归位：critic 单独一组，其余为提案方） */
function DebateLanes({ agents }: { agents: SubAgentProgress[] }) {
  const critics = agents.filter((a) => a.role === "critic");
  const proposers = agents.filter((a) => a.role !== "critic");
  return (
    <div className="agent-debate-lanes">
      <div className="agent-debate-lane">
        <div className="agent-debate-lane-head">提案方</div>
        {proposers.map((a) => <AgentCard key={a.subagentId || a.task} agent={a} />)}
        {proposers.length === 0 && <span className="mcp-empty-inline">（暂无）</span>}
      </div>
      <span className="agent-debate-vs">⇄</span>
      <div className="agent-debate-lane critic">
        <div className="agent-debate-lane-head">批判方</div>
        {critics.map((a) => <AgentCard key={a.subagentId || a.task} agent={a} />)}
        {critics.length === 0 && <span className="mcp-empty-inline">（暂无）</span>}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 主组件

type PanelTabId = "context" | "todos" | "agents" | "mcp" | "background" | "tools";

// tab 条横向滑动：面板拖窄后 tab 队列整体滑动（不是拖动单个 tab）。
// pointer 按住横向拖动 → scrollLeft 跟随位移；位移 < 5px 视为点击不拦截，
// 同时支持触摸板/滚轮横向滚动与键盘左右键。
function useTabStripDrag() {
  const ref = useRef<HTMLDivElement | null>(null);
  const drag = useRef<{ startX: number; startScroll: number; moved: boolean; pointerId: number | null }>({
    startX: 0, startScroll: 0, moved: false, pointerId: null,
  });

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    // 仅主键；面板宽度足够无溢出时不启动（避免无谓拦截）
    const el = ref.current;
    if (e.button !== 0 || !el || el.scrollWidth <= el.clientWidth + 1) return;
    drag.current = { startX: e.clientX, startScroll: el.scrollLeft, moved: false, pointerId: e.pointerId };
    el.setPointerCapture(e.pointerId);
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const el = ref.current;
    const d = drag.current;
    if (!el || d.pointerId !== e.pointerId) return;
    const dx = e.clientX - d.startX;
    if (!d.moved && Math.abs(dx) < 5) return;
    if (!d.moved) {
      d.moved = true;
      el.classList.add("dragging");
    }
    el.scrollLeft = d.startScroll - dx;
  };

  const endDrag = (e: React.PointerEvent<HTMLDivElement>) => {
    const el = ref.current;
    const d = drag.current;
    if (!el || d.pointerId !== e.pointerId) return;
    d.pointerId = null;
    el.classList.remove("dragging");
  };

  // 拖动结束后浏览器仍会向按下元素派发 click（pointer capture 不抑制 click），
  // tab 的 onClick 用此判定吞掉：拖完松手 ≠ 点击
  const wasDragged = () => {
    const d = drag.current;
    if (d.moved) {
      d.moved = false;
      return true;
    }
    return false;
  };

  return { ref, onPointerDown, onPointerMove, onPointerUp: endDrag, onPointerCancel: endDrag, wasDragged };
}

export default function ToolPanel({
  contextStats, mcpServers, tools, todos, agentBoard, orchestrator, backgroundTasks, collapsed, onToggleCollapsed, onKillBackground,
}: {
  contextStats: ContextStats | null;
  mcpServers: MCPServerStatus[];
  tools: { name: string; description: string }[];
  todos: TodoItem[];
  agentBoard?: SubAgentProgress[];
  orchestrator?: { agentId: string; running: boolean };
  backgroundTasks?: BackgroundTaskInfo[];
  collapsed?: boolean;
  onToggleCollapsed?: () => void;
  onKillBackground?: (taskId: string) => void;
}) {
  const [panelTab, setPanelTab] = useState<PanelTabId>("context");
  const strip = useTabStripDrag();
  const todoDone = todos.filter((t) => t.status === "completed").length;
  const runningCount = (backgroundTasks ?? []).filter((t) => t.running).length;
  const runningAgents = (agentBoard ?? []).filter((a) => a.status === "running").length;

  const TABS: { id: PanelTabId; label: string; title?: string }[] = [
    { id: "context", label: "上下文" },
    { id: "todos", label: todos.length ? `TODOs ${todoDone}/${todos.length}` : "TODOs",
      title: todos.length ? `进度 ${todoDone}/${todos.length}` : "Agent 规划多步骤任务时生成 TODO 清单" },
    { id: "agents", label: runningAgents > 0 ? `Agents (${runningAgents})` : "Agents",
      title: "多 Agent 看板：运行中/已完成 竖排交接视图" },
    { id: "mcp", label: "MCP" },
    { id: "background", label: runningCount > 0 ? `后台 (${runningCount})` : "后台" },
    { id: "tools", label: "工具" },
  ];

  return (
    <aside className="tool-panel">
      <div className="panel-tabs-wrap">
        <div
          className="panel-tabs"
          ref={strip.ref}
          onPointerDown={strip.onPointerDown}
          onPointerMove={strip.onPointerMove}
          onPointerUp={strip.onPointerUp}
          onPointerCancel={strip.onPointerCancel}
        >
          {TABS.map((t) => (
            <button
              key={t.id}
              className={`panel-tab ${panelTab === t.id ? "active" : ""}`}
              title={t.title}
              onClick={() => { if (!strip.wasDragged()) setPanelTab(t.id); }}
            >
              {t.label}
            </button>
          ))}
        </div>
        <button className="panel-collapse-btn" onClick={onToggleCollapsed} title={collapsed ? "展开面板" : "收起面板"}>
          {collapsed ? "◀" : "▶"}
        </button>
      </div>
      <div className="tool-panel-body tool-panel-context">
        {panelTab === "context"
          ? <ContextPanel stats={contextStats} />
          : panelTab === "todos"
            ? <TodosPanel todos={todos} />
            : panelTab === "agents"
              ? <AgentsPanel agents={agentBoard ?? []} orchestrator={orchestrator} />
              : panelTab === "mcp"
                ? <McpPanel servers={mcpServers} />
                : panelTab === "background"
                  ? <BackgroundPanel tasks={backgroundTasks ?? []} onKill={onKillBackground ?? (() => {})} />
                  : <ToolsPanel tools={tools} />}
      </div>
    </aside>
  );
}