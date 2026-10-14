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

import { useEffect, useMemo, useRef, useState } from "react";

import {
  blobToBase64, buildCsv, buildExportHtml, downloadBlob, escapeHtml,
  exportBaseName, safeFileName, svgToPngBlob, textToBlob, type ExportTable,
} from "../lib/richExport";

// ---------------------------------------------------------------- 导出按钮

/** 中间产物目录（与后端 _ensure_output_dir 的「中间产物/<subdir>/」约定一致）。 */
const DERIVED_DIR = "中间产物/exports";

interface ExportItem {
  /** 菜单标签 */
  label: string;
  /** 文件后缀（用于补全「另存为」默认名） */
  ext: string;
  /** 生成待写入内容（异步，PNG 需先栅格化） */
  make: () => Promise<{ blob: Blob; name: string }>;
  /** 下载对话框的类型过滤器 */
  filter: { name: string; extensions: string[] };
}

/** 富块导出菜单。两种去向：
 *  - 「下载」：走原生「另存为」（Electron 桥）或浏览器下载（降级）；
 *  - 「导出到项目」：写进 `中间产物/exports/`——这条路径是**给 Agent 用的**，
 *    导出的图可直接被 docx_create / pptx_create 的 Markdown 图片语法引用。
 *
 *  宿主块在**点击时**通过父节点解析（不是渲染期传 ref：首渲染 ref 尚为 null，
 *  会把 null 闭包进 make()）；按钮平时隐藏、悬停富块才出现，不干扰阅读。 */
