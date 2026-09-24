// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { api } from "../api";
import type {
  BackgroundTaskInfo, ContextCallStats, ContextHistoryPoint, ContextMechanisms, ContextStats,
  ContextTaskStats, LiveSpeedStats, MCPServerStatus, SubAgentProgress, TodoItem,
} from "../types";

// ---------------------------------------------------------------- 上下文情况面板
//
// 三层信息架构（替代旧「账本式」label:value 罗列）：
//   ① 实时仪表：模型/状态 chip + 环形水位表 + 近 N 轮水位趋势
//   ② 账单卡  ：会话累计成本大字 + 本任务/本次调用 chip + 缓存命中率与省钱提示
//   ③ 平铺明细：紧凑 kv 网格（本任务/会话累计 + 计费单价），不做折叠

function fmt(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : n.toLocaleString();
}

/** 价格数据年龄（秒）→ 「刚刚 / X 分钟前 / X 小时前 / X 天前」 */
function fmtAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "未知";
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

function pct(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return `${(n * 100).toFixed(1)}%`;
}

/** 大数压缩展示：1,234,567 → 1.23M（横向空间有限时的辅助刻度） */
function compactTokens(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 10_000) return `${(n / 1000).toFixed(1)}k`;
  return n.toLocaleString();
}

/** 水位 sparkline 的 SVG 点串（viewBox 190×34，末点高亮） */
function sparkPoints(values: number[]): { line: string; lastX: number; lastY: number } {
  const w = 190;
  const h = 34;
  const pad = 3;
  const max = Math.max(...values, 1);
  const step = values.length > 1 ? w / (values.length - 1) : w;
  const coords = values.map((v, i) => {
    const x = i * step;
    const y = h - pad - (v / max) * (h - pad * 2);
    return [x, y] as const;
  });
  const line = coords.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const [lastX, lastY] = coords[coords.length - 1] ?? [w, h / 2];
  return { line, lastX, lastY };
}

/** 效率机制节省台账（v1.6.0）：观察打包 / 证据收据 / 压缩决策理由。 */
function MechanismRows({ mechanisms }: { mechanisms?: ContextMechanisms }) {
  if (!mechanisms) return null;
  const { obs_saved_tokens, obs_packed, reducer_saved_tokens, reducer_enabled, compaction_reason } = mechanisms;
  const reducerText = reducer_saved_tokens > 0
    ? fmt(reducer_saved_tokens)
    : reducer_enabled === false ? "未启用" : "—";
  const has = obs_saved_tokens > 0 || reducer_saved_tokens > 0 || obs_packed > 0
    || (compaction_reason && compaction_reason !== "pruned") || reducer_enabled === false;
  if (!has) return null;
  return (
    <>
      <div className="ctx2-ghead" title="SoL-Pi 式效率机制（v1.6.0）：大结果占位符 / 证据收据 / 压缩经济学">
        机制节省（本任务）
      </div>
      <span>观察打包 / 收据</span>
      <b>{obs_saved_tokens > 0 ? `${fmt(obs_saved_tokens)}（${obs_packed} 条）` : "—"} / {reducerText}</b>
      {compaction_reason && (
        <span>压缩决策</span>
      )}
      {compaction_reason && <b>{compaction_reason}</b>}
    </>
  );
}

/** 速度条文案：tok/s 保留 1 位（0 值不显示小数） */
function fmtTps(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  return v >= 100 ? v.toFixed(0) : v.toFixed(1);
}

/** 毫秒 → 秒（首字延迟展示用） */
function fmtSeconds(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "—";
  return `${(ms / 1000).toFixed(2)}s`;
}

/**
 * 本轮 token 速度条（变体 B）：主数字 + 细分（首字 / 本任务均）+ 速度趋势图。
 *
 * 两种状态由数据自然决定，**两者布局尺寸完全一致**（趋势图固定宽度）：
 *   - 生成中：liveSpeed 有值 → 显示估算值（≈ 前缀 + 「估算」小标 + 脉冲点）；
 *   - 本轮结束/空闲：显示 usage 校准后的精确值（+ task.avg_tps 任务平均）。
 *
 * 不显示「本轮生成中/本轮生成速度」这类状态文案、也不显示端到端速度：
 * 它们在两种状态下字数不同，会挤压右侧趋势图导致宽度跳动（图宽必须恒定）。
 */
