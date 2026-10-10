// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 原生富内容块（intelligent UI / generative UI 思路）：
// Agent 在回复里输出「数据 + 组件类型」围栏（```chart / ```cards / ```kpi /
// ```timeline / ```table / ```compare / ```board / ```progress / ```interactive），
// 前端用应用设计系统原生渲染——不生成 HTML 文档/图片文件，随文字流式
// 出现，样式与应用一致。同时提供 rewriteLocalUrls / toFileUrl：本地
// 相对路径统一 rewrite 到 /api/files/raw（复用后端越界检查与鉴权），
// 修复 iframe / 卡片内相对路径断链。

import { useState } from "react";

// ---------------------------------------------------------------- 本地资源 URL

/** 本地相对路径 → /api/files/raw（http(s)/data//api/ 原样）。 */
export function toFileUrl(p: string): string {
  return /^(https?:|data:|\/api\/)/i.test(p) ? p : `/api/files/raw?path=${encodeURIComponent(p)}`;
}

/** HTML 串内 src/href（双/单引号）与 CSS url() 的相对路径 rewrite。
 *  lookbehind 防误伤 data-src 等复合属性名；http(s)/data/#/mailto//api/ 原样。 */
export function rewriteLocalUrls(html: string): string {
  let out = html.replace(
    /(?<![\w-])(src|href)\s*=\s*(?:"([^"]*)"|'([^']*)')/gi,
    (full: string, attr: string, dq?: string, sq?: string) => {
      const val = dq !== undefined ? dq : sq;
      if (val === undefined || val === "" || /^(https?:|data:|#|mailto:|\/api\/)/i.test(val)) return full;
      return `${attr}="/api/files/raw?path=${encodeURIComponent(val)}"`;
    },
  );
  out = out.replace(/url\(\s*(['"]?)([^'")]+)\1\s*\)/gi, (full: string, _q: string, val: string) => {
    if (!val || /^(https?:|data:|\/api\/|#)/i.test(val)) return full;
    return `url("/api/files/raw?path=${encodeURIComponent(val)}")`;
  });
  return out;
}

// ---------------------------------------------------------------- 规格与解析

export interface ChartSeries {
  name?: string;
  data: number[];
}

export interface ChartSpec {
  type: "bar" | "hbar" | "line" | "area" | "pie" | "radar";
  title?: string;
  unit?: string;
  labels: string[];
  series: ChartSeries[];
}

export interface CardItem {
  title?: string;
  image?: string;
  price?: string;
  desc?: string;
  tags?: string[];
  url?: string;
}

export interface CardsSpec {
  title?: string;
  items: CardItem[];
}

/** 指标卡：一行 3-4 个关键数字（可带环比涨跌）。汇报/复盘最常用。 */
export interface KpiItem {
  label: string;
  value: string;
  /** 环比/同比等变化文案；up=向好（绿）/ down=变差（红）/ flat=中性 */
  delta?: string;
  trend?: "up" | "down" | "flat";
  /** 补充说明（口径、时间范围） */
  note?: string;
}

export interface KpiSpec {
  title?: string;
  items: KpiItem[];
}

/** 时间线：带时间的进展/事件序列（发布节奏、事故复盘、里程碑）。 */
export interface TimelineItem {
  time?: string;
  title: string;
  desc?: string;
  status?: "done" | "active" | "pending";
}

export interface TimelineSpec {
  title?: string;
  items: TimelineItem[];
}

/** 数据表：结构化多列对比（数值列右对齐）。 */
export interface TableSpec {
  title?: string;
  columns: string[];
  rows: string[][];
  /** 数值列下标（右对齐 + 等宽数字）；缺省时按内容自动判定 */
  numeric?: number[];
}

/** 左右对比：方案/版本/前后差异的并排对照（取舍场景）。 */
export interface CompareSide {
  title: string;
  items: string[];
  tone?: "good" | "bad" | "neutral";
}

export interface CompareSpec {
  title?: string;
  left: CompareSide;
  right: CompareSide;
}

/** 看板：分组列（待办/进行中/完成），排期与分工场景。 */
export interface BoardCard {
  title: string;
  desc?: string;
  tag?: string;
  tone?: "good" | "bad" | "neutral";
}

export interface BoardColumn {
  name: string;
  cards: BoardCard[];
}

export interface BoardSpec {
  title?: string;
  columns: BoardColumn[];
}

/** 进度条：多项目标完成度（OKR、体检项、覆盖率）。一眼看出短板。 */
export interface ProgressItem {
  label: string;
  /** 完成百分比 0-100（越界自动 clamp） */
  value: number;
  /** 数值后缀文案，如「82%」「12/15」；缺省显示百分比 */
  display?: string;
  /** 补充说明（口径、目标值） */
  note?: string;
}

export interface ProgressSpec {
  title?: string;
  items: ProgressItem[];
}

/** 交互式图解：分块 + 点击切换说明（GPT Intelligent UI 的「可交互图解」）。
 *  纯前端 state 切换，无脚本执行——沙箱安全模型不变。 */
export interface InteractivePart {
  /** 分块名（显示在可点击标签上），如「Frame」「车轮」 */
  name: string;
  /** 该分块的说明（支持多行） */
  desc: string;
  /** 数值/规格补充，如「700c」「11-32T」 */
  meta?: string;
}

export interface InteractiveSpec {
  title?: string;
  /** 图解总览（一句话，始终显示） */
  summary?: string;
  /** 可点击分块（首个默认选中） */
  parts: InteractivePart[];
  /** 是否显示分块序号（结构拆解类默认可读性更好） */
  numbered?: boolean;
}

export type RichSegment =
  | { kind: "chart"; spec: ChartSpec }
  | { kind: "cards"; spec: CardsSpec }
  | { kind: "kpi"; spec: KpiSpec }
  | { kind: "timeline"; spec: TimelineSpec }
  | { kind: "table"; spec: TableSpec }
  | { kind: "compare"; spec: CompareSpec }
  | { kind: "board"; spec: BoardSpec }
  | { kind: "progress"; spec: ProgressSpec }
  | { kind: "interactive"; spec: InteractiveSpec };

/** 富块围栏语言（ChatView 分段正则与解析器共用一份，防两处漂移）。 */
export const RICH_LANGS = [
  "chart", "cards", "kpi", "timeline", "table", "compare", "board", "progress",
  "interactive",
] as const;

function toNums(v: unknown): number[] {
  if (!Array.isArray(v)) return [];
  return v.map((x) => {
    const n = typeof x === "number" ? x : Number(x);
    return Number.isFinite(n) ? n : 0;
  });
}

/** 解析富内容围栏（```chart / ```cards / ```kpi / ```timeline / ```table /
 *  ```compare / ```board）内容；不可渲染（JSON 无效 / 形状不对）返回 null，
 *  调用方保留为普通代码块。规格宽松：单系列可省略 series 直接 data。 */
export function tryParseRichBlock(lang: string, raw: string): RichSegment | null {
  const l = lang.toLowerCase();
  if (!(RICH_LANGS as readonly string[]).includes(l)) return null;
  let o: unknown;
  try {
    o = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!o || typeof o !== "object" || Array.isArray(o)) return null;
  const rec = o as Record<string, unknown>;

  const title = typeof rec.title === "string" && rec.title.trim()
    ? rec.title.trim().slice(0, 80) : undefined;
  const str = (v: unknown, max: number): string | undefined =>
    typeof v === "string" && v.trim() ? v.trim().slice(0, max) : undefined;
  const strList = (v: unknown, max: number, per = 200): string[] =>
    (Array.isArray(v) ? v : [])
      .filter((x): x is string => typeof x === "string" && !!x.trim())
      .map((x) => x.trim().slice(0, per))
      .slice(0, max);

  if (l === "kpi") {
    const items: KpiItem[] = (Array.isArray(rec.items) ? rec.items : [])
      .filter((it): it is Record<string, unknown> => !!it && typeof it === "object")
      .map((it): KpiItem => {
        const tr = typeof it.trend === "string" ? it.trend.toLowerCase() : "";
        const trend: KpiItem["trend"] =
          tr === "up" || tr === "down" || tr === "flat" ? tr : undefined;
        return {
          label: str(it.label, 40) ?? "",
          value: str(it.value, 30) ?? "",
          delta: str(it.delta, 30),
          trend,
          note: str(it.note, 60),
        };
      })
      .filter((it) => it.label && it.value)
      .slice(0, 8);
    if (!items.length) return null;
    return { kind: "kpi", spec: { items, ...(title ? { title } : {}) } };
  }

  if (l === "timeline") {
    const items: TimelineItem[] = (Array.isArray(rec.items) ? rec.items : [])
      .filter((it): it is Record<string, unknown> => !!it && typeof it === "object")
      .map((it): TimelineItem => {
        const st = typeof it.status === "string" ? it.status.toLowerCase() : "";
        const status: TimelineItem["status"] =
          st === "done" || st === "active" || st === "pending" ? st : undefined;
        return {
          time: str(it.time, 30),
          title: str(it.title, 80) ?? "",
          desc: str(it.desc, 300),
          status,
        };
      })
      .filter((it) => it.title)
      .slice(0, 40);
    if (!items.length) return null;
    return { kind: "timeline", spec: { items, ...(title ? { title } : {}) } };
  }

  if (l === "table") {
    const columns = strList(rec.columns, 10, 40);
    const rows = (Array.isArray(rec.rows) ? rec.rows : [])
      .filter((r): r is unknown[] => Array.isArray(r))
      .map((r) => r.slice(0, columns.length || 10)
        .map((c) => (c === null || c === undefined ? "" : String(c).slice(0, 120))))
      .slice(0, 100);
    if (!columns.length || !rows.length) return null;
    const numeric = Array.isArray(rec.numeric)
      ? rec.numeric.filter((x): x is number => typeof x === "number" && x >= 0 && x < columns.length)
      : [];
    return {
      kind: "table",
      spec: { columns, rows, ...(numeric.length ? { numeric } : {}), ...(title ? { title } : {}) },
    };
  }

  if (l === "compare") {
    const side = (v: unknown): CompareSide | null => {
      if (!v || typeof v !== "object" || Array.isArray(v)) return null;
      const s = v as Record<string, unknown>;
      const t = str(s.title, 40);
      const items = strList(s.items, 10);
      if (!t || !items.length) return null;
      const tn = typeof s.tone === "string" ? s.tone.toLowerCase() : "";
      const tone: CompareSide["tone"] =
        tn === "good" || tn === "bad" || tn === "neutral" ? tn : undefined;
      return { title: t, items, tone };
    };
    const left = side(rec.left);
    const right = side(rec.right);
    if (!left || !right) return null;
    return { kind: "compare", spec: { left, right, ...(title ? { title } : {}) } };
  }

  if (l === "board") {
    const columns: BoardColumn[] = (Array.isArray(rec.columns) ? rec.columns : [])
      .filter((c): c is Record<string, unknown> => !!c && typeof c === "object")
      .map((c) => {
        const cards: BoardCard[] = (Array.isArray(c.cards) ? c.cards : [])
          .filter((k): k is Record<string, unknown> => !!k && typeof k === "object")
          .map((k): BoardCard => {
            const tn = typeof k.tone === "string" ? k.tone.toLowerCase() : "";
            const tone: BoardCard["tone"] =
              tn === "good" || tn === "bad" || tn === "neutral" ? tn : undefined;
            return {
              title: str(k.title, 80) ?? "",
              desc: str(k.desc, 200),
              tag: str(k.tag, 20),
              tone,
            };
          })
          .filter((k) => k.title)
          .slice(0, 30);
        return { name: str(c.name, 30) ?? "", cards };
      })
      .filter((c) => c.name)
      .slice(0, 6);
    if (!columns.length) return null;
    return { kind: "board", spec: { columns, ...(title ? { title } : {}) } };
  }

  if (l === "progress") {
    const clamp = (n: number) => Math.max(0, Math.min(100, n));
    const items: ProgressItem[] = (Array.isArray(rec.items) ? rec.items : [])
      .filter((it): it is Record<string, unknown> => !!it && typeof it === "object")
      .map((it): ProgressItem => {
        const raw = typeof it.value === "number" ? it.value : Number(it.value);
        return {
          label: str(it.label, 60) ?? "",
          value: Number.isFinite(raw) ? clamp(raw) : 0,
          display: str(it.display, 20),
          note: str(it.note, 60),
        };
      })
      .filter((it) => it.label)
      .slice(0, 20);
    if (!items.length) return null;
    return { kind: "progress", spec: { items, ...(title ? { title } : {}) } };
  }

  if (l === "interactive") {
    const parts: InteractivePart[] = (Array.isArray(rec.parts) ? rec.parts : [])
      .filter((it): it is Record<string, unknown> => !!it && typeof it === "object")
      .map((it): InteractivePart => ({
        name: str(it.name, 40) ?? "",
        desc: str(it.desc, 1200) ?? "",
        meta: str(it.meta, 40),
      }))
      .filter((it) => it.name && it.desc)
      .slice(0, 12);
    if (!parts.length) return null;
    const spec: InteractiveSpec = { parts };
    if (title) spec.title = title;
    const summary = str(rec.summary, 200);
    if (summary) spec.summary = summary;
    if (rec.numbered === true) spec.numbered = true;
    return { kind: "interactive", spec };
  }

  if (l === "chart") {
    const t = typeof rec.type === "string" ? rec.type.toLowerCase().trim() : "";
    let type: ChartSpec["type"] = "bar";
    if (t === "line" || t === "area" || t === "pie" || t === "radar" || t === "bar") type = t;
    else if (t === "hbar" || t === "horizontal" || t === "horizontal-bar" || t === "horizontal_bar") type = "hbar";

    const series: ChartSeries[] = (Array.isArray(rec.series) ? rec.series : [])
      .filter((s): s is Record<string, unknown> => !!s && typeof s === "object")
      .map((s) => ({
        name: typeof s.name === "string" && s.name.trim() ? s.name.trim() : undefined,
        data: toNums(s.data),
      }))
      .filter((s) => s.data.length > 0);
    if (!series.length) {
      const d = toNums(rec.data);
      if (d.length) {
        series.push({
          name: typeof rec.seriesName === "string" && rec.seriesName.trim() ? rec.seriesName.trim() : undefined,
          data: d,
        });
      }
    }
    if (!series.length) return null;

    let labels: string[] | null = null;
    if (Array.isArray(rec.labels)) {
      labels = rec.labels.map((x) => (typeof x === "string" || typeof x === "number") ? String(x) : "");
    }
    if (!labels || !labels.length) labels = series[0].data.map((_, i) => String(i + 1));
    const n = Math.max(labels.length, ...series.map((s) => s.data.length));
    while (labels.length < n) labels.push(String(labels.length + 1));
    for (const s of series) while (s.data.length < n) s.data.push(0);

    const spec: ChartSpec = {
      type,
      labels: labels.slice(0, 60),
      series: series.slice(0, 8).map((s) => ({ ...s, data: s.data.slice(0, 60) })),
    };
    if (typeof rec.title === "string" && rec.title.trim()) spec.title = rec.title.trim().slice(0, 80);
    if (typeof rec.unit === "string" && rec.unit.trim()) spec.unit = rec.unit.trim().slice(0, 12);
    return { kind: "chart", spec };
  }

  // cards
  const items: CardItem[] = (Array.isArray(rec.items) ? rec.items : [])
    .filter((it): it is Record<string, unknown> => !!it && typeof it === "object")
    .map((it) => ({
      title: typeof it.title === "string" && it.title.trim() ? it.title.trim().slice(0, 80) : undefined,
      image: typeof it.image === "string" && it.image.trim() ? it.image.trim() : undefined,
      price: typeof it.price === "string" && it.price.trim() ? it.price.trim().slice(0, 40) : undefined,
      desc: typeof it.desc === "string" && it.desc.trim() ? it.desc.trim().slice(0, 300) : undefined,
      url: typeof it.url === "string" && /^https?:/i.test(it.url) ? it.url : undefined,
      tags: Array.isArray(it.tags)
        ? it.tags.filter((x): x is string => typeof x === "string" && !!x.trim()).slice(0, 6)
        : undefined,
    }))
    .filter((it) => it.title || it.image)
    .slice(0, 30);
  if (!items.length) return null;
  const spec: CardsSpec = { items };
  if (typeof rec.title === "string" && rec.title.trim()) spec.title = rec.title.trim().slice(0, 80);
  return { kind: "cards", spec };
}

// ---------------------------------------------------------------- 图表渲染（SVG 自绘）

const PALETTE = ["#4f8cff", "#8b5cf6", "#3fb950", "#e3b341", "#f778ba", "#39c5cf", "#ff7b72", "#d2a8ff"];
const C_GRID = "#30363d";
const C_DIM = "#8b949e";
const C_MAIN = "#e6edf3";

function fmtNum(n: number): string {
  const abs = Math.abs(n);
  if (abs >= 1e8) return `${+(n / 1e8).toFixed(1)}亿`;
  if (abs >= 1e4) return `${+(n / 1e4).toFixed(1)}万`;
  if (abs >= 1000) return n.toLocaleString("en-US", { maximumFractionDigits: 0 });
  return String(Math.round(n * 100) / 100);
}

function trunc(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

/** 向上取整到 1/2/2.5/5×10^k 的「好看」刻度上限。 */
function niceMax(v: number): number {
  if (v <= 0) return 1;
  const exp = Math.floor(Math.log10(v));
  const base = Math.pow(10, exp);
  const f = v / base;
  const nf = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10;
  return nf * base;
}

function seriesMax(spec: ChartSpec): number {
  return niceMax(Math.max(0, ...spec.series.flatMap((s) => s.data)));
}

function YGrid({ max, plotH, y0, padL, w }: { max: number; plotH: number; y0: number; padL: number; w: number }) {
  return (
    <>
      {[0, 1, 2, 3, 4].map((i) => {
        const t = (max * i) / 4;
        const y = y0 - (t / max) * plotH;
        return (
          <g key={i}>
            <line x1={padL} x2={w} y1={y} y2={y} stroke={C_GRID} strokeWidth={1} />
            <text x={padL - 6} y={y + 3.5} textAnchor="end" fontSize={10} fill={C_DIM}>{fmtNum(t)}</text>
          </g>
        );
      })}
    </>
  );
}

function BarChart({ spec }: { spec: ChartSpec }) {
  const W = 640, padL = 50, padR = 14, padT = 16, plotH = 230, padB = 44;
  const w = W - padR;
  const n = spec.labels.length;
  const max = seriesMax(spec);
  const y0 = padT + plotH;
  const groupW = (w - padL) / n;
  const ns = spec.series.length;
  const barW = Math.max(4, Math.min(30, (groupW * 0.6) / ns));
  const showVals = n <= 10 && ns === 1;
  const blockW = ns * barW + (ns - 1) * 3;
  return (
    <svg viewBox={`0 0 ${W} ${padT + plotH + padB}`} className="md-chart-svg" role="img">
      <YGrid max={max} plotH={plotH} y0={y0} padL={padL} w={w} />
      {spec.labels.map((lb, i) => (
        <text
          key={i}
          x={padL + i * groupW + groupW / 2}
          y={y0 + 16}
          textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"}
          fontSize={10.5}
          fill={C_DIM}
        >
          {trunc(lb, 10)}
        </text>
      ))}
      {spec.series.map((s, si) =>
        s.data.map((v, i) => {
          const h = Math.max(v > 0 ? 2 : 0, (Math.max(0, v) / max) * plotH);
          const x = padL + i * groupW + groupW / 2 - blockW / 2 + si * (barW + 3);
          const y = y0 - h;
          return (
            <g key={`${si}-${i}`}>
              <rect x={x} y={y} width={barW} height={h} rx={2} fill={PALETTE[si % PALETTE.length]} />
              {showVals && v > 0 && (
                <text x={x + barW / 2} y={y - 4} textAnchor="middle" fontSize={9.5} fill={C_DIM}>{fmtNum(v)}</text>
              )}
            </g>
          );
        }),
      )}
    </svg>
  );
}

function HBarChart({ spec }: { spec: ChartSpec }) {
  const n = spec.labels.length;
  const ns = spec.series.length;
  const rowH = ns > 1 ? 26 : Math.min(34, Math.max(22, 300 / Math.max(1, n)));
  const W = 640, padL = 128, padR = 64, padT = 8, padB = 12;
  const plotW = W - padL - padR;
  const H = padT + n * rowH * ns + padB;
  const max = seriesMax(spec);
  const bh = Math.max(6, Math.min(16, rowH - 6));
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="md-chart-svg" role="img">
      {spec.labels.map((lb, i) => (
        <text
          key={i}
          x={padL - 8}
          y={padT + i * rowH * ns + (rowH * ns) / 2 + 3.5}
          textAnchor="end"
          fontSize={10.5}
          fill={C_DIM}
        >
          {trunc(lb, 12)}
        </text>
      ))}
      {spec.series.map((s, si) =>
        s.data.map((v, i) => {
          const w = Math.max(v > 0 ? 2 : 0, (Math.max(0, v) / max) * plotW);
          const y = padT + i * rowH * ns + si * rowH + (rowH - bh) / 2;
          return (
            <g key={`${si}-${i}`}>
              <rect x={padL} y={y} width={w} height={bh} rx={2} fill={PALETTE[si % PALETTE.length]} />
              {v > 0 && (
                <text x={padL + w + 6} y={y + bh / 2 + 3.5} fontSize={9.5} fill={C_DIM}>{fmtNum(v)}</text>
              )}
            </g>
          );
        }),
      )}
    </svg>
  );
}

function LineChart({ spec, area }: { spec: ChartSpec; area?: boolean }) {
  const W = 640, padL = 50, padR = 14, padT = 16, plotH = 230, padB = 44;
  const w = W - padR;
  const n = spec.labels.length;
  const max = seriesMax(spec);
  const y0 = padT + plotH;
  const xOf = (i: number) => (n <= 1 ? padL + (w - padL) / 2 : padL + ((w - padL) * i) / (n - 1));
  const yOf = (v: number) => y0 - (Math.max(0, v) / max) * plotH;
  const step = n > 24 ? 3 : n > 12 ? 2 : 1;
  const showDots = n <= 40;
  return (
    <svg viewBox={`0 0 ${W} ${padT + plotH + padB}`} className="md-chart-svg" role="img">
      <YGrid max={max} plotH={plotH} y0={y0} padL={padL} w={w} />
      {spec.labels.map((lb, i) =>
        i % step !== 0 ? null : (
          <text
            key={i}
            x={xOf(i)}
            y={y0 + 16}
            textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"}
            fontSize={10.5}
            fill={C_DIM}
          >
            {trunc(lb, 10)}
          </text>
        ),
      )}
      {spec.series.map((s, si) => {
        const color = PALETTE[si % PALETTE.length];
        const pts = s.data.map((v, i) => `${xOf(i)},${yOf(v)}`).join(" ");
        const last = s.data.length - 1;
        return (
          <g key={si}>
            {area && n > 1 && (
              <polygon points={`${xOf(0)},${y0} ${pts} ${xOf(last)},${y0}`} fill={color} fillOpacity={0.13} />
            )}
            <polyline points={pts} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
            {showDots &&
              s.data.map((v, i) => <circle key={i} cx={xOf(i)} cy={yOf(v)} r={2.6} fill={color} stroke="#161b22" strokeWidth={1} />)}
          </g>
        );
      })}
    </svg>
  );
}

function PieChart({ spec }: { spec: ChartSpec }) {
  const s = spec.series[0];
  const vals = s.data.map((v) => Math.max(0, v));
  const total = vals.reduce((a, b) => a + b, 0);
  if (total <= 0) return <div className="md-block-error">⚠ 饼图数据无效（总和为 0）</div>;
  const cx = 130, cy = 130, R = 92, IR = 58;
  let acc = 0;
  const slices = vals.map((v, i) => {
    const frac = v / total;
    const a0 = acc * Math.PI * 2 - Math.PI / 2;
    acc += frac;
    const a1 = acc * Math.PI * 2 - Math.PI / 2;
    const large = a1 - a0 > Math.PI ? 1 : 0;
    const p = (r: number, a: number) => `${cx + r * Math.cos(a)},${cy + r * Math.sin(a)}`;
    const d = `M ${p(R, a0)} A ${R} ${R} 0 ${large} 1 ${p(R, a1)} L ${p(IR, a1)} A ${IR} ${IR} 0 ${large} 0 ${p(IR, a0)} Z`;
    const mid = (a0 + a1) / 2;
    const lr = (R + IR) / 2;
    return {
      d,
      pct: frac * 100,
      value: v,
      color: PALETTE[i % PALETTE.length],
      label: spec.labels[i] ?? String(i + 1),
      lx: cx + lr * Math.cos(mid),
      ly: cy + lr * Math.sin(mid),
      showPct: frac >= 0.05,
    };
  });
  return (
    <div className="md-chart-flex">
      <svg viewBox="0 0 260 260" className="md-chart-svg md-chart-pie" role="img">
        {slices.map((sl, i) => (
          <g key={i}>
            <path d={sl.d} fill={sl.color} stroke="#161b22" strokeWidth={1.5} />
            {sl.showPct && (
              <text x={sl.lx} y={sl.ly + 3.5} textAnchor="middle" fontSize={10.5} fill={C_MAIN} fontWeight={600}>
                {Math.round(sl.pct)}%
              </text>
            )}
          </g>
        ))}
        <text x={cx} y={cy - 2} textAnchor="middle" fontSize={13} fill={C_MAIN} fontWeight={700}>{fmtNum(total)}</text>
        <text x={cx} y={cy + 14} textAnchor="middle" fontSize={9.5} fill={C_DIM}>总计</text>
      </svg>
      <div className="md-chart-legend-list">
        {slices.map((sl, i) => (
          <div key={i} className="md-chart-legend-row">
            <span className="md-chart-dot" style={{ background: sl.color }} />
            <span className="md-chart-legend-name">{trunc(sl.label, 14)}</span>
            <span className="md-chart-legend-val">{fmtNum(sl.value)}</span>
            <span className="md-chart-legend-pct">{sl.pct.toFixed(1)}%</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function RadarChart({ spec }: { spec: ChartSpec }) {
  const n = spec.labels.length;
  if (n < 3) return <div className="md-block-error">⚠ 雷达图至少需要 3 个维度</div>;
  const size = 300, cx = size / 2, cy = size / 2, R = 105;
  const max = seriesMax(spec);
  const ang = (i: number) => (i / n) * Math.PI * 2 - Math.PI / 2;
  const pt = (i: number, r: number) => `${cx + r * Math.cos(ang(i))},${cy + r * Math.sin(ang(i))}`;
  const ring = (f: number) => Array.from({ length: n }, (_, i) => pt(i, R * f)).join(" ");
  return (
    <svg viewBox={`0 0 ${size} ${size}`} className="md-chart-svg md-chart-radar" role="img">
      {[0.25, 0.5, 0.75, 1].map((f, i) => (
        <polygon key={i} points={ring(f)} fill="none" stroke={C_GRID} strokeWidth={1} />
      ))}
      {spec.labels.map((lb, i) => {
        const a = ang(i);
        const lx = cx + (R + 22) * Math.cos(a);
        const ly = cy + (R + 22) * Math.sin(a);
        const anchor = Math.abs(Math.cos(a)) < 0.3 ? "middle" : Math.cos(a) > 0 ? "start" : "end";
        return (
          <g key={i}>
            <line x1={cx} y1={cy} x2={cx + R * Math.cos(a)} y2={cy + R * Math.sin(a)} stroke={C_GRID} strokeWidth={1} />
            <text x={lx} y={ly + 3.5} textAnchor={anchor} fontSize={10.5} fill={C_DIM}>{trunc(lb, 8)}</text>
          </g>
        );
      })}
      {spec.series.map((s, si) => {
        const color = PALETTE[si % PALETTE.length];
        return (
          <g key={si}>
            <polygon
              points={s.data.map((v, i) => pt(i, (Math.max(0, v) / max) * R)).join(" ")}
              fill={color}
              fillOpacity={0.15}
              stroke={color}
              strokeWidth={2}
            />
            {s.data.map((v, i) => {
              const r = (Math.max(0, v) / max) * R;
              return <circle key={i} cx={cx + r * Math.cos(ang(i))} cy={cy + r * Math.sin(ang(i))} r={2.6} fill={color} />;
            })}
          </g>
        );
      })}
    </svg>
  );
}

export function ChartBlock({ spec }: { spec: ChartSpec }) {
  const multi = spec.series.length > 1 && spec.type !== "pie";
  return (
    <div className="md-chart">
      {(spec.title || spec.unit) && (
        <div className="md-chart-head">
          {spec.title && <span className="md-chart-title">{spec.title}</span>}
          {spec.unit && <span className="md-chart-unit">单位：{spec.unit}</span>}
        </div>
      )}
      {spec.type === "hbar" ? <HBarChart spec={spec} />
        : spec.type === "line" ? <LineChart spec={spec} />
        : spec.type === "area" ? <LineChart spec={spec} area />
        : spec.type === "pie" ? <PieChart spec={spec} />
        : spec.type === "radar" ? <RadarChart spec={spec} />
        : <BarChart spec={spec} />}
      {multi && (
        <div className="md-chart-legend">
          {spec.series.map((s, i) => (
            <span key={i} className="md-chart-legend-item">
              <span className="md-chart-dot" style={{ background: PALETTE[i % PALETTE.length] }} />
              {s.name ?? `系列 ${i + 1}`}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- 实体卡片

export function CardsBlock({ spec }: { spec: CardsSpec }) {
  return (
    <div className="md-cards">
      {spec.title && <div className="md-cards-title">{spec.title}</div>}
      <div className="md-cards-grid">
        {spec.items.map((it, i) => {
          const url = it.url ?? null;
          return (
            <div className="md-card" key={i}>
              {it.image && (
                <div className="md-card-img">
                  <img src={toFileUrl(it.image)} alt={it.title ?? ""} loading="lazy" />
                </div>
              )}
              <div className="md-card-body">
                <div className="md-card-title">
                  {url ? (
                    <a href={url} onClick={(e) => { e.preventDefault(); window.open(url, "_blank"); }}>
                      {it.title ?? "链接"}
                    </a>
                  ) : (
                    it.title
                  )}
                </div>
                {it.price && <div className="md-card-price">{it.price}</div>}
                {it.desc && <div className="md-card-desc">{it.desc}</div>}
                {!!it.tags?.length && (
                  <div className="md-card-tags">
                    {it.tags.map((t, j) => (
                      <span key={j} className="md-card-tag">{t}</span>
                    ))}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 指标卡

export function KpiBlock({ spec }: { spec: KpiSpec }) {
  return (
    <div className="md-rich">
      {spec.title && <div className="md-rich-title">{spec.title}</div>}
      <div className="md-kpi-grid">
        {spec.items.map((it, i) => (
          <div className="md-kpi" key={i}>
            <div className="md-kpi-label">{it.label}</div>
            <div className="md-kpi-value">{it.value}</div>
            {(it.delta || it.note) && (
              <div className="md-kpi-foot">
                {it.delta && (
                  <span className={`md-kpi-delta ${it.trend ?? "flat"}`}>
                    {it.trend === "up" ? "▲ " : it.trend === "down" ? "▼ " : ""}{it.delta}
                  </span>
                )}
                {it.note && <span className="md-kpi-note">{it.note}</span>}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 时间线

export function TimelineBlock({ spec }: { spec: TimelineSpec }) {
  return (
    <div className="md-rich">
      {spec.title && <div className="md-rich-title">{spec.title}</div>}
      <div className="md-tl">
        {spec.items.map((it, i) => {
          const st = it.status ?? "pending";
          return (
            <div className={`md-tl-item ${st}`} key={i}>
              <span className={`md-tl-dot ${st}`} />
              <div className="md-tl-body">
                <div className="md-tl-head">
                  <span className="md-tl-title">{it.title}</span>
                  {it.time && <span className="md-tl-time">{it.time}</span>}
                </div>
                {it.desc && <div className="md-tl-desc">{it.desc}</div>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 数据表

/** 数值判定：纯数字 / 带千分位 / 货币 / 百分比（含正负号）→ 右对齐等宽。 */
function looksNumeric(s: string): boolean {
  return /^[-+]?[¥$€]?\s?\d[\d,.\s]*%?$/.test(s.trim()) && /\d/.test(s);
}

export function TableBlock({ spec }: { spec: TableSpec }) {
  const numeric = new Set(
    spec.numeric && spec.numeric.length
      ? spec.numeric
      // 未声明时按列内容自动判定（整列都是数值才算）
      : spec.columns.map((_, ci) => ci).filter((ci) =>
          spec.rows.every((r) => !r[ci] || looksNumeric(r[ci])) &&
          spec.rows.some((r) => r[ci] && looksNumeric(r[ci]))),
  );
  return (
    <div className="md-rich">
      {spec.title && <div className="md-rich-title">{spec.title}</div>}
      <div className="md-table-wrap">
        <table className="md-table">
          <thead>
            <tr>
              {spec.columns.map((c, i) => (
                <th key={i} className={numeric.has(i) ? "num" : ""}>{c}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {spec.rows.map((r, ri) => (
              <tr key={ri}>
                {spec.columns.map((_, ci) => (
                  <td key={ci} className={numeric.has(ci) ? "num" : ""}>{r[ci] ?? ""}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 左右对比

export function CompareBlock({ spec }: { spec: CompareSpec }) {
  const side = (s: CompareSide, fallback: "bad" | "good") => (
    <div className={`md-cmp-side ${s.tone ?? fallback}`}>
      <div className="md-cmp-head">{s.title}</div>
      <ul className="md-cmp-list">
        {s.items.map((t, i) => <li key={i}>{t}</li>)}
      </ul>
    </div>
  );
  return (
    <div className="md-rich">
      {spec.title && <div className="md-rich-title">{spec.title}</div>}
      <div className="md-cmp">
        {side(spec.left, "bad")}
        <span className="md-cmp-vs">⇄</span>
        {side(spec.right, "good")}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 看板

export function BoardBlock({ spec }: { spec: BoardSpec }) {
  return (
    <div className="md-rich">
      {spec.title && <div className="md-rich-title">{spec.title}</div>}
      <div className="md-board">
        {spec.columns.map((col, ci) => (
          <div className="md-board-col" key={ci}>
            <div className="md-board-col-head">
              {col.name}
              <span className="md-board-count">{col.cards.length}</span>
            </div>
            {col.cards.map((k, ki) => (
              <div className={`md-board-card ${k.tone ?? "neutral"}`} key={ki}>
                <div className="md-board-card-title">{k.title}</div>
                {k.desc && <div className="md-board-card-desc">{k.desc}</div>}
                {k.tag && <span className="md-board-tag">{k.tag}</span>}
              </div>
            ))}
            {col.cards.length === 0 && <div className="md-board-empty">（空）</div>}
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 进度条

/** 进度色阶：<40 红（短板）/ 40-79 黄（进行中）/ ≥80 绿（健康）。 */
function progressTone(v: number): "bad" | "warn" | "good" {
  return v < 40 ? "bad" : v < 80 ? "warn" : "good";
}

// ---------------------------------------------------------------- 交互式图解

/** 交互式图解：分块标签（可点击）+ 当前分块说明。
 *  对齐 GPT Intelligent UI 的「点不同模块看对应原理」；纯 state 切换，
 *  不执行任何脚本（安全模型与 render_card 沙箱一致）。 */
export function InteractiveBlock({ spec }: { spec: InteractiveSpec }) {
  const [active, setActive] = useState(0);
  const parts = spec.parts;
  const cur = parts[Math.min(active, parts.length - 1)];
  return (
    <div className="md-rich">
      {spec.title && <div className="md-rich-title">{spec.title}</div>}
      {spec.summary && <div className="md-ix-summary">{spec.summary}</div>}
      <div className="md-ix-parts" role="tablist" aria-label="图解分块">
        {parts.map((p, i) => (
          <button
            key={i}
            type="button"
            role="tab"
            aria-selected={i === active}
            className={`md-ix-part ${i === active ? "active" : ""}`}
            onClick={() => setActive(i)}
            title={p.meta ? `${p.name} · ${p.meta}` : p.name}
          >
            {spec.numbered && <span className="md-ix-no">{i + 1}</span>}
            <span className="md-ix-name">{p.name}</span>
            {p.meta && <span className="md-ix-meta">{p.meta}</span>}
          </button>
        ))}
      </div>
      <div className="md-ix-detail" role="tabpanel">
        <div className="md-ix-detail-head">
          {spec.numbered && <span className="md-ix-no">{Math.min(active, parts.length - 1) + 1}</span>}
          <span className="md-ix-detail-name">{cur.name}</span>
          {cur.meta && <span className="md-ix-meta">{cur.meta}</span>}
        </div>
        <div className="md-ix-desc">{cur.desc}</div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 进度条

export function ProgressBlock({ spec }: { spec: ProgressSpec }) {
  return (
    <div className="md-rich">
      {spec.title && <div className="md-rich-title">{spec.title}</div>}
      <div className="md-progress-list">
        {spec.items.map((it, i) => {
          const tone = progressTone(it.value);
          return (
            <div className="md-progress" key={i}>
              <div className="md-progress-head">
                <span className="md-progress-label" title={it.label}>{it.label}</span>
                <span className={`md-progress-value ${tone}`}>
                  {it.display ?? `${Math.round(it.value)}%`}
                </span>
              </div>
              <div className="md-progress-track">
                <i className={`md-progress-fill ${tone}`} style={{ width: `${it.value}%` }} />
              </div>
              {it.note && <div className="md-progress-note">{it.note}</div>}
            </div>
          );
        })}
      </div>
    </div>
  );
}