function ExportMenu({ build }: { build: (host: HTMLElement) => ExportItem[] }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const boxRef = useRef<HTMLDivElement>(null);

  // 点击外部收起菜单（含键盘 Esc）
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!boxRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const flash = (msg: string) => {
    setNote(msg);
    window.setTimeout(() => setNote(""), 2600);
  };

  /** 把 blob 交给用户保存（桌面走原生对话框写入；浏览器直接下载）。 */
  const saveBlob = async (blob: Blob, filename: string): Promise<string> => {
    const bridge = window.liteWork;
    if (bridge?.saveFile) {
      const base64 = await blobToBase64(blob);
      const r = await bridge.saveFile({ defaultName: filename, base64, title: "导出文件", filters: [] });
      if (!r.ok) return r.error === "cancelled" ? "" : `导出失败：${r.error ?? "未知错误"}`;
      return "已导出";
    }
    downloadBlob(blob, filename); // Web 版（浏览器）降级
    return "已导出";
  };

  /** 宿主富块根节点 = 菜单的父元素（菜单固定作为块的首个子元素渲染）。 */
  const hostOf = (): HTMLElement | null => boxRef.current?.parentElement ?? null;

  const run = async (pick: (items: ExportItem[], host: HTMLElement) => ExportItem, toProject: boolean) => {
    setOpen(false);
    const host = hostOf();
    if (!host) return;
    const it = pick(build(host), host);
    if (!it) return;
    const bridge = window.liteWork;
    if (toProject && !bridge?.writeWorkspaceFile) { flash("导出到项目仅桌面应用可用"); return; }
    setBusy(true);
    try {
      const { blob, name } = await it.make();
      if (!toProject) {
        const msg = await saveBlob(blob, name);
        if (msg) flash(msg);
        return;
      }
      const base64 = await blobToBase64(blob);
      const rel = `${DERIVED_DIR}/${safeFileName(name, `export.${it.ext}`)}`;
      const r = await bridge!.writeWorkspaceFile!(rel, base64);
      if (!r.ok) { flash(`导出失败：${r.error ?? "未知错误"}`); return; }
      flash(`已导出到 ${rel}`);
      // 说明：把引用语法复制到剪贴板，粘贴进 docx_create 的 Markdown 图片语法即可用
      try { await navigator.clipboard.writeText(`![图表](${rel})`); } catch { /* 无剪贴板权限：忽略 */ }
    } catch (err) {
      flash(`导出失败：${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="md-export" ref={boxRef}>
      <button
        type="button"
        className="md-export-btn"
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={busy}
        title="导出"
        onClick={(e) => { e.stopPropagation(); setOpen((v) => !v); }}
      >
        {busy ? "…" : note || "⇩"}
      </button>
      {open && (
        <div className="md-export-menu" role="menu">
          <div className="md-export-group">下载</div>
          {build(hostOf() ?? document.body).map((it) => (
            <button key={it.label} type="button" role="menuitem"
              onClick={() => void run((items) => items.find((x) => x.label === it.label)!, false)}>
              {it.label}
            </button>
          ))}
          <div className="md-export-group">导出到项目（供 Agent 引用）</div>
          {build(hostOf() ?? document.body).map((it) => (
            <button key={`ws-${it.label}`} type="button" role="menuitem"
              onClick={() => void run((items) => items.find((x) => x.label === it.label)!, true)}>
              {it.label} → 中间产物
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** 图表可导出的三种格式（PNG / SVG / 自足 HTML）。`wrap` 用于就地取 SVG。 */
function chartExportItems(spec: ChartSpec, wrap: HTMLElement | null): ExportItem[] {
  const base = exportBaseName(spec.title, "chart");
  const table: ExportTable | undefined =
    spec.labels.length && spec.series.some((s) => s.data.length)
      ? {
          columns: ["", ...spec.series.map((s, i) => s.name ?? `系列 ${i + 1}`)],
          rows: spec.labels.map((lb, i) => [lb, ...spec.series.map((s) => String(s.data[i] ?? ""))]),
          numeric: spec.series.map((_, i) => i + 1),
        }
      : undefined;
  const html = (): Blob =>
    textToBlob(buildExportHtml({ title: spec.title, wrap: wrap!, table, filename: base }), "text/html;charset=utf-8");
  return [
    {
      label: "PNG 图片", ext: "png", filter: { name: "PNG 图片", extensions: ["png"] },
      make: async () => ({ blob: await svgToPngBlob(wrap!), name: `${base}.png` }),
    },
    {
      label: "独立 HTML（可交互）", ext: "html", filter: { name: "网页", extensions: ["html"] },
      make: async () => ({ blob: html(), name: `${base}.html` }),
    },
  ];
}

/** 表格可导出的两种格式（CSV / TSV）。 */
function tableExportItems(spec: TableSpec): ExportItem[] {
  const base = exportBaseName(spec.title, "table");
  const data: ExportTable = { columns: spec.columns, rows: spec.rows };
  return [
    {
      label: "CSV（Excel 可直接打开）", ext: "csv", filter: { name: "CSV", extensions: ["csv"] },
      make: async () => ({ blob: textToBlob(buildCsv(data), "text/csv;charset=utf-8"), name: `${base}.csv` }),
    },
    {
      label: "HTML 表格", ext: "html", filter: { name: "网页", extensions: ["html"] },
      make: async () => ({
        blob: textToBlob(
          buildTableHtml(spec),
          "text/html;charset=utf-8",
        ),
        name: `${base}.html`,
      }),
    },
  ];
}

/** 表格 → 自足 HTML（无 SVG，故不走 buildExportHtml）。 */
function buildTableHtml(spec: TableSpec): string {
  const title = escapeHtml(spec.title || "数据表");
  const head = spec.columns.map((c) => `<th>${escapeHtml(c)}</th>`).join("");
  const body = spec.rows
    .map((r) => `<tr>${spec.columns.map((_, ci) => {
      const num = spec.numeric?.includes(ci) ?? false;
      return `<td${num ? ' class="num"' : ""}>${escapeHtml(r[ci] ?? "")}</td>`;
    }).join("")}</tr>`)
    .join("\n");
  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${title}</title>
<style>
* { box-sizing: border-box; }
body {
  margin: 0; padding: 30px 26px 46px; background: #0d1117; color: #e6edf3;
  font-family: -apple-system, "PingFang SC", "Segoe UI", "Microsoft YaHei", sans-serif;
  font-size: 14px; line-height: 1.6; -webkit-font-smoothing: antialiased;
}
h1 { font-size: 17px; font-weight: 600; margin: 0 0 14px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th {
  text-align: left; font-size: 12px; font-weight: 500; color: #6e7a8c;
  padding: 9px 12px; border-bottom: 1px solid #262e3f; white-space: nowrap;
}
td { padding: 8px 12px; color: #a8b3c0; border-bottom: 1px solid #1c2330; }
tr:nth-child(even) td { background: rgba(230, 237, 243, .03); }
tr:last-child td { border-bottom: 0; }
td.num {
  text-align: right; color: #e6edf3; font-weight: 600;
  font-variant-numeric: tabular-nums; white-space: nowrap;
}
.btn {
  font: inherit; font-size: 12.5px; padding: 6px 14px; cursor: pointer;
  border: 1px solid #262e3f; border-radius: 8px; background: #1c2330; color: #a8b3c0;
  margin-bottom: 14px;
}
.btn:hover { background: #21283a; color: #e6edf3; }
@media print {
  body { background: #fff; color: #000; padding: 0; }
  th { color: #555; } td { color: #222; border-color: #ddd; }
  tr:nth-child(even) td { background: #f7f7f7; }
  .btn { display: none; }
}
</style>
</head>
<body>
<h1>${title}</h1>
<button class="btn" type="button" onclick="window.print()">打印 / 存 PDF</button>
<table>
<thead><tr>${head}</tr></thead>
<tbody>
${body}
</tbody>
</table>
</body>
</html>`;
}

// ---------------------------------------------------------------- 本地资源 URL

/** URL 形态的相对路径 → 原始文件路径。
 *
 *  卡片/markdown/HTML 里的 src 语义上都是 **URL**：`%20` 代表空格，中文等
 *  非 ASCII 字符也应写作 %XX。但 Agent 给出的通常是**未编码的原始路径**，
 *  而 react-markdown（mdast-util-to-hast 的 normalizeUri）在交给 img 组件前
 *  已把非 ASCII 与空格编码过一遍——所以这里先解码回原始路径，再由调用方
 *  统一编码一次，避免「中文/空格路径被二次编码」。
 *
 *  解码失败（含裸 `%` 等非法转义，如 `折扣100%.png`）原样返回，不改变既有行为。 */
function decodeUrlPath(p: string): string {
  if (!p.includes("%")) return p;
  try {
    return decodeURIComponent(p);
  } catch {
    return p;
  }
}

/** 本地相对路径 → /api/files/raw（http(s)/data//api/ 原样）。
 *
 *  注意输入可能是**已被 URI 归一化**的 URL（markdown 图片地址必经过这一步）：
 *  若直接 encodeURIComponent，`中间产物/img/a.jpg` 会变成
 *  `%25E4%25B8%25AD…`，后端解码后拿到字面量 `%E4%B8%AD…` → 404 断图。
 *  故统一走 decodeUrlPath 归一为「原始路径 → 编码一次」。 */
export function toFileUrl(p: string): string {
  const raw = (p ?? "").trim();
  if (!raw || /^(https?:|data:|\/api\/)/i.test(raw)) return raw;
  return `/api/files/raw?path=${encodeURIComponent(decodeUrlPath(raw))}`;
}

/** HTML 串内 src/href（双/单引号）与 CSS url() 的相对路径 rewrite。
 *  lookbehind 防误伤 data-src 等复合属性名；http(s)/data/#/mailto//api/ 原样。 */
export function rewriteLocalUrls(html: string): string {
  let out = html.replace(
    /(?<![\w-])(src|href)\s*=\s*(?:"([^"]*)"|'([^']*)')/gi,
    (full: string, attr: string, dq?: string, sq?: string) => {
      const val = dq !== undefined ? dq : sq;
      if (val === undefined || val === "" || /^(https?:|data:|#|mailto:|\/api\/)/i.test(val)) return full;
      return `${attr}="/api/files/raw?path=${encodeURIComponent(decodeUrlPath(val))}"`;
    },
  );
  out = out.replace(/url\(\s*(['"]?)([^'")]+)\1\s*\)/gi, (full: string, _q: string, val: string) => {
    if (!val || /^(https?:|data:|\/api\/|#)/i.test(val)) return full;
    return `url("/api/files/raw?path=${encodeURIComponent(decodeUrlPath(val))}")`;
  });
  return out;
}

// ---------------------------------------------------------------- 邮件草稿卡（气泡内编辑）

/** ```email-draft 渲染：对话气泡里的可编辑邮件草稿。
 *  与审批卡编辑器的分工：这里是「内容创作」现场（可反复改），
 *  审批卡只是最终「允许发送」开关。确认后把 edits 交给 onConfirm，
 *  由 Agent 去 dry_run 正式发送（同一轮对话里完成，不发额外请求）。 */
export function EmailDraftBlock({ spec, onConfirm }: {
  spec: EmailDraftSpec;
  onConfirm?: (edits: { id: string; to: string; cc?: string; bcc?: string; subject: string; body: string; html: string }) => void;
}) {
  const [fields, setFields] = useState({
    to: spec.to, cc: spec.cc ?? "", bcc: spec.bcc ?? "", subject: spec.subject,
  });
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const [sent, setSent] = useState(false);

  const fieldRow = (label: string, key: "to" | "cc" | "bcc" | "subject", placeholder: string) => (
    <tr>
      <th style={{ padding: "6px 10px", background: "var(--bg-1)", border: "1px solid var(--border)",
                   textAlign: "left", fontWeight: 600, whiteSpace: "nowrap", width: 64, verticalAlign: "top" }}>{label}</th>
      <td style={{ padding: "6px 10px", border: "1px solid var(--border)", verticalAlign: "top" }}>
        <input
          className="form-input"
          style={{ width: "100%", fontSize: 13, padding: "4px 8px" }}
          value={fields[key]}
          placeholder={placeholder}
          disabled={sent}
          onChange={(e) => setFields((f) => ({ ...f, [key]: e.target.value }))}
        />
      </td>
    </tr>
  );

  const initialHtml = useMemo(() => {
    const html = (spec.html ?? "").trim();
    if (html) return html;
    const esc = (t: string) => t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    return esc(spec.body).replace(/\n/g, "<br>");
  }, [spec]);

  useEffect(() => {
    if (canvasRef.current) canvasRef.current.innerHTML = initialHtml;
  }, [initialHtml]);

  const confirm = () => {
    if (sent || !onConfirm) return;
    const inner = canvasRef.current?.innerHTML ?? "";
    const plain = canvasRef.current?.innerText ?? spec.body;
    setSent(true);
    onConfirm({
      id: spec.id,
      to: fields.to, cc: fields.cc, bcc: fields.bcc, subject: fields.subject,
      body: plain.trim(), html: inner,
    });
  };

  return (
    <div style={{ maxWidth: 720, margin: "10px 0", border: "1px solid var(--border)",
                  borderRadius: 10, overflow: "hidden", background: "var(--bg-0)" }}>
      <div style={{ padding: "10px 14px", background: "var(--accent, #4f8cff)", color: "#fff", fontWeight: 600,
                    fontSize: 13, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span>📧 邮件草稿 · {spec.id}</span>
        <span style={{ fontSize: 11, opacity: 0.9, background: "rgba(255,255,255,0.2)",
                       borderRadius: 8, padding: "1px 8px" }}>
          {sent ? "✓ 已确认发送" : "未发送 · 可编辑"}
        </span>
      </div>
      <div style={{ padding: "12px 14px" }}>
        <table style={{ borderCollapse: "collapse", width: "100%" }}>
          <tbody>
            {fieldRow("收件人", "to", "收件邮箱，多个逗号分隔")}
            {fields.cc !== "" || spec.cc ? fieldRow("抄送", "cc", "可选") : null}
            {fields.bcc !== "" || spec.bcc ? fieldRow("密送", "bcc", "可选") : null}
            {fieldRow("主题", "subject", "邮件主题")}
          </tbody>
        </table>
        <div style={{ margin: "12px 0 4px", fontWeight: 600, fontSize: 13 }}>正文
          <span style={{ fontWeight: 400, color: "var(--text-2)", fontSize: 11 }}>（直接点进去编辑）</span>
        </div>
        <div
          ref={canvasRef}
          contentEditable={!sent}
          suppressContentEditableWarning
          spellCheck={false}
          style={{ border: "1px dashed var(--border)", borderRadius: 6, padding: 12,
                   background: "#fff", color: "#222", minHeight: 80, fontSize: 14,
                   lineHeight: 1.7, outline: "none", maxHeight: 360, overflow: "auto" }}
        />
        {spec.attachments && spec.attachments.length > 0 && (
          <div style={{ marginTop: 8, fontSize: 12, color: "var(--text-2)" }}>
            附件：{spec.attachments.join(", ")}
          </div>
        )}
        <div style={{ marginTop: 12, display: "flex", gap: 10, alignItems: "center" }}>
          <button
            className="btn-test"
            disabled={sent}
            onClick={confirm}
          >
            {sent ? "✓ 已确认" : "确认发送这封"}
          </button>
          <span style={{ fontSize: 11.5, color: "var(--text-2)" }}>
            {sent ? "改动已回传给 Agent，等待正式发送结果" : "改完点此确认；不改动直接点也按原稿发送"}
          </span>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 规格与解析

export interface ChartSeries {
  name?: string;
  data: number[];
  /** combo（双轴组合图）：该系列走右轴。 */
  axis?: "left" | "right";
}

/** 散点图数据点。`y` 是 x 的函数，故 y 可以为负。 */
export interface ScatterPoint {
  x: number;
  y: number;
}

export interface BoxStat {
  label: string;
  min: number;
  q1: number;
  median: number;
  q3: number;
  max: number;
  /** 离群值（绘制为单独的点）。 */
  outliers?: number[];
}

/** 瀑布图条目：`total` 为小计柱（从 0 起，不断层），否则为增量柱。 */
export interface WaterfallItem {
  label: string;
  value: number;
  total?: boolean;
}

export interface HeatmapSpec {
  /** 矩阵值（行数 × 列数）。 */
  values: number[][];
  xLabels?: string[];
  yLabels?: string[];
  /** 色阶：`sequential` 单色深浅（默认，适合绝对值）；`diverging` 红-灰-绿（适合相关性 -1..1）。 */
  mode?: "sequential" | "diverging";
  min?: number;
  max?: number;
}

export type ChartType =
  | "bar" | "hbar" | "line" | "area" | "pie" | "radar"
  // 专业统计图
  | "scatter" | "boxplot" | "histogram" | "heatmap" | "combo" | "waterfall" | "stacked" | "stacked-area";

export interface ChartSpec {
  type: ChartType;
  title?: string;
  unit?: string;
  labels: string[];
  series: ChartSeries[];
  /** scatter：每系列一组点（与 series 平行，index 对齐）。 */
  points?: ScatterPoint[][];
  /** scatter：是否绘制最小二乘趋势线（默认 true；点数 < 3 时自动跳过）。 */
  trend?: boolean;
  /** boxplot：每系列一组五数概括。 */
  boxStats?: BoxStat[][];
  /** heatmap：矩阵数据（独立于 labels/series）。 */
  heatmap?: HeatmapSpec;
  /** waterfall：增量/小计条目（独立于 labels/series）。 */
  items?: WaterfallItem[];
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
 *  纯前端 state 切换，无脚本执行——沙箱安全模型不变。
 *  组合能力（interactive × diagram）：part 可带可选 shapes（diagram 同款
 *  白名单原语）——选中该块时详情区上方渲染该块的聚焦示意图；无 shapes
 *  的块照旧纯文字，两种可混用。 */
export interface InteractivePart {
  /** 分块名（显示在可点击标签上），如「Frame」「车轮」 */
  name: string;
  /** 该分块的说明（支持多行） */
  desc: string;
  /** 数值/规格补充，如「700c」「11-32T」 */
  meta?: string;
  /** 该分块的聚焦示意图（可选；diagram 同款白名单原语，单块单色渲染） */
  shapes?: DiagramShape[];
}

export interface InteractiveSpec {
  title?: string;
  /** 图解总览（一句话，始终显示） */
  summary?: string;
  /** 可点击分块（首个默认选中） */
  parts: InteractivePart[];
  /** 是否显示分块序号（结构拆解类默认可读性更好） */
  numbered?: boolean;
  /** 分块聚焦视图的画布（仅当有 part 带 shapes 时使用；缺省 470×190） */
  canvas?: { w: number; h: number };
}
/** 图形图解（```diagram）：模型输出结构化图形数据（原语 + 部件分组），
 *  前端渲染成原生 SVG——对齐 GPT Intelligent UI 的图形化拆解（自行车/
 *  网络分层/发动机），任意主题现场生成，无需预制图库。
 *  原语白名单：circle / rect / line / polyline / path / ellipse / label。
 *  label 为图内文字标签（纯文本、限长限次——模型手排坐标的排版能力有限，
 *  长文仍交给下方说明区；短标签是图的「名字」，对齐 GPT 图内命名形态）。 */
export type DiagramShape =
  | { t: "circle"; x: number; y: number; r: number }
  | { t: "rect"; x: number; y: number; w: number; h: number; rx?: number }
  | { t: "line"; x1: number; y1: number; x2: number; y2: number }
  | { t: "ellipse"; x: number; y: number; rx: number; ry: number }
  | { t: "polyline"; pts: [number, number][] }
  | { t: "path"; d: string }
  | { t: "label"; x: number; y: number; text: string; size?: number; anchor?: "start" | "middle" | "end" };

export interface DiagramPart {
  /** 部件名（标签 + 说明标题） */
  name: string;
  /** 该部件的图形原语（同组同色、点击整组高亮） */
  shapes: DiagramShape[];
  /** 说明（点击后展示） */
  desc: string;
  /** 规格补充（标签与说明区显示） */
  meta?: string;
}

export interface DiagramSpec {
  title?: string;
  summary?: string;
  /** 画布宽高（viewBox；缺省 470×300） */
  canvas?: { w: number; h: number };
  parts: DiagramPart[];
}

/** 语义布局图（```layout）：模型只输出「节点 + 边 + 层级」的语义结构，
 *  前端跑分层布局算法算坐标——LLM 不手排坐标（它最不擅长的事），
 *  只负责「谁连谁、怎么分组」。布局：同 level 横排等距、层间等高、
 *  group 同色聚簇。渲染复用 diagram 的原语层（label + 低饱和填充）。 */
export interface LayoutNode {
  id: string;
  label: string;
  /** 层级（0 起，同层横排；缺省按拓扑深度推导） */
  level?: number;
  /** 分组名（同组同色聚簇；缺省独立成组） */
  group?: string;
  meta?: string;
}

export interface LayoutEdge {
  from: string;
  to: string;
  /** 连线标注（可选，短词） */
  label?: string;
}

export interface LayoutSpec {
  title?: string;
  summary?: string;
  /** 布局方向：TB（自上而下，默认）/ LR（从左到右） */
  dir?: "TB" | "LR";
  nodes: LayoutNode[];
  edges: LayoutEdge[];
}

/** 邮件草稿（```email-draft）：dry_run 输出的可编辑草稿卡。
 *  用户在气泡里直接改收件人/主题/正文，点「确认发送这封」后经
 *  onEmailDraftConfirm 回传改动，Agent 去 dry_run 正式发送。 */
export interface EmailDraftSpec {
  id: string;
  to: string;
  cc?: string;
  bcc?: string;
  subject: string;
  body: string;
  html?: string;
  attachments?: string[];
}

export type RichSegment =
  | { kind: "chart"; spec: ChartSpec }
  | { kind: "email-draft"; spec: EmailDraftSpec }
  | { kind: "cards"; spec: CardsSpec }
  | { kind: "kpi"; spec: KpiSpec }
  | { kind: "timeline"; spec: TimelineSpec }
  | { kind: "table"; spec: TableSpec }
  | { kind: "compare"; spec: CompareSpec }
  | { kind: "board"; spec: BoardSpec }
  | { kind: "progress"; spec: ProgressSpec }
  | { kind: "interactive"; spec: InteractiveSpec }
  | { kind: "diagram"; spec: DiagramSpec }
  | { kind: "layout"; spec: LayoutSpec };

/** 富块围栏语言（ChatView 分段正则与解析器共用一份，防两处漂移）。
 *  mermaid 不在此列——它是 DSL 非 JSON，ChatView 单独分段。 */
export const RICH_LANGS = [
  "chart", "cards", "kpi", "timeline", "table", "compare", "board", "progress",
  "interactive", "diagram", "layout", "email-draft",
] as const;

function toNums(v: unknown): number[] {
  if (!Array.isArray(v)) return [];
  return v.map((x) => {
    const n = typeof x === "number" ? x : Number(x);
    return Number.isFinite(n) ? n : 0;
  });
}

// ---------------------------------------------------------------- 图形原语解析（共享）

/** diagram / interactive(part.shapes) 共用的白名单原语解析 + 数值安全化。
 *  坐标越界 clamp、非法原语丢弃、path d 清洗（<>/{}/javascript:/on*=）。
 *  返回清洗后的原语数组（可能为空 → 调用方决定降级语义）。 */
function parseDiagramShapes(raw: unknown, canvas: { w: number; h: number }): DiagramShape[] {
  const num = (v: unknown): number | null => {
    const n = typeof v === "number" ? v : Number(v);
    return Number.isFinite(n) ? n : null;
  };
  const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));
  const nOr0 = (v: unknown) => num(v) ?? 0;
  const cx = canvas.w, cy = canvas.h;
  const parseShape = (s: Record<string, unknown>): DiagramShape | null => {
    const t = typeof s.t === "string" ? s.t.toLowerCase() : "";
    if (t === "circle") {
      return { t: "circle", x: clamp(nOr0(s.x), -50, cx + 50), y: clamp(nOr0(s.y), -50, cy + 50),
               r: clamp(nOr0(s.r), 0, 800) };
    }
    if (t === "rect") {
      return { t: "rect", x: clamp(nOr0(s.x), -50, cx), y: clamp(nOr0(s.y), -50, cy),
               w: clamp(nOr0(s.w), 0, cx + 100), h: clamp(nOr0(s.h), 0, cy + 100),
               ...(num(s.rx) !== null ? { rx: clamp(nOr0(s.rx), 0, 60) } : {}) };
    }
    if (t === "line") {
      return { t: "line", x1: clamp(nOr0(s.x1), -50, cx + 50), y1: clamp(nOr0(s.y1), -50, cy + 50),
               x2: clamp(nOr0(s.x2), -50, cx + 50), y2: clamp(nOr0(s.y2), -50, cy + 50) };
    }
    if (t === "ellipse") {
      return { t: "ellipse", x: clamp(nOr0(s.x), -50, cx + 50), y: clamp(nOr0(s.y), -50, cy + 50),
               rx: clamp(nOr0(s.rx), 0, 800), ry: clamp(nOr0(s.ry), 0, 800) };
    }
    if (t === "polyline" && Array.isArray(s.pts)) {
      const pts = s.pts
        .filter((p): p is unknown[] => Array.isArray(p) && p.length >= 2)
        .map((p) => [clamp(nOr0(p[0]), -50, cx + 50), clamp(nOr0(p[1]), -50, cy + 50)] as [number, number])
        .slice(0, 40);
      if (pts.length < 2) return null;
      return { t: "polyline", pts };
    }
    if (t === "path" && typeof s.d === "string") {
      const d = s.d.replace(/[<>{}]|javascript:|on\w+=/gi, "").slice(0, 600);
      if (!d.trim()) return null;
      return { t: "path", d };
    }
    if (t === "label" && typeof s.text === "string") {
      // 图内标签：纯文本、单条限长（长文交给说明区）。
      // 清洗两步：①整段剥掉类标签/事件/协议片段；②残余按白名单字符截留
      // （字母/数字/中文/常用标点空格），React text 节点本就转义，这里是
      // 内容卫生而非安全兜底。
      const sanitized = s.text
        .replace(/<\/?[a-z][^>]*>|javascript:|on\w+=/gi, "")
        .slice(0, 24)
        .replace(/[^\w\u4e00-\u9fff .·:：()（）\-—/，、?!！%+]/g, "")
        .trim();
      if (!sanitized) return null;
      const anchor = s.anchor === "start" || s.anchor === "end" ? s.anchor : "middle";
      const size = clamp(nOr0(s.size), 9, 22);
      return { t: "label", x: clamp(nOr0(s.x), -50, cx + 50), y: clamp(nOr0(s.y), -50, cy + 50), text: sanitized, size, anchor };
    }
    return null;
  };
  return (Array.isArray(raw) ? raw : [])
    .filter((s): s is Record<string, unknown> => !!s && typeof s === "object")
    .map(parseShape)
    .filter((s): s is DiagramShape => s !== null)
    .slice(0, 24);
}

/** canvas 尺寸解析（diagram / interactive 共用）：clamp 200-1200 × 150-800。 */
function parseCanvas(rec: Record<string, unknown>, defW: number, defH: number): { w: number; h: number } {
  const num = (v: unknown) => {
    const n = typeof v === "number" ? v : Number(v);
    return Number.isFinite(n) ? n : null;
  };
  const c = rec.canvas as Record<string, unknown> | undefined;
  const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));
  return {
    w: clamp(Math.round(num(c?.w) ?? defW), 200, 1200),
    h: clamp(Math.round(num(c?.h) ?? defH), 150, 800),
  };
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

  if (l === "email-draft") {
    const id = str(rec.id, 32);
    const to = str(rec.to, 400);
    const subject = str(rec.subject, 200);
    if (!id || !to || subject === undefined) return null;
    const spec: EmailDraftSpec = {
      id, to, subject: subject ?? "",
      cc: str(rec.cc, 400),
      bcc: str(rec.bcc, 400),
      body: typeof rec.body === "string" ? rec.body.slice(0, 20000) : "",
      html: typeof rec.html === "string" ? rec.html.slice(0, 60000) : undefined,
      attachments: strList(rec.attachments, 8),
    };
    return { kind: "email-draft", spec };
  }

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
    // 组合能力：part 可带 shapes（diagram 白名单原语）→ 聚焦视图 + 说明
    const iCanvas = parseCanvas(rec, 470, 190);
    const parts: InteractivePart[] = (Array.isArray(rec.parts) ? rec.parts : [])
      .filter((it): it is Record<string, unknown> => !!it && typeof it === "object")
      .map((it): InteractivePart => {
        const shapes = parseDiagramShapes(it.shapes, iCanvas);
        return {
          name: str(it.name, 40) ?? "",
          desc: str(it.desc, 1200) ?? "",
          meta: str(it.meta, 40),
          ...(shapes.length ? { shapes } : {}),
        };
      })
      .filter((it) => it.name && it.desc)
      .slice(0, 12);
    if (!parts.length) return null;
    const spec: InteractiveSpec = { parts };
    if (title) spec.title = title;
    const summary = str(rec.summary, 200);
    if (summary) spec.summary = summary;
    if (rec.numbered === true) spec.numbered = true;
    if (parts.some((p) => p.shapes?.length)) spec.canvas = iCanvas;
    return { kind: "interactive", spec };
  }

  if (l === "diagram") {
    // 原语白名单解析 + 数值安全化（有限数、画布内 clamp）。
    // 坐标越界 / 原语非法不致命：clamp 或丢弃该原语；整组无有效原语 → 部件降级。
    const canvas = parseCanvas(rec, 470, 300);
    const parts: DiagramPart[] = (Array.isArray(rec.parts) ? rec.parts : [])
      .filter((it): it is Record<string, unknown> => !!it && typeof it === "object")
      .map((it) => {
        const shapes = parseDiagramShapes(it.shapes, canvas);
        return {
          name: str(it.name, 40) ?? "",
          desc: str(it.desc, 1200) ?? "",
          meta: str(it.meta, 40),
          shapes,
        };
      })
      .filter((it) => it.name && it.desc && it.shapes.length > 0)
      .slice(0, 10);
    if (!parts.length) return null;
    const spec: DiagramSpec = { parts, canvas };
    if (title) spec.title = title;
    const summary = str(rec.summary, 200);
    if (summary) spec.summary = summary;
    return { kind: "diagram", spec };
  }

  if (l === "layout") {
    // 语义布局图：模型只给 nodes/edges（语义），坐标由前端布局算法算——
    // 从源头消灭「LLM 手排坐标画歪」的问题。
    const nodes: LayoutNode[] = (Array.isArray(rec.nodes) ? rec.nodes : [])
      .filter((it): it is Record<string, unknown> => !!it && typeof it === "object")
      .map((it) => {
        const id = str(it.id, 40);
        const label = str(it.label, 24) ?? id ?? "";
        const lv = typeof it.level === "number" && Number.isFinite(it.level)
          ? Math.max(0, Math.min(12, Math.floor(it.level))) : undefined;
        return {
          id: id ?? label,
          label,
          ...(lv !== undefined ? { level: lv } : {}),
          ...(str(it.group, 40) ? { group: str(it.group, 40) } : {}),
          ...(str(it.meta, 40) ? { meta: str(it.meta, 40) } : {}),
        };
      })
      .filter((it) => it.id)
      .slice(0, 40);
    if (nodes.length < 2) return null;
    const ids = new Set(nodes.map((n) => n.id));
    const edges: LayoutEdge[] = (Array.isArray(rec.edges) ? rec.edges : [])
      .filter((it): it is Record<string, unknown> => !!it && typeof it === "object")
      .map((it) => ({
        from: str(it.from, 40) ?? "",
        to: str(it.to, 40) ?? "",
        ...(str(it.label, 16) ? { label: str(it.label, 16) } : {}),
      }))
      .filter((it) => ids.has(it.from) && ids.has(it.to) && it.from !== it.to)
      .slice(0, 60);
    const spec: LayoutSpec = { nodes, edges };
    if (title) spec.title = title;
    const summary = str(rec.summary, 200);
    if (summary) spec.summary = summary;
    if (rec.dir === "LR") spec.dir = "LR";
    return { kind: "layout", spec };
  }
  if (l === "chart") {
    const t = typeof rec.type === "string" ? rec.type.toLowerCase().trim() : "";
    // 基础图 + 专业统计图类型白名单（未识别的 type 一律降级为 bar，保持宽容）。
    const KNOWN: ChartType[] = [
      "bar", "hbar", "line", "area", "pie", "radar",
      "scatter", "boxplot", "histogram", "heatmap", "combo", "waterfall", "stacked", "stacked-area",
    ];
    const ALIAS: Record<string, ChartType> = {
      "horizontal": "hbar", "horizontal-bar": "hbar", "horizontal_bar": "hbar",
      "scatterplot": "scatter", "bubble": "scatter",
      "box": "boxplot", "box-plot": "boxplot", "box_plot": "boxplot", "candlestick": "boxplot",
      "hist": "histogram", "distribution": "histogram",
      "heat-map": "heatmap", "heat_map": "heatmap", "matrix": "heatmap", "correlation": "heatmap", "corr": "heatmap",
      "dual": "combo", "dual-axis": "combo", "combo-chart": "combo", "mixed": "combo",
      "water-fall": "waterfall", "funnel": "waterfall",
      "stack": "stacked", "stacked-bar": "stacked", "stacked_column": "stacked",
      "stacked-area": "stacked-area", "stackarea": "stacked-area", "area-stacked": "stacked-area",
    };
    let type: ChartType = KNOWN.includes(t as ChartType) ? (t as ChartType)
      : ALIAS[t] ?? "bar";

    // 结构型图型（数据形状与 labels+series 不同）各自解析，避免走通用路径。
    if (type === "heatmap") {
      const hv = rec.heatmap && typeof rec.heatmap === "object" ? rec.heatmap as Record<string, unknown> : rec;
      const rawVals = Array.isArray(hv.values) ? hv.values : [];
      const values = rawVals
        .filter((r): r is unknown[] => Array.isArray(r))
        .map((r) => toNums(r))
        .filter((r) => r.length > 0)
        .slice(0, 20);
      const w = Math.max(0, ...values.map((r) => r.length));
      if (!values.length || w === 0) return null;
      for (const r of values) while (r.length < w) r.push(0);
      const strs = (v: unknown, n: number) =>
        Array.isArray(v)
          ? v.filter((x): x is string | number => typeof x === "string" || typeof x === "number")
              .map((x) => String(x).slice(0, 24)).slice(0, n)
          : [];
      const hm: HeatmapSpec = { values };
      const xl = strs(hv.xLabels, w);
      const yl = strs(hv.yLabels, values.length);
      if (xl.length) hm.xLabels = xl;
      if (yl.length) hm.yLabels = yl;
      if (hv.mode === "diverging" || hv.mode === "sequential") hm.mode = hv.mode;
      if (Number.isFinite(Number(hv.min))) hm.min = Number(hv.min);
      if (Number.isFinite(Number(hv.max))) hm.max = Number(hv.max);
      const spec: ChartSpec = { type, labels: [], series: [], heatmap: hm };
      if (typeof rec.title === "string" && rec.title.trim()) spec.title = rec.title.trim().slice(0, 80);
      if (typeof rec.unit === "string" && rec.unit.trim()) spec.unit = rec.unit.trim().slice(0, 12);
      return { kind: "chart", spec };
    }

    if (type === "waterfall") {
      const src = Array.isArray(rec.items) ? rec.items
        : Array.isArray(rec.steps) ? rec.steps
        : Array.isArray(rec.series) && rec.series[0] && typeof rec.series[0] === "object"
          ? (rec.series[0] as Record<string, unknown>).data as unknown[]
          : [];
      const items: WaterfallItem[] = (Array.isArray(src) ? src : [])
        .map((it) => {
          if (typeof it === "number") return { label: "", value: it };
          if (!it || typeof it !== "object") return null;
          const r = it as Record<string, unknown>;
          const value = Number(r.value ?? r.delta ?? r.amount);
          if (!Number.isFinite(value)) return null;
          const label = typeof r.label === "string" ? r.label.trim().slice(0, 40)
            : typeof r.name === "string" ? r.name.trim().slice(0, 40) : "";
          const total = r.total === true || r.kind === "total" || r.type === "total";
          return { label, value, ...(total ? { total: true } : {}) };
        })
        .filter((x): x is WaterfallItem => x !== null)
        .slice(0, 24);
      if (items.length < 2) return null;
      const spec: ChartSpec = { type, labels: [], series: [], items };
      if (typeof rec.title === "string" && rec.title.trim()) spec.title = rec.title.trim().slice(0, 80);
      if (typeof rec.unit === "string" && rec.unit.trim()) spec.unit = rec.unit.trim().slice(0, 12);
      return { kind: "chart", spec };
    }

    const series: ChartSeries[] = (Array.isArray(rec.series) ? rec.series : [])
      .filter((s): s is Record<string, unknown> => !!s && typeof s === "object")
      .map((s) => ({
        name: typeof s.name === "string" && s.name.trim() ? s.name.trim() : undefined,
        data: toNums(s.data),
        ...(s.axis === "right" ? { axis: "right" as const } : {}),
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

    // scatter：点集优先，其次才用 labels+data 合成点（x 取 labels 或序号）。
    let points: ScatterPoint[][] | undefined;
    if (type === "scatter") {
      const rawPts = rec.points ?? rec.data;
      // 形态判定：[[x,y],…]（扁平对 = 单系列）vs [[[x,y],…],…]（嵌套 = 多系列）
      // vs [{x,y},…]（对象）。靠首元素判定，避免把扁平对误当多系列。
      const isPair = (el: unknown) =>
        Array.isArray(el) && el.length >= 2 && typeof el[0] === "number" && typeof el[1] === "number";
      let groups: unknown[][] = [];
      if (Array.isArray(rawPts) && rawPts.length) {
        if (isPair(rawPts[0])) groups = [rawPts];
        else if (Array.isArray(rawPts[0])) groups = rawPts as unknown[][];
        else if (rawPts[0] && typeof rawPts[0] === "object") groups = [rawPts];
        else groups = [rawPts];
      }
      const parsed = groups.map((g) => (Array.isArray(g) ? g : [])
        .map((pt) => {
          if (Array.isArray(pt)) {
            const x = Number(pt[0]), y = Number(pt[1]);
            return Number.isFinite(x) && Number.isFinite(y) ? { x, y } : null;
          }
          if (pt && typeof pt === "object") {
            const r = pt as Record<string, unknown>;
            const x = Number(r.x), y = Number(r.y ?? r.value);
            return Number.isFinite(x) && Number.isFinite(y) ? { x, y } : null;
          }
          return null;
        })
        .filter((p): p is ScatterPoint => p !== null)
        .slice(0, 2000))
        .filter((g) => g.length > 0);
      if (parsed.length) {
        points = parsed;
        if (!series.length) {
          series.push(...parsed.map(() => ({ name: undefined, data: [] as number[] })));
        }
      }
    }

    // boxplot：五数概括（每系列一组）。
    let boxStats: BoxStat[][] | undefined;
    if (type === "boxplot") {
      const raw = rec.boxStats ?? rec.boxes ?? rec.groups ?? rec.stats;
      const groups: unknown[][] = Array.isArray(raw) && raw.length && Array.isArray(raw[0])
        ? (raw as unknown[][])
        : (Array.isArray(raw) ? [raw] : []);
      const num = (v: unknown) => {
        const n = Number(v);
        return Number.isFinite(n) ? n : null;
      };
      boxStats = groups.map((g) => (Array.isArray(g) ? g : [])
        .map((it) => {
          if (!it || typeof it !== "object") return null;
          const r = it as Record<string, unknown>;
          const min = num(r.min), q1 = num(r.q1), median = num(r.median ?? r.med), q3 = num(r.q3), max = num(r.max);
          if (min === null || q1 === null || median === null || q3 === null || max === null) return null;
          const label = typeof r.label === "string" ? r.label.trim().slice(0, 30)
            : typeof r.name === "string" ? r.name.trim().slice(0, 30) : "";
          const outliers = toNums(r.outliers).slice(0, 60);
          return { label, min, q1, median, q3, max, ...(outliers.length ? { outliers } : {}) };
        })
        .filter((b): b is BoxStat => b !== null)
        .slice(0, 20))
        .filter((g) => g.length > 0);
      if (!boxStats.length) boxStats = undefined;
    }

    // 各图型的必要条件：结构型看自己的字段，其余必须有 series。
    if (type === "scatter") { if (!points) return null; }
    else if (type === "boxplot") { if (!boxStats) return null; }
    else if (!series.length) return null;

    let labels: string[] | null = null;
    if (Array.isArray(rec.labels)) {
      labels = rec.labels.map((x) => (typeof x === "string" || typeof x === "number") ? String(x) : "");
    }
    if (!labels || !labels.length) {
      const len = type === "scatter" ? Math.max(0, ...(points ?? []).map((g) => g.length))
        : type === "boxplot" ? Math.max(0, ...(boxStats ?? []).map((g) => g.length))
        : series[0]?.data.length ?? 0;
      labels = Array.from({ length: len }, (_, i) => String(i + 1));
    }
    const n = Math.max(labels.length, ...series.map((s) => s.data.length));
    while (labels.length < n) labels.push(String(labels.length + 1));
    for (const s of series) while (s.data.length < n) s.data.push(0);

    const spec: ChartSpec = {
      type,
      labels: labels.slice(0, 60),
      series: series.slice(0, 8).map((s) => ({ ...s, data: s.data.slice(0, 60) })),
    };
    if (points) spec.points = points.slice(0, 8);
    if (type === "scatter" && rec.trend === false) spec.trend = false;
    if (boxStats) spec.boxStats = boxStats.slice(0, 8);
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
// 图表内的「结构性」颜色走主题变量（而非硬编码 hex）：网格/刻度/正文随主题
// 自动适配，与 OpenIntelligentUI design-system 的做法一致；PALETTE 作为
// 数据系列色板保留字面量（需要彼此可区分的稳定色相）。
const C_GRID = "var(--border, #262e3f)";
const C_DIM = "var(--text-2, #6e7a8c)";
const C_MAIN = "var(--text-0, #e6edf3)";
/** 分片/节点的背景描边色（与气泡底色一致，用于切开相邻扇形与数据点）。 */
const C_SURFACE = "var(--bg-1, #161b22)";

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

/** 数据点悬停提示：系列名 · 标签：值 单位。作为 <title> 内容提供原生 tooltip。 */
function seriesTip(spec: ChartSpec, s: ChartSeries, i: number): string {
  const label = spec.labels[i] ?? String(i + 1);
  const val = fmtNum(s.data[i] ?? 0);
  const unit = spec.unit ? ` ${spec.unit}` : "";
  const name = s.name ? `${s.name} · ` : "";
  return `${name}${label}：${val}${unit}`;
}

/** 图表的可访问名称（role=img 需要，否则读屏只念得出「图形」）。
 *  标题缺失时退化为「图表」；附单位与系列名，信息量足够定位。 */
function chartAria(spec: ChartSpec): string {
  const base = spec.title ?? "图表";
  const unit = spec.unit ? `，单位 ${spec.unit}` : "";
  const names = spec.series.map((s) => s.name).filter(Boolean);
  return names.length ? `${base}${unit}，系列：${names.join("、")}` : `${base}${unit}`;
}

/** SVG 图表根元素：统一 viewBox / role=img / aria-label。
 *  SVG 内每个数据图形另挂 <title> 子元素提供悬停原生 tooltip（对齐
 *  OpenIntelligentUI 的图表交互，且不需要任何图表库依赖）。 */
function ChartSvg({ vb, cls = "md-chart-svg", spec, children }: {
  vb: string;
  cls?: string;
  spec: ChartSpec;
  children: React.ReactNode;
}) {
  return (
    <svg viewBox={vb} className={cls} role="img" aria-label={chartAria(spec)}>
      {children}
    </svg>
  );
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
    <ChartSvg vb={`0 0 ${W} ${padT + plotH + padB}`} spec={spec}>
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
              <rect x={x} y={y} width={barW} height={h} rx={2} fill={PALETTE[si % PALETTE.length]}>
                <title>{seriesTip(spec, s, i)}</title>
              </rect>
              {showVals && v > 0 && (
                <text x={x + barW / 2} y={y - 4} textAnchor="middle" fontSize={9.5} fill={C_DIM}>{fmtNum(v)}</text>
              )}
            </g>
          );
        }),
      )}
    </ChartSvg>
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
    <ChartSvg vb={`0 0 ${W} ${H}`} spec={spec}>
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
              <rect x={padL} y={y} width={w} height={bh} rx={2} fill={PALETTE[si % PALETTE.length]}>
                <title>{seriesTip(spec, s, i)}</title>
              </rect>
              {v > 0 && (
                <text x={padL + w + 6} y={y + bh / 2 + 3.5} fontSize={9.5} fill={C_DIM}>{fmtNum(v)}</text>
              )}
            </g>
          );
        }),
      )}
    </ChartSvg>
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
    <ChartSvg vb={`0 0 ${W} ${padT + plotH + padB}`} spec={spec}>
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
              s.data.map((v, i) => (
                <circle key={i} cx={xOf(i)} cy={yOf(v)} r={2.6} fill={color} stroke={C_SURFACE} strokeWidth={1}>
                  <title>{seriesTip(spec, s, i)}</title>
                </circle>
              ))}
          </g>
        );
      })}
    </ChartSvg>
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
      <ChartSvg vb="0 0 260 260" cls="md-chart-svg md-chart-pie" spec={spec}>
        {slices.map((sl, i) => (
          <g key={i}>
            <path d={sl.d} fill={sl.color} stroke={C_SURFACE} strokeWidth={1.5}>
              <title>{`${sl.label}：${fmtNum(sl.value)}${spec.unit ? ` ${spec.unit}` : ""}（${sl.pct.toFixed(1)}%）`}</title>
            </path>
            {sl.showPct && (
              <text x={sl.lx} y={sl.ly + 3.5} textAnchor="middle" fontSize={10.5} fill={C_MAIN} fontWeight={600}>
                {Math.round(sl.pct)}%
              </text>
            )}
          </g>
        ))}
        <text x={cx} y={cy - 2} textAnchor="middle" fontSize={13} fill={C_MAIN} fontWeight={700}>{fmtNum(total)}</text>
        <text x={cx} y={cy + 14} textAnchor="middle" fontSize={9.5} fill={C_DIM}>总计</text>
      </ChartSvg>
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
    <ChartSvg vb={`0 0 ${size} ${size}`} cls="md-chart-svg md-chart-radar" spec={spec}>
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
              return (
                <circle key={i} cx={cx + r * Math.cos(ang(i))} cy={cy + r * Math.sin(ang(i))} r={2.6} fill={color}>
                  <title>{seriesTip(spec, s, i)}</title>
                </circle>
              );
            })}
          </g>
        );
      })}
    </ChartSvg>
  );
}