function SpeedRow({ call, avgTps, speedTurns, live, history, running }: {
  call?: ContextCallStats;
  avgTps?: number | null;
  speedTurns?: number;
  live?: LiveSpeedStats | null;
  history?: ContextHistoryPoint[];
  running?: boolean;
}) {
  // 趋势线：只用真正有速度值的轮次（无计量的轮次会留空档，不能当 0 画）
  const series = (history ?? [])
    .map((h) => h.tps)
    .filter((v): v is number => typeof v === "number" && Number.isFinite(v));
  const spark = series.length >= 2 ? sparkPoints(series) : null;
  const seriesAvg = series.length > 0
    ? series.reduce((a, b) => a + b, 0) / series.length
    : null;

  // 生成中优先用实时估算值；否则用最近一次调用的精确结果
  const liveTps = live?.tps ?? null;
  const isLive = !!running && live != null && liveTps != null;
  const tps = isLive ? liveTps : (call?.tps_gen ?? null);
  const ttft = isLive ? (live?.ttft_ms ?? call?.ttft_ms ?? null) : (call?.ttft_ms ?? null);
  const estimated = isLive ? true : (call?.tps_estimated ?? false);

  // 全程无数据（旧载荷 / 适配器未计量）→ 整块不渲染，不占位
  if (tps == null && ttft == null && avgTps == null && !spark) return null;

  return (
    <div className="ctx2-speedrow">
      <div className="ctx2-speedmain">
        <div className="ctx2-speedbig">
          <b className={estimated ? "est" : undefined}>{isLive ? "≈ " : ""}{fmtTps(tps)}</b>
          <small>tok/s</small>
          {estimated && <span className="ctx2-esttag">估算</span>}
          {isLive && <i className="ctx2-dot run" />}
        </div>
        <div className="ctx2-speedsub">
          <span>首字 <b>{fmtSeconds(ttft)}</b></span>
          {avgTps != null && (
            <span title={`Σ输出 tokens ÷ Σ生成窗口（${speedTurns ?? 0} 轮）`}>
              本任务均 <b>{fmtTps(avgTps)}</b>
            </span>
          )}
        </div>
      </div>
      {spark && (
        <div className="ctx2-sparkwrap">
          <svg className="ctx2-spark" viewBox="0 0 190 34" preserveAspectRatio="none">
            <polygon className="ctx2-sparkfill speed" points={`${spark.line} 190,34 0,34`} />
            <polyline className="ctx2-sparkline speed" points={spark.line} />
            <circle className="ctx2-sparkdot speed" cx={spark.lastX} cy={spark.lastY} r="2.4" />
          </svg>
          <div className="ctx2-sparkcap">
            <span>近 {series.length} 轮速度</span>
            <span>{seriesAvg != null ? `平均 ${fmtTps(seriesAvg)} tok/s` : ""}</span>
          </div>
        </div>
      )}
    </div>
  );
}

