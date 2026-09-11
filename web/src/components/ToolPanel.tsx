// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import { useCallback, useEffect, useRef, useState } from "react";
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
  // 当前上下文水位 = 最近一次调用实际发出的 prompt（不是跨轮累加值）
  const prompt = stats.task?.last_prompt_tokens ?? stats.task?.prompt_tokens ?? 0;
  const ratio = window > 0 ? prompt / window : 0;
  const pctWidth = Math.min(100, Math.max(0, ratio * 100));
  const danger = ratio >= 0.9;
  const task = stats.task ?? ({} as ContextTaskStats);
  const session = stats.session ?? {};
  // 「本次调用」：优先取 last 段；旧载荷无该字段时退化为累计值（保持可用）
  const call = task.last ?? {
    prompt_tokens: task.prompt_tokens ?? 0,
    output_tokens: task.output_tokens ?? 0,
    cache_hit_tokens: task.cache_hit_tokens ?? 0,
    cache_miss_tokens: task.cache_miss_tokens ?? 0,
    cost_estimate: task.cost_estimate,
  };
  const pricing = stats.pricing;

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
            当前上下文 {fmt(prompt)} · {pct(ratio)}
          </span>
          {danger && <span className="ctx-danger">≥90%，已自动压缩</span>}
        </div>
      </div>

      {/* 口径区分：每轮都会把「系统提示 + 全部历史 + 工具结果」整段重发，
          累计值 ≈ 轮数 × 上下文，因此不能当作当前上下文看 */}
      <div className="ctx-section-label">本次调用（模型准确返回）</div>
      <ContextBlock label="Prompt tokens" value={fmt(call.prompt_tokens)} />
      <ContextBlock label="输出 tokens" value={fmt(call.output_tokens)} />
      <ContextBlock label="Cache 命中 / 未命中" value={`${fmt(call.cache_hit_tokens)} / ${fmt(call.cache_miss_tokens)}`} />
      <ContextBlock label="本次成本" value={call.cost_estimate != null ? `$${call.cost_estimate.toFixed(4)}` : "—"} />

      <div className="ctx-section-label">本任务累计（每轮重发上下文）</div>
      <ContextBlock label="Prompt tokens" value={fmt(task.prompt_tokens)} />
      <ContextBlock label="输出 tokens" value={fmt(task.output_tokens)} />
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

      {/* 计费单价：成本估算的唯一依据，直接展示便于与供应商账单对账 */}
      {pricing && (
        <>
          <div className="ctx-section-label" title="models.dev 同步成功后按该模型真实价计费；未收录模型用配置回退价（设置 → 定价）">
            计费单价（每 M tokens）
          </div>
          <ContextBlock
            label="输入 / 输出 / 缓存命中"
            value={`$${pricing.input_per_mtok} / $${pricing.output_per_mtok} / $${pricing.cache_hit_per_mtok}`}
          />
        </>
      )}
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
function AgentCard({ agent, delivered, reviewed, onToggleReview }: {
  agent: SubAgentProgress;
  delivered?: boolean;
  reviewed?: boolean;
  onToggleReview?: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
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
              onClick={() => onToggleReview?.()}
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
 *
 * 长程任务的防乱设计：
 * - 有运行中 agent 的分组置顶且展开；全部完成的分组自动折叠为统计行（点击展开）
 * - 过滤开关：全部 / 仅运行中（默认全部；只想盯活跃工作时一键收窄）
 * - 分组内已完成卡片截断（最近 MAX_DONE_SHOWN 个），"展开全部 N" 按需展开
 * - 待审查计数聚合到折叠头部（review gate 不因折叠而不可见） */
const MAX_DONE_SHOWN = 3;

function AgentsPanel({ agents, orchestrator }: {
  agents: SubAgentProgress[];
  orchestrator?: { agentId: string; running: boolean };
}) {
  const running = agents.filter((a) => a.status === "running");
  const done = agents.filter((a) => a.status !== "running");
  // 运行时长计时：有 running 卡片时每秒重渲染
  const [, tick] = useState(0);
  useEffect(() => {
    if (running.length === 0) return;
    const t = window.setInterval(() => tick((v) => v + 1), 1000);
    return () => window.clearInterval(t);
  }, [running.length]);

  // 过滤（全部 / 仅运行中）；分组展开覆盖（默认：有运行中的展开，全完成折叠）；
  // 组内已完成截断展开；审查状态（提升到面板级，供分组头聚合待审数）
  const [filter, setFilter] = useState<"all" | "running">("all");
  const [groupOpen, setGroupOpen] = useState<Record<string, boolean>>({});
  const [showAllDone, setShowAllDone] = useState<Record<string, boolean>>({});
  const [reviewedMap, setReviewedMap] = useState<Record<string, boolean>>({});
  const toggleReview = useCallback((id: string) => {
    setReviewedMap((m) => ({ ...m, [id]: !m[id] }));
  }, []);

  const orch = orchestrator ?? { agentId: "build", running: false };

  // 按 mode 分组（保持派生顺序）；仅运行中过滤时隐藏无活跃 agent 的组
  const groups = new Map<string, SubAgentProgress[]>();
  for (const a of agents) {
    const m = a.mode && a.mode !== "orchestrate" ? a.mode : "orchestrate";
    if (!groups.has(m)) groups.set(m, []);
    groups.get(m)!.push(a);
  }
  const pendingReview = done.filter((a) =>
    a.changedFiles && a.changedFiles.length > 0 && !reviewedMap[a.subagentId || a.task]).length;

  // 排序：有运行中 agent 的分组置顶（活跃工作永远在视野上方）
  const orderedModes = [...groups.keys()].sort((a, b) => {
    const rank = (m: string) => (groups.get(m)!.some((x) => x.status === "running") ? 0 : 1);
    return rank(a) - rank(b);
  });

  return (
    <div className="agents-panel">
      <div className={`agent-orchestrator ${orch.running ? "running" : "idle"}`}>
        <span className="agent-orch-icon">🤖</span>
        <div className="agent-orch-main">
          <div className="agent-orch-name">{orch.agentId}</div>
          <div className="agent-orch-meta">
            {orch.running ? "编排运行中" : "空闲"}
            {agents.length > 0 && ` · 派生 ${done.length}/${agents.length}`}
          </div>
        </div>
      </div>
      {agents.length === 0 ? (
        <div className="tool-panel-empty">暂无派生的子 Agent（主 Agent 派生任务时在此显示看板）</div>
      ) : (
        <>
          {/* 过滤条：全部 / 仅运行中 + 待审查聚合提醒 */}
          <div className="agent-filter-row">
            <button
              className={`agent-filter-chip ${filter === "all" ? "on" : ""}`}
              onClick={() => setFilter("all")}
            >全部 {agents.length}</button>
            <button
              className={`agent-filter-chip ${filter === "running" ? "on" : ""}`}
              onClick={() => setFilter("running")}
            >运行中 {running.length}</button>
            {pendingReview > 0 && (
              <span className="agent-filter-review" title="有 agent 改动文件待人工过目（展开对应分组查看）">
                ⚠ 待审查 {pendingReview}
              </span>
            )}
          </div>
          <div className="agent-branches">
            {orderedModes.map((mode) => (
              <ModeSection
                key={mode}
                mode={mode}
                agents={groups.get(mode)!}
                filter={filter}
                open={groupOpen[mode] ?? groups.get(mode)!.some((x) => x.status === "running")}
                onToggle={() => setGroupOpen((m) => ({ ...m, [mode]: !(m[mode] ?? groups.get(mode)!.some((x) => x.status === "running")) }))}
                showAllDone={!!showAllDone[mode]}
                onToggleShowAll={() => setShowAllDone((m) => ({ ...m, [mode]: !m[mode] }))}
                reviewedMap={reviewedMap}
                onToggleReview={toggleReview}
              />
            ))}
          </div>
        </>
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
  meeting: { icon: "💬", label: "会议 · 群聊共议" },
};

function ModeSection({ mode, agents, filter, open, onToggle, showAllDone, onToggleShowAll,
                       reviewedMap, onToggleReview }: {
  mode: string;
  agents: SubAgentProgress[];
  filter: "all" | "running";
  open: boolean;
  onToggle: () => void;
  showAllDone: boolean;
  onToggleShowAll: () => void;
  reviewedMap: Record<string, boolean>;
  onToggleReview: (id: string) => void;
}) {
  const meta = MODE_META[mode] ?? MODE_META.orchestrate;
  const runningList = agents.filter((a) => a.status === "running");
  const doneList = agents.filter((a) => a.status !== "running");
  const pendingReview = doneList.filter(
    (a) => a.changedFiles && a.changedFiles.length > 0 && !reviewedMap[a.subagentId || a.task]).length;

  // 仅运行中过滤：无活跃 agent 的组直接隐藏
  if (filter === "running" && runningList.length === 0) return null;

  // 折叠头部：统计行代替卡片堆（默认：全完成组自动折叠，活跃组展开）
  if (!open) {
    return (
      <div className="agent-mode-group">
        <div className="agent-mode-head collapsible" onClick={onToggle} title="点击展开分组">
          <span className="agent-mode-caret">▸</span>
          <span>{meta.icon}</span>{meta.label}
          <span className="agent-mode-stats">
            {runningList.length > 0 && <span className="agent-mode-stat live">运行中 {runningList.length}</span>}
            <span>已完成 {doneList.length}</span>
            {pendingReview > 0 && <span className="agent-mode-stat warn">⚠ 待审 {pendingReview}</span>}
          </span>
        </div>
      </div>
    );
  }

  // 完成卡片截断：默认最近 MAX_DONE_SHOWN 个（最近派生），"展开全部"按需
  const doneShown = showAllDone ? doneList : doneList.slice(-MAX_DONE_SHOWN);
  const doneHidden = doneList.length - doneShown.length;
  const cardProps = (a: SubAgentProgress, delivered = false) => ({
    key: a.subagentId || a.task,
    agent: a,
    delivered,
    reviewed: !!reviewedMap[a.subagentId || a.task],
    onToggleReview: () => onToggleReview(a.subagentId || a.task),
  });
  const moreBtn = doneHidden > 0 && (
    <button className="agent-group-more" onClick={onToggleShowAll}>
      ⋯ 展开全部已完成（{doneList.length}）
    </button>
  );

  return (
    <div className="agent-mode-group">
      <div className="agent-mode-head collapsible" onClick={onToggle} title="点击折叠分组">
        <span className="agent-mode-caret open">▾</span>
        <span>{meta.icon}</span>{meta.label}
        <span className="agent-mode-stats">
          {runningList.length > 0 && <span className="agent-mode-stat live">运行中 {runningList.length}</span>}
          <span>已完成 {doneList.length}</span>
          {pendingReview > 0 && <span className="agent-mode-stat warn">⚠ 待审 {pendingReview}</span>}
        </span>
      </div>
      {mode === "brainstorm" || mode === "meeting" ? (
        // 视角提案墙 / 会议发言流：卡片平铺（会议按发言序，运行中脉冲点标识未完）
        <div className="agent-brainstorm-wall">
          {(filter === "running" ? runningList : [...runningList, ...doneShown]).map((a) => (
            <AgentCard {...cardProps(a)} />
          ))}
          {moreBtn}
        </div>
      ) : mode === "debate" ? (
        // 对抗泳道：proposer（提案/修订） vs critic（批判）
        <DebateLanes agents={filter === "running" ? runningList : agents}
                     doneList={filter === "running" ? [] : doneShown}
                     doneHidden={doneHidden} onToggleShowAll={onToggleShowAll}
                     reviewedMap={reviewedMap} onToggleReview={onToggleReview} />
      ) : mode === "pipeline" ? (
        // 接力链：按派生序编号 + 箭头串联
        <div className="agent-pipeline-chain">
          {(filter === "running" ? runningList : [...runningList, ...doneShown])
            .sort((a, b) => (a.startedAt ?? 0) - (b.startedAt ?? 0))
            .map((a, i) => (
              <div key={a.subagentId || a.task} className="agent-pipeline-node">
                {i > 0 && <span className="agent-pipeline-arrow">↓</span>}
                <div className="agent-pipeline-step">
                  <span className="agent-pipeline-no">{i + 1}</span>
                  <AgentCard {...cardProps(a)} />
                </div>
              </div>
            ))}
          {moreBtn}
        </div>
      ) : (
        // orchestrate：竖排 kanban（运行中/已完成分区，卡片跨区流动）
        <>
          <div className="agent-section-head">
            <span className="agent-dot running" />运行中（{runningList.length}）
          </div>
          {runningList.map((a) => <AgentCard {...cardProps(a)} />)}
          {doneShown.length > 0 && (
            <>
              <div className="agent-section-head">
                <span className="agent-dot done" />已完成（{doneList.length}）
              </div>
              {doneShown.map((a) => <AgentCard {...cardProps(a, true)} />)}
            </>
          )}
          {moreBtn}
        </>
      )}
    </div>
  );
}

/** 辩论泳道：proposer vs critic 对垒（角色归位：critic 单独一组，其余为提案方） */
function DebateLanes({ agents, doneList, doneHidden, onToggleShowAll, reviewedMap, onToggleReview }: {
  agents: SubAgentProgress[];
  doneList: SubAgentProgress[];
  doneHidden: number;
  onToggleShowAll: () => void;
  reviewedMap: Record<string, boolean>;
  onToggleReview: (id: string) => void;
}) {
  const critics = agents.filter((a) => a.role === "critic");
  const proposers = agents.filter((a) => a.role !== "critic");
  const cardProps = (a: SubAgentProgress) => ({
    key: a.subagentId || a.task,
    agent: a,
    reviewed: !!reviewedMap[a.subagentId || a.task],
    onToggleReview: () => onToggleReview(a.subagentId || a.task),
  });
  return (
    <div className="agent-debate-lanes">
      <div className="agent-debate-lane">
        <div className="agent-debate-lane-head">提案方</div>
        {proposers.map((a) => <AgentCard {...cardProps(a)} />)}
        {proposers.length === 0 && <span className="mcp-empty-inline">（暂无）</span>}
      </div>
      <span className="agent-debate-vs">⇄</span>
      <div className="agent-debate-lane critic">
        <div className="agent-debate-lane-head">批判方</div>
        {critics.map((a) => <AgentCard {...cardProps(a)} />)}
        {critics.length === 0 && <span className="mcp-empty-inline">（暂无）</span>}
      </div>
      {doneHidden > 0 && (
        <button className="agent-group-more" onClick={onToggleShowAll}>
          ⋯ 展开全部已完成（{doneList.length}）
        </button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- 主组件

type PanelTabId = "context" | "todos" | "agents" | "mcp" | "background" | "tools";

// tab 条横向滑动：面板拖窄后 tab 队列整体滑动（不是拖动单个 tab）。
// 按住横向拖动 → scrollLeft 跟随位移；位移 < 5px 视为点击不拦截。
// 实现注意：不能用 setPointerCapture——capture 后浏览器把 click 派发到
// 捕获元素（容器）而非实际按下的 tab 按钮，导致切 tab 失效；因此拖动
// 跟随用 document 级监听（任意位置生效），click 保持原生目标。
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

    const onMove = (ev: PointerEvent) => {
      const d = drag.current;
      if (d.pointerId !== ev.pointerId) return;
      const dx = ev.clientX - d.startX;
      if (!d.moved && Math.abs(dx) < 5) return;
      if (!d.moved) {
        d.moved = true;
        el.classList.add("dragging");
      }
      el.scrollLeft = d.startScroll - dx;
    };
    const onEnd = (ev: PointerEvent) => {
      if (drag.current.pointerId !== ev.pointerId) return;
      drag.current.pointerId = null;
      el.classList.remove("dragging");
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", onEnd);
      document.removeEventListener("pointercancel", onEnd);
    };
    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", onEnd);
    document.addEventListener("pointercancel", onEnd);
  };

  // 拖动后松手的 click 不应触发切 tab（拖完松手 ≠ 点击）
  const wasDragged = () => {
    const d = drag.current;
    if (d.moved) {
      d.moved = false;
      return true;
    }
    return false;
  };

  return { ref, onPointerDown, wasDragged };
}

export default function ToolPanel({
  contextStats, mcpServers, tools, todos, agentBoard, orchestrator, backgroundTasks,
  collapsed, onToggleCollapsed, onKillBackground, activeTab, onTabChange,
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
  /** 受控 tab（可选）：聊天区状态条等外部入口可跳转到指定 tab */
  activeTab?: PanelTabId;
  onTabChange?: (tab: PanelTabId) => void;
}) {
  const [panelTabLocal, setPanelTabLocal] = useState<PanelTabId>("context");
  // 受控优先（外部传入），否则内部状态
  const panelTab = activeTab ?? panelTabLocal;
  const setPanelTab = (t: PanelTabId) => {
    setPanelTabLocal(t);
    onTabChange?.(t);
  };
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