// ---------------------------------------------------------------- 专业统计图

/** 最小二乘线性回归：返回斜率、截距与决定系数 R²（用于散点趋势线）。 */
function linreg(pts: ScatterPoint[]): { k: number; b: number; r2: number } | null {
  const n = pts.length;
  if (n < 3) return null;
  let sx = 0, sy = 0;
  for (const p of pts) { sx += p.x; sy += p.y; }
  const mx = sx / n, my = sy / n;
  let sxy = 0, sxx = 0, syy = 0;
  for (const p of pts) {
    const dx = p.x - mx, dy = p.y - my;
    sxy += dx * dy; sxx += dx * dx; syy += dy * dy;
  }
  if (sxx === 0) return null;
  const k = sxy / sxx;
  const b = my - k * mx;
  const r2 = syy === 0 ? 1 : (sxy * sxy) / (sxx * syy);
  return { k, b, r2 };
}

/** 把一列数值自动分箱成直方图（Freedman–Diaconis 规则，退回 Sturges，钳制 3..30 箱）。 */
function histogramBins(values: number[]): { centers: number[]; counts: number[]; w: number; lo: number; hi: number } {
  const lo = Math.min(...values), hi = Math.max(...values);
  const span = hi - lo || 1;
  let k = Math.ceil(Math.sqrt(values.length)); // Sturges 近似
  if (values.length >= 4) {
    const sorted = [...values].sort((a, b) => a - b);
    const q = (p: number) => {
      const i = (sorted.length - 1) * p, f = Math.floor(i);
      return sorted[f] + (sorted[Math.min(f + 1, sorted.length - 1)] - sorted[f]) * (i - f);
    };
    const iqr = q(0.75) - q(0.25);
    if (iqr > 0) k = Math.ceil(span / (2 * iqr * Math.pow(values.length, -1 / 3)));
  }
  k = Math.max(3, Math.min(30, k || 5));
  const w = span / k;
  const counts = new Array(k).fill(0);
  for (const v of values) {
    const bi = Math.min(k - 1, Math.max(0, Math.floor((v - lo) / w)));
    counts[bi]++;
  }
  const centers = counts.map((_, i) => lo + w * (i + 0.5));
  return { centers, counts, w, lo, hi };
}