function ContextPanel({ stats, history, running, liveSpeed, onCompact, compacting }: {
  stats: ContextStats | null;
  history?: ContextHistoryPoint[];
  running?: boolean;
  /** 流式生成中的实时速度（llm:progress，估算值） */
  liveSpeed?: LiveSpeedStats | null;
  /** 手动压缩入口（环形仪表点击触发，来自 App 层） */
  onCompact?: () => void;
  /** 压缩进行中（禁用环 + 扫掠动画 + 中间文字切换） */
  compacting?: boolean;
}) {
  if (!stats) {
    return <div className="tool-panel-empty">暂无上下文数据（发起对话后显示）</div>;
  }
  const window = stats.context_window || 0;
  const task = stats.task ?? ({} as ContextTaskStats);
  const session = stats.session ?? {};
  const pricing = stats.pricing;
  // 当前生效档：分时供应商（DeepSeek）处于空闲时段时，实际计费按 off_peak 档
  // （半价）。面板顶部的「已生效单价」必须显示这一档，否则会出现「标签写空闲·半价、
  // 数字却是高峰全价」的错位（pricing.input/output 始终是高峰基准价）。
  const effective = pricing
    ? (pricing.off_peak && pricing.off_peak_active
      ? {
          input_per_mtok: pricing.off_peak.input_per_mtok,
          output_per_mtok: pricing.off_peak.output_per_mtok,
          cache_hit_per_mtok: pricing.off_peak.cache_hit_per_mtok,
        }
      : {
          input_per_mtok: pricing.input_per_mtok,
          output_per_mtok: pricing.output_per_mtok,
          cache_hit_per_mtok: pricing.cache_hit_per_mtok,
        })
    : null;
  // 当前上下文水位 = 最近一次调用实际发出的 prompt（不是跨轮累加值）；
  // GET 刷新后无 task 段 → 回退会话压缩后水位（compact_session 写入）
  const prompt = task.last_prompt_tokens ?? task.prompt_tokens ?? session.last_prompt_tokens ?? 0;
  const ratio = window > 0 ? Math.min(1, Math.max(0, prompt / window)) : 0;
  const danger = ratio >= 0.9;
  // 「本次调用」：优先取 last 段；旧载荷无该字段时退化为累计值（保持可用）
  const call = task.last ?? {
    prompt_tokens: task.prompt_tokens ?? 0,
    output_tokens: task.output_tokens ?? 0,
    cache_hit_tokens: task.cache_hit_tokens ?? 0,
    cache_miss_tokens: task.cache_miss_tokens ?? 0,
    cost_estimate: task.cost_estimate,
  };
  const turns = task.turns ?? 0;

  // 水位趋势：前端在每轮 context:stats 时追加（App.tsx），仅 ≥2 点才画
  const levels = (history ?? []).map((h) => h.p).filter((v) => v > 0);
  const showSpark = levels.length >= 2;
  const spark = showSpark ? sparkPoints(levels) : null;
  const first = levels[0] ?? 0;
  const last = levels[levels.length - 1] ?? 0;
  const trendText = !showSpark ? ""
    : last > first * 1.03 ? "缓升 ↗"
    : last < first * 0.97 ? "回落 ↘"
    : "平稳 →";

  // 缓存帮整个会话省下多少钱：命中部分按「未命中全价 − 命中折扣价」的差额
  // 口径 = 会话累计（session.cache_hit_tokens，跨任务加总），与「会话累计成本」一致
  const saved = effective && (session.cache_hit_tokens ?? 0) > 0
    ? (session.cache_hit_tokens * (effective.input_per_mtok - effective.cache_hit_per_mtok)) / 1_000_000
    : null;

  // 环形仪表：r=32 → 周长 ≈ 201.06，dashoffset 控制进度
  const CIRC = 2 * Math.PI * 32;

  return (
    <div className="ctx2">
      <div className="ctx2-head">
        <span className="ctx2-chip" title="模型上下文窗口（models.dev 同步/内置表/手动覆盖）">
          <i className="ctx2-dot on" />{stats.model || "模型"} <small>{compactTokens(window)}</small>
        </span>
        <span className="ctx2-chip" title={running ? "本任务已执行的 LLM 轮数" : "当前无任务在运行"}>
          <i className={running ? "ctx2-dot run" : "ctx2-dot"} />
          {running ? `任务进行中 · 第 ${turns || "…"} 轮` : "空闲"}
        </span>
      </div>

      <div className="ctx2-gaugerow">
        <button
          type="button"
          className="ctx2-gauge"
          onClick={onCompact}
          disabled={!onCompact || compacting}
          title={compacting ? "正在压缩上下文…" : "点击压缩上下文（旧轮次摘要化，最近几轮原样保留）"}
          aria-label={compacting ? "压缩中" : "压缩上下文"}
        >
          <svg width="74" height="74" viewBox="0 0 74 74">
            <circle cx="37" cy="37" r="32" fill="none" stroke="var(--bg-3)" strokeWidth="7" />
            <g className={compacting ? "ctx2-arc-spin" : undefined}>
              <circle
                cx="37" cy="37" r="32" fill="none"
                className={danger ? "ctx2-arc danger" : "ctx2-arc"}
                strokeDasharray={compacting ? 90 : CIRC}
                strokeDashoffset={CIRC * (1 - ratio)}
              />
            </g>
          </svg>
          <div className="ctx2-gv">
            <b>{pct(ratio)}</b>
            <i>{compacting ? "压缩中…" : "已用"}</i>
          </div>
        </button>
        <div className="ctx2-gside">
          <div className="ctx2-lab">
            当前上下文
            {onCompact && <span className="ctx2-compact-hint"> · 🗜️ 点击环形压缩</span>}
          </div>
          <div className="ctx2-big">{fmt(prompt)}<small> / {compactTokens(window)}</small></div>
          {danger && <div className="ctx2-warn">≥90%，将触发自动压缩</div>}
          {spark && (
            <div className="ctx2-sparkwrap">
              <svg className="ctx2-spark" viewBox="0 0 190 34" preserveAspectRatio="none">
                <polygon className="ctx2-sparkfill" points={`${spark.line} 190,34 0,34`} />
                <polyline className="ctx2-sparkline" points={spark.line} />
                <circle className="ctx2-sparkdot" cx={spark.lastX} cy={spark.lastY} r="2.4" />
              </svg>
              <div className="ctx2-sparkcap">
                <span>近 {levels.length} 轮水位</span><span>{trendText}</span>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* 本轮 token 速度（变体 B）：主数字 + 首字/端到端/本任务均 + 速度趋势 */}
      <SpeedRow
        call={call}
        avgTps={task.avg_tps}
        speedTurns={task.speed_turns}
        live={liveSpeed}
        history={history}
        running={running}
      />

      <div className="ctx2-card">
        <div className="ctx2-cardhead">
          <span className="ctx2-cardtitle">本会话累计成本</span>
          {effective && (
            <span
              className="ctx2-price"
              title={
                `当前生效计费单价（每 M tokens）：输入 $${effective.input_per_mtok}`
                + ` / 输出 $${effective.output_per_mtok} / 缓存命中 $${effective.cache_hit_per_mtok}`
                + (pricing?.off_peak
                  ? `\n分时（DeepSeek）：高峰全价 输入 $${pricing.input_per_mtok}`
                    + ` / 输出 $${pricing.output_per_mtok}`
                    + `；空闲半价 输入 $${pricing.off_peak.input_per_mtok}`
                    + ` / 输出 $${pricing.off_peak.output_per_mtok}`
                  : "")
                + (pricing?.source ? `\n价格来源：${pricing.source}` : "")
              }
            >
              {pricing?.off_peak && (
                <span className={`ctx2-tag ${pricing.off_peak_active ? "offpeak" : "peak"}`}>
                  {pricing.off_peak_active ? "空闲·半价" : "高峰"}
                </span>
              )}
              ${effective.input_per_mtok}/${effective.output_per_mtok} 每M
            </span>
          )}
        </div>
        <div className="ctx2-cost">
          {session.cost_estimate != null ? `$${session.cost_estimate.toFixed(4)}` : "—"}
        </div>
        <div className="ctx2-chips">
          <div className="ctx2-mini">
            <i>本任务{turns ? `（${turns} 轮）` : ""}</i>
            <b>{task.cost_estimate != null ? `$${task.cost_estimate.toFixed(4)}` : "—"}</b>
          </div>
          <div className="ctx2-mini"><i>本次调用</i>
            <b>{call.cost_estimate != null ? `$${call.cost_estimate.toFixed(4)}` : "—"}</b>
          </div>
        </div>
        {pricing?.stale && (
          <div className="ctx2-stalehint" title="价格数据不自动联网更新；可在 设置 → 综合设置 → 立即同步定价数据">
            ⚠ 定价数据已过期，成本估算可能偏差——建议到「设置 → 综合设置」同步
          </div>
        )}
        {(task.cache_hit_rate != null || saved != null) && (
          <div className="ctx2-cachewrap">
            <div className="ctx2-meterrow"><span>缓存命中率（会话累计）</span><b>{pct(session.cache_hit_rate)}</b></div>
            <div className="ctx2-meter">
              <i style={{ width: `${Math.min(100, Math.max(0, (session.cache_hit_rate ?? 0) * 100))}%` }} />
            </div>
            {saved != null && saved > 0 && (
              <div className="ctx2-meterrow" title="整个会话所有任务的缓存命中部分，若全按未命中价计费需多花的金额（未命中全价 − 命中折扣价）">
                <span>缓存帮你省下（会话累计）</span><b>≈ ${saved.toFixed(4)}</b>
              </div>
            )}
          </div>
        )}
      </div>

      {/* 口径提示：本任务累计 ≈ 轮数 × 上下文（每轮整段重发），不能当当前水位看 */}
      <div className="ctx2-detail" title="本任务累计 ≈ 轮数 × 上下文：Agent 每轮都把整段历史重发给模型">
        <div className="ctx2-ghead">本任务累计</div>
        <span>输入 / 输出</span><b>{fmt(task.prompt_tokens)} / {fmt(task.output_tokens)}</b>
        <span>缓存命中 / 未命中</span><b>{fmt(task.cache_hit_tokens)} / {fmt(task.cache_miss_tokens)}</b>
        <span>工具调用 / 安全拦截</span><b>{task.tool_calls ?? 0} 次 / {task.blocked ?? 0} 次</b>
        <div className="ctx2-ghead">会话累计</div>
        <span>输入 / 输出</span><b>{fmt(session.prompt_tokens)} / {fmt(session.output_tokens)}</b>
        <span>上下文压缩 / 节省</span><b>{session.compression_count ?? 0} 次 / {fmt(session.compressed_tokens)}</b>
        <span>工具调用 / 安全拦截</span><b>{session.tool_calls ?? 0} 次 / {session.blocked ?? 0} 次</b>
        <MechanismRows mechanisms={stats.mechanisms} />
        {pricing && (
          <>
            <div className="ctx2-ghead">计费单价（每 M tokens）</div>
            {pricing.off_peak ? (
              <>
                <span>高峰全价</span>
                <b>${pricing.input_per_mtok} / ${pricing.output_per_mtok} / ${pricing.cache_hit_per_mtok}</b>
                <span>空闲半价</span>
                <b>${pricing.off_peak.input_per_mtok} / ${pricing.off_peak.output_per_mtok} / ${pricing.off_peak.cache_hit_per_mtok}</b>
                <span>当前生效档</span>
                <b>{pricing.off_peak_active ? "空闲·半价" : "高峰·全价"}</b>
              </>
            ) : (
              <>
                <span>输入 / 输出 / 缓存命中</span>
                <b>${pricing.input_per_mtok} / ${pricing.output_per_mtok} / ${pricing.cache_hit_per_mtok}</b>
              </>
            )}
            <span>价格来源 / 更新</span>
            <b>{pricing.source || "—"}{pricing.source_age_seconds != null ? ` · ${fmtAge(pricing.source_age_seconds)}` : ""}</b>
          </>
        )}
      </div>
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

/** TODOs 面板（竖排脊线看板）：三个状态组纵向排列，左侧脊线串联状态点。 */
const TODO_LANES: { status: TodoItem["status"]; label: string }[] = [
  { status: "in_progress", label: "进行中" },
  { status: "pending", label: "待办" },
  { status: "completed", label: "已完成" },
];

function TodosPanel({ todos }: { todos: TodoItem[] }) {
  if (!todos || todos.length === 0) {
    return <div className="tool-panel-empty">暂无 TODO（Agent 规划多步骤任务时自动生成）</div>;
  }
  const done = todos.filter((t) => t.status === "completed").length;
  const doing = todos.filter((t) => t.status === "in_progress").length;
  const ratio = todos.length > 0 ? done / todos.length : 0;
  const allDone = done === todos.length;

  return (
    <div className="todos-panel">
      <div className="todo2-progress">
        <div className="ctx2-meterrow">
          <span className="todo2-title">
            {allDone ? "✅ 全部完成" : `任务进度 ${done}/${todos.length}`}
            {doing > 0 ? ` · 进行中 ${doing}` : ""}
          </span>
          <b>{Math.round(ratio * 100)}%</b>
        </div>
        <div className="ctx2-meter">
          <i style={{ width: `${Math.min(100, Math.max(0, ratio * 100))}%` }} />
        </div>
      </div>
      <div className="todos-board">
        {TODO_LANES.map((lane) => {
          const items = todos.filter((t) => t.status === lane.status);
          return (
            <div className={`todo-lane lane-${lane.status}`} key={lane.status}>
              <div className="todo-lane-dotcol">
                <span className="todo-lane-dot" aria-hidden />
              </div>
              <div className="todo-lane-main">
                <div className="todo-lane-head">
                  {lane.label}
                  <span className="todo-lane-count">{items.length}</span>
                </div>
                <div className="todo-lane-cards">
                  {items.length === 0 ? (
                    <div className="todo-lane-empty" aria-hidden>—</div>
                  ) : (
                    items.map((t, i) => (
                      <div
                        key={i}
                        className={`todo-item todo-${t.status}`}
                        title={t.updated_at ? `更新于 ${new Date(t.updated_at * 1000).toLocaleString()}` : undefined}
                      >
                        <span className={t.status === "in_progress" ? "todo-mark todo-spin" : "todo-mark"} aria-hidden>
                          {TODO_MARKS[t.status]}
                        </span>
                        <span className="todo-content">{t.content}</span>
                      </div>
                    ))
                  )}
                </div>
              </div>
            </div>
          );
        })}
      </div>
      {allDone && <div className="todo2-alldone">本组任务已全部完成，Agent 可以开始下一个任务</div>}
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

/** 单张 agent 卡片：运行中显示实时步骤/流式尾部（可展开详情+取消）；完成显示可展开的 summary + 交付标记。 */
function AgentCard({ agent, delivered, reviewed, onToggleReview, onClose }: {
  agent: SubAgentProgress;
  delivered?: boolean;
  reviewed?: boolean;
  onToggleReview?: () => void;
  /** 手动取消（仅 running 态显示按钮；Agents 看板发起） */
  onClose?: (agentId: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const running = agent.status === "running";
  const elapsed = agent.startedAt ? Math.floor((Date.now() - agent.startedAt) / 1000) : null;
  const current = [...agent.steps].reverse().find((s) => s.status === "running");
  const doneSteps = agent.steps.filter((s) => s.status !== "running").length;
  const color = agentColor(`${agent.role}/${agent.subagentId || agent.task}`);
  const cancel = () => {
    if (!agent.subagentId || !onClose) return;
    if (window.confirm("确定取消该子 Agent？进行中的工作将丢失（已改动文件保留）。")) {
      onClose(agent.subagentId);
    }
  };
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
        {running && onClose && agent.subagentId && (
          <button
            className="agent-cancel-btn"
            title="取消该子 Agent（终止运行中的工作）"
            onClick={(e) => { e.stopPropagation(); cancel(); }}
          >✕</button>
        )}
      </div>
      <div className="agent-task" title={agent.task}>{agent.task || "（未描述任务）"}</div>
      {running ? (
        <>
          <div
            className="agent-live"
            style={{ cursor: "pointer" }}
            onClick={() => setExpanded((v) => !v)}
            title="点击展开/收起实时输出详情"
          >
            {current
              ? <span className="agent-step" title={current.brief}>▸ {current.tool}</span>
              : (doneSteps > 0 || agent.turn > 0
                  ? <span className="agent-step">已执行 {doneSteps} 步</span>
                  : <span className="agent-step">启动中…</span>)}
            {agent.streaming_text && (
              <span className="agent-stream">
                {expanded ? "▾ 收起实时输出" : agent.streaming_text.slice(-100)}
              </span>
            )}
          </div>
          {expanded && (
            <div className="agent-live-detail">
              {agent.steps.length > 0 && (
                <div className="agent-steps-list">
                  {agent.steps.map((s, i) => (
                    <div key={`${s.tool}-${i}`} className="agent-steps-row">
                      <span className={`agent-steps-mark ${s.status}`}>
                        {s.status === "running" ? "●" : s.status === "error" ? "✗" : "✓"}
                      </span>
                      <span className="agent-steps-tool">{s.tool}</span>
                      {s.durationMs != null && <span className="agent-steps-dur">{s.durationMs}ms</span>}
                      {s.brief && <span className="agent-steps-brief" title={s.brief}>{s.brief}</span>}
                    </div>
                  ))}
                </div>
              )}
              {agent.streaming_text && (
                <pre className="agent-stream-full">{agent.streaming_text.slice(-5000)}</pre>
              )}
            </div>
          )}
        </>
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

function AgentsPanel({ agents, orchestrator, onCloseAgent }: {
  agents: SubAgentProgress[];
  orchestrator?: { agentId: string; running: boolean };
  /** 手动取消子 Agent（透传 AgentCard 的 ✕ 按钮） */
  onCloseAgent?: (agentId: string) => void;
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
                onCloseAgent={onCloseAgent}
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
                       reviewedMap, onToggleReview, onCloseAgent }: {
  mode: string;
  agents: SubAgentProgress[];
  filter: "all" | "running";
  open: boolean;
  onToggle: () => void;
  showAllDone: boolean;
  onToggleShowAll: () => void;
  reviewedMap: Record<string, boolean>;
  onToggleReview: (id: string) => void;
  onCloseAgent?: (id: string) => void;
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
    agent: a,
    delivered,
    reviewed: !!reviewedMap[a.subagentId || a.task],
    onToggleReview: () => onToggleReview(a.subagentId || a.task),
    onClose: onCloseAgent,
  });
  const cardKey = (a: SubAgentProgress) => a.subagentId || a.task;
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
            <AgentCard key={cardKey(a)} {...cardProps(a)} />
          ))}
          {moreBtn}
        </div>
      ) : mode === "debate" ? (
        // 对抗泳道：proposer（提案/修订） vs critic（批判）
        <DebateLanes agents={filter === "running" ? runningList : agents}
                     doneList={filter === "running" ? [] : doneShown}
                     doneHidden={doneHidden} onToggleShowAll={onToggleShowAll}
                     reviewedMap={reviewedMap} onToggleReview={onToggleReview}
                     onCloseAgent={onCloseAgent} />
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
                  <AgentCard key={cardKey(a)} {...cardProps(a)} />
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
          {runningList.map((a) => <AgentCard key={cardKey(a)} {...cardProps(a)} />)}
          {doneShown.length > 0 && (
            <>
              <div className="agent-section-head">
                <span className="agent-dot done" />已完成（{doneList.length}）
              </div>
              {doneShown.map((a) => <AgentCard key={cardKey(a)} {...cardProps(a, true)} />)}
            </>
          )}
          {moreBtn}
        </>
      )}
    </div>
  );
}

/** 辩论泳道：proposer vs critic 对垒（角色归位：critic 单独一组，其余为提案方） */
function DebateLanes({ agents, doneList, doneHidden, onToggleShowAll, reviewedMap, onToggleReview, onCloseAgent }: {
  agents: SubAgentProgress[];
  doneList: SubAgentProgress[];
  doneHidden: number;
  onToggleShowAll: () => void;
  reviewedMap: Record<string, boolean>;
  onToggleReview: (id: string) => void;
  onCloseAgent?: (id: string) => void;
}) {
  const critics = agents.filter((a) => a.role === "critic");
  const proposers = agents.filter((a) => a.role !== "critic");
  const cardKey = (a: SubAgentProgress) => a.subagentId || a.task;
  const cardProps = (a: SubAgentProgress) => ({
    agent: a,
    reviewed: !!reviewedMap[a.subagentId || a.task],
    onToggleReview: () => onToggleReview(a.subagentId || a.task),
    onClose: onCloseAgent,
  });
  return (
    <div className="agent-debate-lanes">
      <div className="agent-debate-lane">
        <div className="agent-debate-lane-head">提案方</div>
        {proposers.map((a) => <AgentCard key={cardKey(a)} {...cardProps(a)} />)}
        {proposers.length === 0 && <span className="mcp-empty-inline">（暂无）</span>}
      </div>
      <span className="agent-debate-vs">⇄</span>
      <div className="agent-debate-lane critic">
        <div className="agent-debate-lane-head">批判方</div>
        {critics.map((a) => <AgentCard key={cardKey(a)} {...cardProps(a)} />)}
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
  contextStats, contextHistory, running, liveSpeed, mcpServers, tools, todos, agentBoard,
  orchestrator, backgroundTasks, collapsed, onToggleCollapsed, onKillBackground,
  activeTab, onTabChange, onCompact, compacting, onCloseAgent,
}: {
  contextStats: ContextStats | null;
  /** 每轮水位轨迹（面板趋势图数据源） */
  contextHistory?: ContextHistoryPoint[];
  /** 主任务是否运行中（状态 chip） */
  running?: boolean;
  /** 流式生成中的实时速度（llm:progress 估算值，随 SSE 到达） */
  liveSpeed?: LiveSpeedStats | null;
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
  /** 手动压缩上下文（环形仪表点击触发） */
  onCompact?: () => void;
  /** 压缩进行中 */
  compacting?: boolean;
  /** 手动取消子 Agent（Agents 看板 ✕ 按钮） */
  onCloseAgent?: (agentId: string) => void;
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

/** 渲染插件面板内容：JSON（lite-tree 通用决策树）→ 决策树；非 JSON → Markdown */
function PluginPanelContent({ markdown, pluginPanelBusy, onRefresh }: {
  markdown: string;
  pluginPanelBusy: boolean;
  onRefresh: () => void;
}) {
  // 尝试解析 JSON：**通用决策树协议 lite-tree**（插件输出该 schema 即渲染成树）
  let treeData: {
    /** 通用状态行（核心不解释具体含义）：text + tone + meta + chips */
    status: { text: string; tone?: string; meta?: string[]; chips?: string[]; reason?: string };
    trees: { nodes: { label: string; type: string; value?: number; threshold?: number; bar?: number; pass?: boolean; action?: string }[] }[];
    total: number;
  } | null = null;
  try {
    const parsed = JSON.parse(markdown);
    if (parsed && parsed.type === "lite-tree") treeData = parsed;
  } catch { /* 非 JSON，走 Markdown */ }

  if (treeData) {
    const s = treeData.status;
    return (
      <div style={{ padding: "14px 16px", fontSize: 13, lineHeight: 1.7 }}>
        {/* 状态行 */}
        <div style={{
          display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap",
          borderBottom: "1px solid var(--border)", paddingBottom: 8, marginBottom: 12,
        }}>
          <span style={{
            color: s.tone === "ok" ? "var(--green)" : s.tone === "warn" ? "var(--yellow)" : "var(--text-1)",
            fontWeight: 600,
          }}>{s.text}</span>
          {(s.meta ?? []).map((m, i) => <span key={i} style={{ color: "var(--text-2)" }}>{m}</span>)}
          {(s.chips ?? []).map((c, i) => (
            <span key={i} className="chip" style={{
              color: "var(--text-1)", fontSize: 11.5, border: "1px solid var(--border)",
              borderRadius: 4, padding: "1px 6px",
            }}>{c}</span>
          ))}
          {s.reason && <span style={{ color: "var(--text-2)", fontSize: 12 }}>{s.reason}</span>}
          <button
            disabled={pluginPanelBusy}
            onClick={onRefresh}
            style={{
              marginLeft: "auto", appearance: "none", border: "none", background: "transparent",
              color: pluginPanelBusy ? "var(--text-2)" : "var(--text-1)",
              fontSize: 11.5, cursor: "pointer", padding: "2px 6px",
            }}>
            {pluginPanelBusy ? "…" : "↻ 刷新"}
          </button>
        </div>

        {/* 决策树 */}
        {treeData.trees.length === 0 ? (
          <div style={{ color: "var(--text-2)", fontSize: 12.5, padding: "20px 0", textAlign: "center" }}>
            暂无判断记录。发起对话后，每次判断的推理链会在这里展示。
          </div>
        ) : (
          treeData.trees.map((tree, ti) => (
            <div key={ti} style={{ marginBottom: ti < treeData.trees.length - 1 ? 20 : 0 }}>
              {tree.nodes.map((node, ni) => {
                const isLast = ni === tree.nodes.length - 1;
                const isAction = node.type === "action";
                const isVerdict = node.type === "verdict";
                const isConf = node.type === "confidence";
                const isAccepted = node.type === "accepted";
                const barLen = node.bar ?? 0;

                return (
                  <div key={ni} style={{ display: "flex", alignItems: "flex-start" }}>
                    {/* 左侧连线列 */}
                    <div style={{
                      width: 28, display: "flex", flexDirection: "column", alignItems: "center",
                      paddingTop: 4, flexShrink: 0,
                    }}>
                      {/* 圆点 */}
                      <div style={{
                        width: 7, height: 7, borderRadius: "50%",
                        background: isAction
                          ? node.action === "block" ? "var(--red)"
                            : node.action === "allow" ? "var(--green)"
                            : node.action === "answer" ? "var(--accent)"
                            : "var(--text-2)"
                          : isVerdict ? "var(--accent-2)"
                          : isConf ? (node.pass ? "var(--green)" : "var(--yellow)")
                          : isAccepted ? (node.pass ? "var(--green)" : "var(--yellow)")
                          : "var(--border)",
                        flexShrink: 0,
                      }} />
                      {/* 竖线（非最后节点） */}
                      {!isLast && (
                        <div style={{
                          width: 1.5, flex: 1, minHeight: 22,
                          background: "var(--border)",
                        }} />
                      )}
                    </div>

                    {/* 内容列 */}
                    <div style={{
                      flex: 1, paddingBottom: isLast ? 0 : 10, minWidth: 0,
                    }}>
                      {/* 标签 */}
                      <span style={{
                        fontSize: 12.5,
                        color: isAction
                          ? node.action === "block" ? "var(--red)"
                            : node.action === "allow" ? "var(--green)"
                            : node.action === "answer" ? "var(--accent)"
                            : "var(--text-2)"
                          : isVerdict ? "var(--text-0)"
                          : isAccepted ? (node.pass ? "var(--green)" : "var(--yellow)")
                          : "var(--text-1)",
                        fontWeight: isVerdict || isAction || isAccepted ? 600 : 400,
                      }}>
                        {node.label}
                      </span>

                      {/* 置信度条 */}
                      {isConf && (
                        <span style={{
                          marginLeft: 8, fontFamily: "var(--font-mono)", fontSize: 11,
                          color: node.pass ? "var(--green)" : "var(--yellow)",
                        }}>
                          {"█".repeat(barLen)}{"░".repeat(10 - barLen)}
                          <span style={{ marginLeft: 6, color: "var(--text-2)" }}>
                            ≥ {node.threshold?.toFixed(2)}
                          </span>
                        </span>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          ))
        )}

        {/* 底部 */}
        <div style={{
          marginTop: 12, paddingTop: 8, borderTop: "1px solid var(--border)",
          fontSize: 11, color: "var(--text-2)",
        }}>
          共 {treeData.total} 次判断 · 仅存本进程内存
        </div>
      </div>
    );
  }

  // 非 JSON → Markdown 渲染
  return (
    <div className="plugin-panel-md" style={{ padding: "12px 14px", fontSize: 13, lineHeight: 1.7 }}>
      <div style={{
        display: "flex", justifyContent: "flex-end",
        borderBottom: "1px solid var(--border)", paddingBottom: 6, marginBottom: 10,
      }}>
        <button
          disabled={pluginPanelBusy}
          onClick={onRefresh}
          style={{
            appearance: "none", border: "none", background: "transparent",
            color: pluginPanelBusy ? "var(--text-2)" : "var(--text-1)",
            fontSize: 11.5, cursor: "pointer", padding: "2px 6px",
          }}>
          {pluginPanelBusy ? "…" : "↻ 刷新"}
        </button>
      </div>
      <ReactMarkdown remarkPlugins={[remarkGfm]}>
        {markdown || "加载中…"}
      </ReactMarkdown>
    </div>
  );
}

  // 通用插件 UI 协议：右栏动态 tab —— 插件在 contributes.panels 里声明即出现
  type PluginPanelRef = { plugin: string; id: string; title: string; icon?: string };
  const [pluginPanels, setPluginPanels] = useState<PluginPanelRef[]>([]);
  const [pluginPanelMd, setPluginPanelMd] = useState<Record<string, string>>({});
  const [pluginPanelBusy, setPluginPanelBusy] = useState(false);
  const panelKeyOf = (x: PluginPanelRef) => `plugin:${x.plugin}:${x.id}`;
  // 本次"激活期间"已抓取的 key：切到别的 tab 时清空 → 切回即重抓（面板是日志，不能缓存）
  const loadedPanelRef = useRef<string>("");

  const loadPluginPanel = useCallback(async (plugin: string, id: string) => {
    const key = `plugin:${plugin}:${id}`;
    setPluginPanelBusy(true);
    try {
      const r = await api.pluginPanel(plugin, id);
      setPluginPanelMd((prev) => ({ ...prev, [key]: r.markdown || "（面板为空）" }));
    } catch (e) {
      setPluginPanelMd((prev) => ({ ...prev, [key]: `读取失败：${(e as Error).message}` }));
    } finally {
      setPluginPanelBusy(false);
    }
  }, []);

  useEffect(() => {
    void api.plugins()
      .then((r) => {
        const out: PluginPanelRef[] = [];
        for (const p of r.plugins ?? []) {
          for (const pane of p.contributes?.panels ?? []) {
            out.push({ plugin: p.name, id: pane.id, title: pane.title || pane.id, icon: pane.icon });
          }
        }
        setPluginPanels(out);
      })
      .catch(() => { /* 插件列表不可用时静默：不影响内置 tab */ });
  }, []);

  useEffect(() => {
    const hit = pluginPanels.find((x) => panelKeyOf(x) === panelTab);
    if (!hit) { loadedPanelRef.current = ""; return; }   // 切走 → 下次切回重抓
    const key = panelKeyOf(hit);
    if (loadedPanelRef.current === key) return;          // 本次激活已抓过
    loadedPanelRef.current = key;
    void loadPluginPanel(hit.plugin, hit.id);
  }, [panelTab, pluginPanels, loadPluginPanel]);

  const TABS: { id: PanelTabId; label: string; title?: string }[] = [
    { id: "context", label: "上下文" },
    { id: "todos", label: todos.length ? `TODOs ${todoDone}/${todos.length}` : "TODOs",
      title: todos.length ? `进度 ${todoDone}/${todos.length}` : "Agent 规划多步骤任务时生成 TODO 清单" },
    { id: "agents", label: runningAgents > 0 ? `Agents (${runningAgents})` : "Agents",
      title: "多 Agent 看板：运行中/已完成 竖排交接视图" },
    { id: "background", label: runningCount > 0 ? `后台 (${runningCount})` : "后台" },
    { id: "tools", label: "工具" },
    // MCP 排在最后：它是低频的「配置/状态」类入口，日常主要看 工具/后台
    { id: "mcp", label: "MCP" },
    // 插件声明的面板（通用插件 UI 协议；无插件时为空数组，不影响内置 tab）
    ...pluginPanels.map((x) => ({
      id: panelKeyOf(x) as PanelTabId,
      label: x.title,
      title: `来自插件 ${x.plugin} 的面板`,
    })),
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
          ? <ContextPanel stats={contextStats} history={contextHistory} running={running}
              liveSpeed={liveSpeed}
              onCompact={onCompact} compacting={compacting} />
          : panelTab === "todos"
            ? <TodosPanel todos={todos} />
            : panelTab === "agents"
              ? <AgentsPanel agents={agentBoard ?? []} orchestrator={orchestrator} onCloseAgent={onCloseAgent} />
              : panelTab === "mcp"
                ? <McpPanel servers={mcpServers} />
                : panelTab === "background"
                  ? <BackgroundPanel tasks={backgroundTasks ?? []} onKill={onKillBackground ?? (() => {})} />
                  : pluginPanels.some((x) => panelKeyOf(x) === panelTab)
                    ? (
                      <PluginPanelContent
                        markdown={pluginPanelMd[panelTab] ?? ""}
                        pluginPanelBusy={pluginPanelBusy}
                        onRefresh={() => {
                          const hit = pluginPanels.find((x) => panelKeyOf(x) === panelTab);
                          if (hit) void loadPluginPanel(hit.plugin, hit.id);
                        }}
                      />
                    )
                    : <ToolsPanel tools={tools} />}
      </div>
    </aside>
  );
}