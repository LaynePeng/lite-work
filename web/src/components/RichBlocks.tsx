// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 原生富内容块（intelligent UI / generative UI 思路）：
// Agent 在回复里输出「数据 + 组件类型」围栏（```chart / ```cards），
// 前端用应用设计系统原生渲染——不生成 HTML 文档/图片文件，随文字流式
// 出现，样式与应用一致。同时提供 rewriteLocalUrls / toFileUrl：本地
// 相对路径统一 rewrite 到 /api/files/raw（复用后端越界检查与鉴权），
// 修复 iframe / 卡片内相对路径断链。

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

export type RichSegment =
  | { kind: "chart"; spec: ChartSpec }
  | { kind: "cards"; spec: CardsSpec };

function toNums(v: unknown): number[] {
  if (!Array.isArray(v)) return [];
  return v.map((x) => {
    const n = typeof x === "number" ? x : Number(x);
    return Number.isFinite(n) ? n : 0;
  });
}

/** 解析 ```chart / ```cards 围栏内容；不可渲染（JSON 无效 / 形状不对）返回 null，
 *  调用方保留为普通代码块。规格宽松：单系列可省略 series 直接 data。 */
export function tryParseRichBlock(lang: string, raw: string): RichSegment | null {
  const l = lang.toLowerCase();
  if (l !== "chart" && l !== "cards") return null;
  let o: unknown;
  try {
    o = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!o || typeof o !== "object" || Array.isArray(o)) return null;
  const rec = o as Record<string, unknown>;

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