/** 散点图：多点 + 每系列最小二乘趋势线（含 R²）。 */
function ScatterChart({ spec }: { spec: ChartSpec }) {
  const groups = (spec.points ?? []).filter((g) => g.length > 0);
  const ys = groups.flatMap((g) => g.map((p) => p.y));
  const xs = groups.flatMap((g) => g.map((p) => p.x));
  let xlo = Math.min(...xs), xhi = Math.max(...xs);
  let ylo = Math.min(...ys, 0), yhi = Math.max(...ys, 0);
  if (xlo === xhi) { xlo -= 1; xhi += 1; }
  if (ylo === yhi) { ylo -= 1; yhi += 1; }
  const W = 640, padL = 54, padR = 16, padT = 18, plotH = 240, padB = 44;
  const plotW = W - padL - padR;
  const y0 = padT + plotH;
  const X = (v: number) => padL + ((v - xlo) / (xhi - xlo)) * plotW;
  const Y = (v: number) => y0 - ((v - ylo) / (yhi - ylo)) * plotH;
  const showTrend = spec.trend !== false;
  const fits = showTrend ? groups.map((g) => linreg(g)) : [];
  return (
    <ChartSvg vb={`0 0 ${W} ${padT + plotH + padB}`} spec={spec}>
      {[0, 1, 2, 3, 4].map((i) => {
        const v = ylo + ((yhi - ylo) * i) / 4;
        const y = Y(v);
        return (
          <g key={i}>
            <line x1={padL} x2={W - padR} y1={y} y2={y} stroke={C_GRID} strokeWidth={1} />
            <text x={padL - 6} y={y + 3.5} textAnchor="end" fontSize={10} fill={C_DIM}>{fmtNum(v)}</text>
          </g>
        );
      })}
      {groups.map((g, gi) => {
        const color = PALETTE[gi % PALETTE.length];
        const fit = fits[gi];
        return (
          <g key={gi}>
            {fit && (
              <line
                x1={X(xlo)} y1={Y(fit.k * xlo + fit.b)}
                x2={X(xhi)} y2={Y(fit.k * xhi + fit.b)}
                stroke={color} strokeWidth={2} strokeDasharray="6 4" opacity={0.75}
              >
                <title>{`趋势线 y=${fmtNum(fit.k)}x+${fmtNum(fit.b)}，R²=${fit.r2.toFixed(3)}`}</title>
              </line>
            )}
            {g.map((p, i) => (
              <circle key={i} cx={X(p.x)} cy={Y(p.y)} r={3} fill={color} fillOpacity={0.72} stroke={color} strokeWidth={0.8}>
                <title>{seriesPointTip(spec, spec.series[gi]?.name, p.x, p.y)}</title>
              </circle>
            ))}
          </g>
        );
      })}
      {[0, 1, 2, 3, 4].map((i) => {
        const v = xlo + ((xhi - xlo) * i) / 4;
        return <text key={i} x={X(v)} y={y0 + 16} textAnchor="middle" fontSize={10} fill={C_DIM}>{fmtNum(v)}</text>;
      })}
    </ChartSvg>
  );
}

/** 散点提示：系列 · (x, y)。 */
function seriesPointTip(spec: ChartSpec, name: string | undefined, x: number, y: number): string {
  const u = spec.unit ? ` ${spec.unit}` : "";
  return `${name ? `${name} · ` : ""}(${fmtNum(x)}, ${fmtNum(y)}${u})`;
}

/** 箱线图：五数概括（须 + 箱 + 中位线）+ 离群点。 */
function BoxplotChart({ spec }: { spec: ChartSpec }) {
  const groups = (spec.boxStats ?? []).filter((g) => g.length > 0);
  const all: number[] = groups.flatMap((g) => g.flatMap((b) => [b.min, b.max, ...(b.outliers ?? [])]));
  let lo = Math.min(...all), hi = Math.max(...all);
  const pad = (hi - lo || 1) * 0.08;
  lo -= pad; hi += pad;
  const W = 640, padL = 54, padR = 16, padT = 18, plotH = 240, padB = 44;
  const plotW = W - padL - padR;
  const y0 = padT + plotH;
  const Y = (v: number) => y0 - ((v - lo) / (hi - lo)) * plotH;
  // 单系列时并排多组箱盒；多系列时按组内 index 对齐。
  const total = Math.max(...groups.map((g) => g.length));
  const slot = plotW / Math.max(1, total);
  const boxW = Math.max(14, Math.min(52, (slot * 0.6) / groups.length));
  return (
    <ChartSvg vb={`0 0 ${W} ${padT + plotH + padB}`} spec={spec}>
      {[0, 1, 2, 3, 4].map((i) => {
        const v = lo + ((hi - lo) * i) / 4;
        const y = Y(v);
        return (
          <g key={i}>
            <line x1={padL} x2={W - padR} y1={y} y2={y} stroke={C_GRID} strokeWidth={1} />
            <text x={padL - 6} y={y + 3.5} textAnchor="end" fontSize={10} fill={C_DIM}>{fmtNum(v)}</text>
          </g>
        );
      })}
      {groups.map((g, gi) => {
        const color = PALETTE[gi % PALETTE.length];
        return (
          <g key={gi}>
            {g.map((b, i) => {
              const cx = padL + slot * (i + 0.5) + (gi - (groups.length - 1) / 2) * (boxW + 3);
              const yMin = Y(b.min), yMax = Y(b.max), yQ1 = Y(b.q1), yQ3 = Y(b.q3), yMed = Y(b.median);
              return (
                <g key={i}>
                  <line x1={cx} x2={cx} y1={yMax} y2={yQ3} stroke={color} strokeWidth={1.5} />
                  <line x1={cx} x2={cx} y1={yQ1} y2={yMin} stroke={color} strokeWidth={1.5} />
                  <line x1={cx - boxW / 3} x2={cx + boxW / 3} y1={yMax} y2={yMax} stroke={color} strokeWidth={1.5} />
                  <line x1={cx - boxW / 3} x2={cx + boxW / 3} y1={yMin} y2={yMin} stroke={color} strokeWidth={1.5} />
                  <rect
                    x={cx - boxW / 2} y={yQ3} width={boxW} height={Math.max(1, yQ1 - yQ3)}
                    rx={3} fill={softFill(color)} stroke={color} strokeWidth={1.6}
                  >
                    <title>{`${b.label || `组 ${i + 1}`}：中位 ${fmtNum(b.median)}（${fmtNum(b.q1)}~${fmtNum(b.q3)}，极值 ${fmtNum(b.min)}~${fmtNum(b.max)}）`}</title>
                  </rect>
                  <line x1={cx - boxW / 2} x2={cx + boxW / 2} y1={yMed} y2={yMed} stroke={color} strokeWidth={2.4} />
                  {(b.outliers ?? []).map((o, oi) => (
                    <circle key={oi} cx={cx} cy={Y(o)} r={2.6} fill="none" stroke={color} strokeWidth={1.3}>
                      <title>{`离群值 ${fmtNum(o)}`}</title>
                    </circle>
                  ))}
                </g>
              );
            })}
          </g>
        );
      })}
      {Array.from({ length: total }, (_, i) => {
        const cx = padL + slot * (i + 0.5);
        const lbl = (groups[0]?.[i]?.label) || spec.labels[i] || String(i + 1);
        return <text key={i} x={cx} y={y0 + 16} textAnchor="middle" fontSize={10} fill={C_DIM}>{trunc(lbl, 8)}</text>;
      })}
    </ChartSvg>
  );
}

/** 直方图：对 series[0] 的取值自动分箱（FD 规则）并画频数柱。 */
function HistogramChart({ spec }: { spec: ChartSpec }) {
  const values = spec.series[0]?.data ?? [];
  if (!values.length) return <div className="md-block-error">⚠ 直方图缺少数值</div>;
  const { centers, counts, w, lo } = histogramBins(values);
  const max = niceMax(Math.max(...counts));
  const W = 640, padL = 50, padR = 14, padT = 16, plotH = 230, padB = 44;
  const w0 = W - padR;
  const y0 = padT + plotH;
  const binW = ((w0 - padL) / centers.length) * 0.92;
  const X = (i: number) => padL + ((w0 - padL) * (i + 0.5)) / centers.length;
  return (
    <ChartSvg vb={`0 0 ${W} ${padT + plotH + padB}`} spec={spec}>
      <YGrid max={max} plotH={plotH} y0={y0} padL={padL} w={w0} />
      {counts.map((c, i) => {
        const h = (c / max) * plotH;
        return (
          <rect key={i} x={X(i) - binW / 2} y={y0 - h} width={binW} height={Math.max(c > 0 ? 1 : 0, h)}
            rx={2} fill={PALETTE[0]} fillOpacity={0.85}>
            <title>{`${fmtNum(lo + w * i)}~${fmtNum(lo + w * (i + 1))}：${c} 个`}</title>
          </rect>
        );
      })}
      {centers.map((_, i) =>
        i % Math.ceil(centers.length / 8) === 0 ? (
          <text key={i} x={X(i)} y={y0 + 16} textAnchor="middle" fontSize={9.5} fill={C_DIM}>
            {fmtNum(lo + w * i)}
          </text>
        ) : null,
      )}
    </ChartSvg>
  );
}

/** 热力图色阶：顺序=靛蓝深浅；发散=红-中性-绿（相关性 -1..1 用）。 */
function heatColor(t: number, mode: "sequential" | "diverging"): string {
  const c = Math.max(0, Math.min(1, t));
  const lerp = (a: number[], b: number[], u: number) => [
    Math.round(a[0] + (b[0] - a[0]) * u),
    Math.round(a[1] + (b[1] - a[1]) * u),
    Math.round(a[2] + (b[2] - a[2]) * u),
  ];
  if (mode === "diverging") {
    // #f85149(红 -1) → #2a3242(中性 0) → #3fb950(绿 +1)
    const RED = [248, 81, 73], MID = [42, 50, 66], GREEN = [63, 185, 80];
    const [r, g, b] = c <= 0.5 ? lerp(RED, MID, c * 2) : lerp(MID, GREEN, (c - 0.5) * 2);
    return `rgb(${r},${g},${b})`;
  }
  // 靛蓝顺序色阶：#1c2330 → #8b5cf6
  const [r, g, b] = lerp([28, 35, 48], [139, 92, 246], c);
  return `rgb(${r},${g},${b})`;
}

/** 热力图：矩阵单元格 + 数值标注 + 行列标签（label/color 走 inline style，
 *  React 不接受 SVG 的 rgb() 直接内联属性色，故统一用 style）。 */
function HeatmapChart({ spec }: { spec: ChartSpec }) {
  const hm = spec.heatmap;
  if (!hm || !hm.values.length) return <div className="md-block-error">⚠ 热力图缺少数据</div>;
  const rows = hm.values.length, cols = Math.max(...hm.values.map((r) => r.length));
  const flat = hm.values.flat();
  const dataMin = hm.min ?? Math.min(...flat);
  const dataMax = hm.max ?? Math.max(...flat);
  const span = dataMax - dataMin || 1;
  const cell = Math.max(20, Math.min(48, Math.floor(420 / Math.max(cols, rows))));
  const padL = hm.yLabels ? 74 : 8;
  const padT = hm.xLabels ? 22 : 8;
  const W = padL + cols * cell + 8, H = padT + rows * cell + 8;
  return (
    <div className="md-chart-heatmap">
      <ChartSvg vb={`0 0 ${W} ${H}`} cls="md-chart-svg md-chart-heat" spec={spec}>
        {hm.values.map((row, ri) => (
          <g key={ri}>
            {row.map((v, ci) => {
              const t = hm.mode === "diverging"
                ? (v - dataMin) / span
                : (v - dataMin) / span;
              const dark = t > 0.55;
              return (
                <g key={ci}>
                  <rect
                    x={padL + ci * cell} y={padT + ri * cell}
                    width={cell - 1.5} height={cell - 1.5} rx={3}
                    style={{ fill: heatColor(t, hm.mode ?? "sequential") }}
                  >
                    <title>{`${hm.yLabels?.[ri] ?? `行 ${ri + 1}`} × ${hm.xLabels?.[ci] ?? `列 ${ci + 1}`}：${fmtNum(v)}`}</title>
                  </rect>
                  {cell >= 30 && (
                    <text
                      x={padL + ci * cell + cell / 2} y={padT + ri * cell + cell / 2 + 3.5}
                      textAnchor="middle" fontSize={10.5} fontWeight={600}
                      style={{ fill: dark ? "#fff" : "var(--text-0, #e6edf3)", pointerEvents: "none" }}
                    >{fmtNum(v)}</text>
                  )}
                </g>
              );
            })}
          </g>
        ))}
        {hm.xLabels && hm.xLabels.map((lb, ci) => (
          <text key={ci} x={padL + ci * cell + cell / 2} y={padT - 7} textAnchor="middle" fontSize={10} fill={C_DIM}>
            {trunc(lb, Math.max(4, Math.floor(cell / 7)))}
          </text>
        ))}
        {hm.yLabels && hm.yLabels.map((lb, ri) => (
          <text key={ri} x={padL - 7} y={padT + ri * cell + cell / 2 + 3.5} textAnchor="end" fontSize={10} fill={C_DIM}>
            {trunc(lb, 10)}
          </text>
        ))}
      </ChartSvg>
      {hm.mode === "diverging" && (
        <div className="md-chart-heat-scale">
          <span style={{ background: heatColor(0, "diverging") }} />{fmtNum(dataMin)}
          <span style={{ background: heatColor(0.5, "diverging") }} />{fmtNum((dataMin + dataMax) / 2)}
          <span style={{ background: heatColor(1, "diverging") }} />{fmtNum(dataMax)}
        </div>
      )}
    </div>
  );
}

/** 双轴组合图：axis=right 的系列画折线（右轴），其余画柱（左轴）。 */
function ComboChart({ spec }: { spec: ChartSpec }) {
  const bars = spec.series.filter((s) => s.axis !== "right");
  const lines = spec.series.filter((s) => s.axis === "right");
  const n = spec.labels.length;
  const leftMax = niceMax(Math.max(0, ...bars.flatMap((s) => s.data)));
  const lineVals = lines.flatMap((s) => s.data);
  const rLo = Math.min(0, ...lineVals), rHi = Math.max(...lineVals, 1);
  const W = 640, padL = 52, padR = 56, padT = 16, plotH = 230, padB = 44;
  const w = W - padR;
  const y0 = padT + plotH;
  const groupW = (w - padL) / n;
  const barW = Math.max(4, Math.min(40, (groupW * 0.6) / Math.max(1, bars.length)));
  const blockW = bars.length * barW + (bars.length - 1) * 3;
  const RY = (v: number) => y0 - ((v - rLo) / (rHi - rLo)) * plotH;
  const showLegend = bars.length > 0 && lines.length > 0;
  return (
    <>
      <ChartSvg vb={`0 0 ${W} ${padT + plotH + padB}`} spec={spec}>
        <YGrid max={leftMax} plotH={plotH} y0={y0} padL={padL} w={w} />
        {spec.labels.map((lb, i) => (
          <text key={i} x={padL + i * groupW + groupW / 2} y={y0 + 16}
            textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"} fontSize={10} fill={C_DIM}>
            {trunc(lb, 10)}
          </text>
        ))}
        {bars.map((s, si) =>
          s.data.map((v, i) => {
            const h = Math.max(v > 0 ? 2 : 0, (Math.max(0, v) / leftMax) * plotH);
            const x = padL + i * groupW + groupW / 2 - blockW / 2 + si * (barW + 3);
            return (
              <rect key={`${si}-${i}`} x={x} y={y0 - h} width={barW} height={h} rx={2} fill={PALETTE[si % PALETTE.length]}>
                <title>{seriesTip(spec, s, i)}</title>
              </rect>
            );
          }),
        )}
        {lines.map((s, si) => {
          const color = PALETTE[(bars.length + si) % PALETTE.length];
          const pts = s.data.map((v, i) => `${padL + i * groupW + groupW / 2},${RY(v)}`).join(" ");
          return (
            <g key={si}>
              <polyline points={pts} fill="none" stroke={color} strokeWidth={2.2} strokeLinejoin="round" strokeLinecap="round" />
              {s.data.map((v, i) => (
                <circle key={i} cx={padL + i * groupW + groupW / 2} cy={RY(v)} r={3} fill={color} stroke={C_SURFACE} strokeWidth={1}>
                  <title>{seriesTip(spec, s, i)}</title>
                </circle>
              ))}
            </g>
          );
        })}
      </ChartSvg>
      {showLegend && (
        <div className="md-chart-legend">
          {bars.map((s, i) => (
            <span key={`b${i}`} className="md-chart-legend-item">
              <span className="md-chart-dot" style={{ background: PALETTE[i % PALETTE.length] }} />
              {s.name ?? `系列 ${i + 1}`}（左轴）
            </span>
          ))}
          {lines.map((s, i) => (
            <span key={`l${i}`} className="md-chart-legend-item">
              <span className="md-chart-dot" style={{ background: PALETTE[(bars.length + i) % PALETTE.length] }} />
              {s.name ?? `系列 ${bars.length + i + 1}`}（右轴）
            </span>
          ))}
        </div>
      )}
    </>
  );
}

/** 瀑布图：增量柱悬空连接，total 柱从 0 起。 */
function WaterfallChart({ spec }: { spec: ChartSpec }) {
  const items = spec.items ?? [];
  // 计算每根柱的起止高度：total 归零重算，增量在上一个累计值上叠加。
  let acc = 0;
  const bars = items.map((it) => {
    if (it.total) {
      const from = 0, to = acc;
      return { label: it.label, from, to, delta: it.value, total: true };
    }
    const from = acc;
    acc += it.value;
    return { label: it.label, from, to: acc, delta: it.value, total: false };
  });
  const allVals = bars.flatMap((b) => [b.from, b.to]);
  const hi = niceMax(Math.max(0, ...allVals));
  const lo = Math.min(0, ...allVals);
  const W = 640, padL = 54, padR = 14, padT = 16, plotH = 230, padB = 46;
  const plotW = W - padL - padR;
  const span = hi - lo || 1;
  const y0 = padT + plotH;
  const Y = (v: number) => y0 - ((v - lo) / span) * plotH;
  const slot = plotW / bars.length;
  const barW = Math.max(10, Math.min(46, slot * 0.62));
  return (
    <ChartSvg vb={`0 0 ${W} ${padT + plotH + padB}`} spec={spec}>
      {[0, 1, 2, 3, 4].map((i) => {
        const v = lo + (span * i) / 4;
        const y = Y(v);
        return (
          <g key={i}>
            <line x1={padL} x2={W - padR} y1={y} y2={y} stroke={C_GRID} strokeWidth={1} />
            <text x={padL - 6} y={y + 3.5} textAnchor="end" fontSize={10} fill={C_DIM}>{fmtNum(v)}</text>
          </g>
        );
      })}
      <line x1={padL} x2={W - padR} y1={Y(0)} y2={Y(0)} stroke={C_DIM} strokeWidth={1.2} />
      {bars.map((b, i) => {
        const cx = padL + slot * (i + 0.5);
        const y1 = Y(b.from), y2 = Y(b.to);
        const top = Math.min(y1, y2), h = Math.max(2, Math.abs(y2 - y1));
        const color = b.total ? "#8b5cf6" : b.delta >= 0 ? "#3fb950" : "#f85149";
        return (
          <g key={i}>
            <rect x={cx - barW / 2} y={top} width={barW} height={h} rx={2} fill={color} fillOpacity={b.total ? 0.95 : 0.85}>
              <title>{`${b.label || `步骤 ${i + 1}`}：${b.delta >= 0 && !b.total ? "+" : ""}${fmtNum(b.delta)}${spec.unit ? ` ${spec.unit}` : ""}`}</title>
            </rect>
            <text x={cx} y={b.delta >= 0 ? top - 4 : top + h + 11} textAnchor="middle" fontSize={9.5} fill={C_DIM}>
              {b.total ? fmtNum(b.to) : `${b.delta >= 0 ? "+" : ""}${fmtNum(b.delta)}`}
            </text>
            {i < bars.length - 1 && (
              <line x1={cx + barW / 2} x2={padL + slot * (i + 1.5) - barW / 2} y1={Y(b.to)} y2={Y(b.to)}
                stroke={C_DIM} strokeWidth={1} strokeDasharray="3 3" opacity={0.6} />
            )}
            <text x={cx} y={y0 + 17} textAnchor="middle" fontSize={9.5} fill={C_DIM}>{trunc(b.label, 8)}</text>
          </g>
        );
      })}
    </ChartSvg>
  );
}

/** 堆叠柱 / 堆叠面积：按系列逐层累加（第一个系列在最底部）。 */
function StackedChart({ spec, areaMode }: { spec: ChartSpec; areaMode?: boolean }) {
  const n = spec.labels.length;
  const ns = spec.series.length;
  const totals = Array.from({ length: n }, (_, i) => spec.series.reduce((a, s) => a + Math.max(0, s.data[i] ?? 0), 0));
  const max = niceMax(Math.max(0, ...totals));
  const W = 640, padL = 50, padR = 14, padT = 16, plotH = 230, padB = 44;
  const w = W - padR;
  const y0 = padT + plotH;
  const X = (i: number) => n <= 1 ? padL + (w - padL) / 2 : padL + ((w - padL) * i) / (n - 1);
  const Y = (v: number) => y0 - (Math.max(0, v) / max) * plotH;
  if (areaMode) {
    // 逐系列累加基线，画堆叠面积（band 由下往上）。
    const lower = new Array(n).fill(0);
    const bands = spec.series.map((s, si) => {
      const upper = s.data.map((v, i) => lower[i] + Math.max(0, v));
      const poly = [
        ...s.data.map((_, i) => `${X(i)},${Y(upper[i])}`),
        ...[...s.data.keys()].reverse().map((i) => `${X(i)},${Y(lower[i])}`),
      ].join(" ");
      s.data.forEach((v, i) => { lower[i] = upper[i]; });
      return { pts: poly, color: PALETTE[si % PALETTE.length], name: s.name };
    });
    return (
      <ChartSvg vb={`0 0 ${W} ${padT + plotH + padB}`} spec={spec}>
        <YGrid max={max} plotH={plotH} y0={y0} padL={padL} w={w} />
        {bands.map((b, i) => (
          <polygon key={i} points={b.pts} fill={b.color} fillOpacity={0.7} stroke={b.color} strokeWidth={1}>
            <title>{b.name ?? `系列 ${i + 1}`}</title>
          </polygon>
        ))}
        {spec.labels.map((lb, i) =>
          i % Math.ceil(n / 10) === 0 ? (
            <text key={i} x={X(i)} y={y0 + 16} textAnchor="middle" fontSize={10} fill={C_DIM}>{trunc(lb, 10)}</text>
          ) : null,
        )}
      </ChartSvg>
    );
  }
  const groupW = (w - padL) / n;
  const barW = Math.max(6, Math.min(48, groupW * 0.62));
  return (
    <ChartSvg vb={`0 0 ${W} ${padT + plotH + padB}`} spec={spec}>
      <YGrid max={max} plotH={plotH} y0={y0} padL={padL} w={w} />
      {spec.labels.map((lb, i) => (
        <text key={i} x={padL + i * groupW + groupW / 2} y={y0 + 16}
          textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"} fontSize={10} fill={C_DIM}>
          {trunc(lb, 10)}
        </text>
      ))}
      {spec.labels.map((_, i) => {
        let acc = 0;
        const cx = padL + i * groupW + groupW / 2;
        return (
          <g key={i}>
            {spec.series.map((s, si) => {
              const v = Math.max(0, s.data[i] ?? 0);
              const h = (v / max) * plotH;
              const y = Y(acc + v);
              acc += v;
              return (
                <rect key={si} x={cx - barW / 2} y={y} width={barW} height={Math.max(0, h)} rx={si === ns - 1 ? 2 : 0}
                  fill={PALETTE[si % PALETTE.length]}>
                  <title>{seriesTip(spec, s, i)}</title>
                </rect>
              );
            })}
          </g>
        );
      })}
    </ChartSvg>
  );
}

export function ChartBlock({ spec }: { spec: ChartSpec }) {
  const multi = spec.series.length > 1 && spec.type !== "pie";
  const isHeat = spec.type === "heatmap";
  const isCombo = spec.type === "combo";
  return (
    <div className="md-chart">
      {(spec.title || spec.unit) && (
        <div className="md-chart-head">
          {spec.title && <span className="md-chart-title">{spec.title}</span>}
          {spec.unit && <span className="md-chart-unit">单位：{spec.unit}</span>}
        </div>
      )}
      <ExportMenu build={(host) => chartExportItems(spec, host)} />
      {spec.type === "hbar" ? <HBarChart spec={spec} />
        : spec.type === "line" ? <LineChart spec={spec} />
        : spec.type === "area" ? <LineChart spec={spec} area />
        : spec.type === "pie" ? <PieChart spec={spec} />
        : spec.type === "radar" ? <RadarChart spec={spec} />
        : spec.type === "scatter" ? <ScatterChart spec={spec} />
        : spec.type === "boxplot" ? <BoxplotChart spec={spec} />
        : spec.type === "histogram" ? <HistogramChart spec={spec} />
        : spec.type === "heatmap" ? <HeatmapChart spec={spec} />
        : spec.type === "combo" ? <ComboChart spec={spec} />
        : spec.type === "waterfall" ? <WaterfallChart spec={spec} />
        : spec.type === "stacked" ? <StackedChart spec={spec} />
        : spec.type === "stacked-area" ? <StackedChart spec={spec} areaMode />
        : <BarChart spec={spec} />}
      {multi && !isCombo && !isHeat && (
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
      <ExportMenu build={() => tableExportItems(spec)} />
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

// ---------------------------------------------------------------- 图形图解

/** 图形图解渲染：部件分色 SVG + 点击高亮（其余变暗）+ 说明区。
 *  安全线：只渲染白名单原语（解析器已 clamp 坐标 / 清洗 path d），
 *  不插入任何用户文本进 SVG 属性，无脚本。 */
const DIAGRAM_COLORS = ["#4f8cff", "#3fb950", "#d29922", "#f778ba", "#39c5cf", "#ff7b72", "#d2a8ff", "#e3b341"];

/** hex 主题色 → 低饱和填充色（同色 12% 透明度叠在暗底上≈浅色底纹）。 */
function softFill(color: string): string {
  return `${color}1f`; // 8 位 hex：末两位 ≈12% alpha
}

function renderShape(s: DiagramShape, color: string, key: number) {
  // 面图形（圆/椭圆/矩形）用低饱和填充 + 描边——从「线框」升级为「填充卡」，
  // 视觉层次更接近 GPT 的图解（形状实底、连线细）；线类保持细描边。
  // key 必须显式传给每个元素：塞进 spread 的 props 会触发 React 的
  // 「key is being spread」警告。
  const common = { stroke: color, strokeWidth: 3, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };
  const soft = softFill(color);
  switch (s.t) {
    case "circle": return <circle key={key} {...common} fill={soft} cx={s.x} cy={s.y} r={s.r} />;
    case "ellipse": return <ellipse key={key} {...common} fill={soft} cx={s.x} cy={s.y} rx={s.rx} ry={s.ry} />;
    case "rect": return <rect key={key} {...common} fill={soft} x={s.x} y={s.y} width={s.w} height={s.h} rx={s.rx ?? 10} />;
    case "line": return <line key={key} {...{ ...common, fill: "none" }} x1={s.x1} y1={s.y1} x2={s.x2} y2={s.y2} />;
    case "polyline": return <polyline key={key} {...{ ...common, fill: "none" }} points={s.pts.map((p) => p.join(",")).join(" ")} />;
    case "path": return <path key={key} {...{ ...common, fill: "none" }} d={s.d} />;
    case "label":
      return (
        <text
          key={key}
          x={s.x}
          y={s.y}
          fill="var(--text-0, #e6edf3)"
          fontSize={s.size ?? 13}
          fontWeight={600}
          textAnchor={s.anchor ?? "middle"}
          dominantBaseline="middle"
          style={{ pointerEvents: "none", userSelect: "none" }}
        >
          {s.text}
        </text>
      );
  }
}

export function DiagramBlock({ spec }: { spec: DiagramSpec }) {
  const [active, setActive] = useState(0);
  const parts = spec.parts;
  const cur = parts[Math.min(active, parts.length - 1)];
  const canvas = spec.canvas ?? { w: 470, h: 300 };
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
          >
            <span className="md-diagram-dot" style={{ background: DIAGRAM_COLORS[i % DIAGRAM_COLORS.length] }} />
            <span className="md-ix-name">{p.name}</span>
            {p.meta && <span className="md-ix-meta">{p.meta}</span>}
          </button>
        ))}
      </div>
      <div className="md-diagram-svg-wrap">
        <svg viewBox={`0 0 ${canvas.w} ${canvas.h}`} className="md-diagram-svg" role="img" aria-label={spec.title ?? "图解"}>
          {parts.map((p, i) => (
            <g
              key={i}
              className={`md-diagram-part ${i === active ? "hot" : "dim"}`}
              onClick={() => setActive(i)}
              role="img"
              aria-label={`${p.name}（点击查看说明）`}
            >
              <title>{p.meta ? `${p.name} · ${p.meta}` : p.name}</title>
              {p.shapes.map((s, si) => renderShape(s, DIAGRAM_COLORS[i % DIAGRAM_COLORS.length], si))}
            </g>
          ))}
        </svg>
      </div>
      <div className="md-ix-detail" role="tabpanel">
        <div className="md-ix-detail-head">
          <span className="md-diagram-dot" style={{ background: DIAGRAM_COLORS[Math.min(active, parts.length - 1) % DIAGRAM_COLORS.length] }} />
          <span className="md-ix-detail-name">{cur.name}</span>
          {cur.meta && <span className="md-ix-meta">{cur.meta}</span>}
        </div>
        <div className="md-ix-desc">{cur.desc}</div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 交互式图解

/** 交互式图解：分块标签（可点击）+ 当前分块说明。
 *  对齐 GPT Intelligent UI 的「点不同模块看对应原理」；纯 state 切换，
 *  不执行任何脚本（安全模型与 render_card 沙箱一致）。 */
export function InteractiveBlock({ spec }: { spec: InteractiveSpec }) {
  const [active, setActive] = useState(0);
  const parts = spec.parts;
  const cur = parts[Math.min(active, parts.length - 1)];
  const canvas = spec.canvas ?? { w: 470, h: 190 };
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
      {/* 分块聚焦视图（interactive × diagram 组合）：选中块带 shapes 时渲染
          该块的示意图（单块单色，纯展示无点击语义） */}
      {cur.shapes && cur.shapes.length > 0 && (
        <div className="md-diagram-svg-wrap">
          <svg viewBox={`0 0 ${canvas.w} ${canvas.h}`} className="md-diagram-svg" role="img">
            {cur.shapes.map((s, si) => renderShape(s, "var(--accent)", si))}
          </svg>
        </div>
      )}
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

// ---------------------------------------------------------------- 语义布局图（```layout）

/** 分层布局算法：语义结构 → 坐标（LLM 不手排坐标的兑现处）。
 *
 * 规则：
 * - level 显式优先；缺省按 edges 拓扑深度推导（无入边 = 0 层，环按出现序兜底）；
 * - 同层横排等距（LR 模式则纵排），层间距固定；组内相邻聚簇（同 group 排一起）；
 * - 节点盒尺寸按 label 长度估算（中文 13px/字，宽 90-220 clamp）；
 * - 边画为折线（同层内垂直、跨层走层间隙），label 放中点。
 * 返回 { nodes: {id,label,x,y,w,h,color}, edges: 折线点集, canvas }。
 */
export function computeLayout(spec: LayoutSpec): {
  nodes: (LayoutNode & { x: number; y: number; w: number; h: number; colorIdx: number })[];
  edges: { pts: [number, number][]; label?: string; from: string; to: string }[];
  canvas: { w: number; h: number };
} {
  const dir = spec.dir ?? "TB";
  const nodes = spec.nodes;

  // 1. 层级：显式优先，缺省拓扑深度（Kahn 式；剩余节点按出现序续层）
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const levelOf = new Map<string, number>();
  nodes.forEach((n) => { if (n.level !== undefined) levelOf.set(n.id, n.level); });
  const inDeg = new Map<string, number>(nodes.map((n) => [n.id, 0]));
  const out: Map<string, string[]> = new Map(nodes.map((n) => [n.id, []]));
  spec.edges.forEach((e) => {
    if (levelOf.has(e.from) && levelOf.has(e.to)) return; // 两端都显式定层，边不参与推导
    inDeg.set(e.to, (inDeg.get(e.to) ?? 0) + 1);
    out.get(e.from)?.push(e.to);
  });
  const queue = nodes.filter((n) => !levelOf.has(n.id) && (inDeg.get(n.id) ?? 0) === 0).map((n) => n.id);
  queue.forEach((id) => levelOf.set(id, 0));
  let qi = 0;
  while (qi < queue.length) {
    const id = queue[qi++];
    const lv = levelOf.get(id) ?? 0;
    out.get(id)?.forEach((next) => {
      if (levelOf.has(next) && levelOf.get(next)! > lv) return;
      levelOf.set(next, Math.max(levelOf.get(next) ?? 0, lv + 1));
      const d = (inDeg.get(next) ?? 1) - 1;
      inDeg.set(next, d);
      if (d === 0 && !levelOf.has(next)) queue.push(next);
    });
  }
  // 环/孤岛兜底：未定层的按出现序给当前最大层 +1
  let maxLv = Math.max(0, ...[...levelOf.values()]);
  nodes.forEach((n) => { if (!levelOf.has(n.id)) levelOf.set(n.id, ++maxLv); });

  // 2. 分层 + 组内聚簇排序（同层内按 group 聚在一起）
  const levels = new Map<number, LayoutNode[]>();
  nodes.forEach((n) => {
    const lv = levelOf.get(n.id) ?? 0;
    if (!levels.has(lv)) levels.set(lv, []);
    levels.get(lv)!.push(n);
  });
  const groupIdx = new Map<string, number>();
  let groupSeq = 0;
  nodes.forEach((n) => {
    const g = n.group ?? n.id;
    if (!groupIdx.has(g)) groupIdx.set(g, groupSeq++);
  });
  [...levels.keys()].sort((a, b) => a - b).forEach((lv) => {
    levels.get(lv)!.sort((a, b) =>
      (groupIdx.get(a.group ?? a.id) ?? 0) - (groupIdx.get(b.group ?? b.id) ?? 0));
  });

  // 3. 盒尺寸（按 label 长度）
  const boxOf = (n: LayoutNode) => {
    const chars = Math.max(2, n.label.length);
    const w = Math.min(220, Math.max(88, chars * 15 + 28));
    return { w, h: 44 };
  };

  // 4. 坐标：层间距 74，同层间距 26；画布按内容自适应
  const LAYER_GAP = 74, SIBLING_GAP = 26, PAD = 24;
  const rows = [...levels.keys()].sort((a, b) => a - b).map((lv) => levels.get(lv)!);
  const rowWidths = rows.map((row) => row.reduce((s, n) => s + boxOf(n).w, 0) + SIBLING_GAP * (row.length - 1));
  const maxRowW = Math.max(120, ...rowWidths);
  const canvasW = maxRowW + PAD * 2;
  const canvasH = rows.length * 44 + (rows.length - 1) * LAYER_GAP + PAD * 2;

  const placed = new Map<string, { x: number; y: number; w: number; h: number }>();
  rows.forEach((row, ri) => {
    const rowW = rowWidths[ri];
    let cur = (canvasW - rowW) / 2;
    row.forEach((n) => {
      const { w, h } = boxOf(n);
      if (dir === "TB") placed.set(n.id, { x: cur, y: PAD + ri * (44 + LAYER_GAP), w, h });
      else placed.set(n.id, { x: PAD + ri * (44 + LAYER_GAP), y: cur, w, h }); // LR：层→列
      cur += w + SIBLING_GAP;
    });
  });

  // 5. 边折线：TB 从下边中点 → 层间隙水平 → 上边中点（跨多层走两侧绕行简化为直折线）
  const edges = spec.edges.map((e) => {
    const a = placed.get(e.from)!;
    const b = placed.get(e.to)!;
    let pts: [number, number][];
    if (dir === "TB") {
      const ax = a.x + a.w / 2, bx = b.x + b.w / 2;
      const midY = (a.y + a.h + b.y) / 2;
      if (b.y >= a.y + a.h) {
        pts = [[ax, a.y + a.h], [ax, midY], [bx, midY], [bx, b.y]];
      } else {
        // 同层/回边：绕右侧
        const rx = Math.max(a.x + a.w, b.x + b.w) + 18;
        pts = [[ax, a.y + a.h], [ax, midY], [rx, midY], [rx, b.y + b.h / 2], [b.x + b.w, b.y + b.h / 2]];
      }
    } else {
      const ay = a.y + a.h / 2, by = b.y + b.h / 2;
      const midX = (a.x + a.w + b.x) / 2;
      if (b.x >= a.x + a.w) {
        pts = [[a.x + a.w, ay], [midX, ay], [midX, by], [b.x, by]];
      } else {
        const ry = Math.max(a.y + a.h, b.y + b.h) + 18;
        pts = [[a.x + a.w, ay], [midX, ay], [midX, ry], [b.x + b.w / 2, ry], [b.x + b.w / 2, b.y + b.h]];
      }
    }
    return { pts, from: e.from, to: e.to, ...(e.label ? { label: e.label } : {}) };
  });

  return {
    nodes: nodes.map((n) => ({
      ...n, ...placed.get(n.id)!,
      colorIdx: (groupIdx.get(n.group ?? n.id) ?? 0) % DIAGRAM_COLORS.length,
    })),
    edges,
    canvas: { w: Math.max(canvasW, dir === "TB" ? canvasW : canvasH), h: dir === "TB" ? canvasH : canvasW },
  };
}

export function LayoutBlock({ spec }: { spec: LayoutSpec }) {
  const [hover, setHover] = useState<string | null>(null);
  const { nodes, edges, canvas } = useMemo(() => computeLayout(spec), [spec]);
  const node = (id: string) => nodes.find((n) => n.id === id);
  return (
    <div className="md-rich">
      {spec.title && <div className="md-rich-title">{spec.title}</div>}
      {spec.summary && <div className="md-ix-summary">{spec.summary}</div>}
      <div className="md-diagram-svg-wrap">
        <svg viewBox={`0 0 ${canvas.w} ${canvas.h}`} className="md-diagram-svg" role="img">
          {/* 边（先画在下层） */}
          {edges.map((e, i) => {
            const dimmed = hover !== null && hover !== e.from && hover !== e.to;
            return (
              <g key={`e${i}`} opacity={dimmed ? 0.25 : 0.9}>
                <polyline
                  points={e.pts.map((p) => p.join(",")).join(" ")}
                  fill="none"
                  stroke="var(--text-2, #6e7a8c)"
                  strokeWidth={1.6}
                  strokeLinejoin="round"
                />
                {/* 终点小箭头：最后一段方向 */}
                {(() => {
                  const n = e.pts.length;
                  if (n < 2) return null;
                  const [x1, y1] = e.pts[n - 2];
                  const [x2, y2] = e.pts[n - 1];
                  const ang = Math.atan2(y2 - y1, x2 - x1);
                  const s = 6;
                  const p1 = `${x2 - s * Math.cos(ang - 0.45)},${y2 - s * Math.sin(ang - 0.45)}`;
                  const p2 = `${x2 - s * Math.cos(ang + 0.45)},${y2 - s * Math.sin(ang + 0.45)}`;
                  return <polygon points={`${x2},${y2} ${p1} ${p2}`} fill="var(--text-2, #6e7a8c)" />;
                })()}
                {e.label && (
                  <text
                    x={(e.pts[0][0] + e.pts[e.pts.length - 1][0]) / 2}
                    y={(e.pts[0][1] + e.pts[e.pts.length - 1][1]) / 2 - 4}
                    textAnchor="middle" fontSize={10.5} fill="var(--text-2, #6e7a8c)"
                    style={{ pointerEvents: "none" }}
                  >{e.label}</text>
                )}
              </g>
            );
          })}
          {/* 节点盒 */}
          {nodes.map((n, i) => {
            const color = DIAGRAM_COLORS[n.colorIdx];
            const dimmed = hover !== null && hover !== n.id;
            return (
              <g
                key={n.id + i}
                opacity={dimmed ? 0.35 : 1}
                onMouseEnter={() => setHover(n.id)}
                onMouseLeave={() => setHover(null)}
              >
                <rect x={n.x} y={n.y} width={n.w} height={n.h} rx={10}
                  fill={softFill(color)} stroke={color} strokeWidth={dimmed ? 1.5 : 2.4} />
                <text x={n.x + n.w / 2} y={n.y + n.h / 2} textAnchor="middle" dominantBaseline="middle"
                  fontSize={13} fontWeight={600} fill="var(--text-0, #e6edf3)"
                  style={{ pointerEvents: "none" }}>{n.label}</text>
                {n.meta && (
                  <text x={n.x + n.w / 2} y={n.y + n.h - 8} textAnchor="middle"
                    fontSize={9.5} fill="var(--text-2, #6e7a8c)"
                    style={{ pointerEvents: "none" }}>{n.meta}</text>
                )}
              </g>
            );
          })}
        </svg>
      </div>
      {/* 图例（分组色） */}
      {(() => {
        const groups = [...new Set(nodes.map((n) => n.group).filter(Boolean))] as string[];
        if (groups.length < 2) return null;
        return (
          <div className="md-layout-legend">
            {groups.map((g, i) => (
              <span key={g} className="md-layout-legend-item">
                <span className="md-diagram-dot" style={{ background: DIAGRAM_COLORS[(groupSeqOf(nodes, g)) % DIAGRAM_COLORS.length] }} />
                {g}
              </span>
            ))}
          </div>
        );
      })()}
    </div>
  );
}

function groupSeqOf(nodes: { group?: string }[], group: string): number {
  const seen = new Set<string>();
  for (const n of nodes) {
    const g = n.group ?? "";
    if (g && !seen.has(g)) {
      seen.add(g);
      if (g === group) return seen.size - 1;
    }
  }
  return 0;
}

// ---------------------------------------------------------------- Mermaid 图（```mermaid）

/** Mermaid 渲染（strict 安全级：禁 HTML/脚本）。
 *  异步渲染 + 失败降级：DSL 语法错 → 显示原文本代码块（用户还能看清内容）。
 *  懒初始化：首个 mermaid 块出现才 import（约 2MB，不该进主 bundle）。 */
let mermaidInit: Promise<typeof import("mermaid").default> | null = null;
function ensureMermaid() {
  if (!mermaidInit) {
    mermaidInit = import("mermaid").then((mod) => {
      mod.default.initialize({
        startOnLoad: false,
        securityLevel: "strict",
        theme: "dark",
        fontFamily: "inherit",
        flowchart: { curve: "basis", useMaxWidth: true },
        sequence: { useMaxWidth: true },
      });
      return mod.default;
    });
  }
  return mermaidInit;
}

export function MermaidBlock({ code }: { code: string }) {
  const [svg, setSvg] = useState<string>("");
  const [error, setError] = useState("");
  const idRef = useRef(`mmd-${Math.random().toString(36).slice(2, 9)}`);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const mermaid = await ensureMermaid();
        const { svg: out } = await mermaid.render(idRef.current, code);
        if (alive) { setSvg(out); setError(""); }
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => { alive = false; };
  }, [code]);

  if (error) {
    return (
      <div className="md-mermaid-fallback">
        <div className="md-mermaid-err">⚠ Mermaid 渲染失败（{error.slice(0, 120)}），源码如下：</div>
        <pre>{code}</pre>
      </div>
    );
  }
  return (
    <div className="md-rich md-mermaid-wrap">
      {svg
        ? <div className="md-mermaid-svg" dangerouslySetInnerHTML={{ __html: svg }} />
        : <div className="rich-card-loading">🎨 图形生成中…</div>}
    </div>
  );
}
