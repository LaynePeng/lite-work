// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 富内容块导出（```chart / ```table）：
//   - 图表 → PNG（SVG → canvas，4 倍超采样，宽裕留白）；
//   - 图表 → 自足 HTML（皮肤内联 + 导出工具条 + <details> 数据表，可独立双击打开）；
//   - 表格 → CSV（RFC 4180 转义，含 BOM，Excel 直接认 UTF-8）。
//
// 关键约束（踩坑记录）：
//   1. **不要把 React 节点 serialize 成字符串**再 rawify——React 文本节点转义、
//      注释节点会让 SVG 过期（曾用 innerHTML 方案，遇到信息丢失/丢字）。
//      一律走 XMLSerializer + CSSOM 内联，纯 DOM 操作。
//   2. SVG 内的 `fill="var(--text-2)"` 在离开页面上下文后无法解析 → 导出前把
//      变量按当前主题取值替换为字面量颜色，否则读到的图是黑/透明。
//   3. canvas 需要显式尺寸：viewBox 的 width/height 在部分实现里缺省为 0。

/** 规整文件名：去掉路径分隔符与非法字符，供「另存为」默认名与工作区内落盘使用。
 *  首尾的点与空白一并剥掉——否则 `../..` 这类输入会留下 `..` 片段。 */
export function safeFileName(name: string, fallback = "export"): string {
  const cleaned = (name ?? "")
    .replace(/[\\/:*?"<>|\u0000-\u001f]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/^[.\s]+|[.\s]+$/g, "")
    .trim();
  return (cleaned || fallback).slice(0, 80);
}

/** 去掉扩展名（导出时好按目标格式补后缀）。 */
export function stripExt(name: string): string {
  const i = name.lastIndexOf(".");
  return i > 0 ? name.slice(0, i) : name;
}

/** 从富块标题推导文件名（无标题时用兜底名）。 */
export function exportBaseName(title: string | undefined, fallback: string): string {
  return safeFileName(stripExt(title ?? "") || fallback, fallback).slice(0, 60);
}

// ---------------------------------------------------------------- 主题内联

const CSS_VAR_RE = /var\(\s*(--[\w-]+)\s*(?:,\s*([^)]*))?\)/g;

/** 把 SVG 属性/样式里引用的 CSS 变量替换为当前主题下的字面量。
 *  读取根元素的计算值（`:root` 上的 --text-2 等），保证导出图与屏幕一致。 */
function inlineSvgVars(elm: Element, root: Element): void {
  const styles = getComputedStyle(root);
  const resolve = (raw: string): string => {
    if (!raw.includes("var(")) return raw;
    return raw.replace(CSS_VAR_RE, (_m, name: string, fallback?: string) => {
      const v = styles.getPropertyValue(name).trim();
      if (v) return v;
      return (fallback ?? "").trim();
    });
  };
  for (const attr of ["fill", "stroke", "stop-color"]) {
    const v = elm.getAttribute(attr);
    if (v) elm.setAttribute(attr, resolve(v));
  }
  const style = elm.getAttribute("style");
  if (style && style.includes("var(")) elm.setAttribute("style", resolve(style));
  for (const child of Array.from(elm.children)) inlineSvgVars(child, root);
}

// ---------------------------------------------------------------- PNG

/** 把一段独立 SVG（自足：不含外部依赖）编码为 data URL。 */
function svgToDataUrl(svg: string): string {
  // encodeURIComponent 而非 btoa：SVG 含中文标签，btoa 会抛 InvalidCharacterError
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

/**
 * 导出某个富块容器内的 SVG 为 PNG blob。
 * @param wrap 包含 `<svg>` 的容器元素（导出按钮所在富块）
 * @param scale 超采样倍率（默认 4，视网膜清晰；上限 6 防超大画布）
 */
export async function svgToPngBlob(wrap: HTMLElement, scale = 4): Promise<Blob> {
  const svg = wrap.querySelector("svg");
  if (!svg) throw new Error("未找到可导出的图形");
  const box = svg.getBoundingClientRect();
  const vb = svg.getAttribute("viewBox")?.split(/[\s,]+/).map(Number);
  // 优先 viewBox（逻辑坐标系），退回渲染尺寸
  const w = vb && vb.length === 4 && vb[2] > 0 ? vb[2] : Math.max(1, box.width);
  const h = vb && vb.length === 4 && vb[3] > 0 ? vb[3] : Math.max(1, box.height);
  if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0) {
    throw new Error("图形尺寸无效");
  }

  const clone = svg.cloneNode(true) as SVGElement;
  inlineSvgVars(clone, document.documentElement);
  clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  clone.setAttribute("xmlns:xlink", "http://www.w3.org/1999/xlink");
  clone.setAttribute("width", String(w));
  clone.setAttribute("height", String(h));
  // 落成自足文档：独立解析时也保留字体与背景
  const source = new XMLSerializer().serializeToString(clone);
  const svgDoc = `<?xml version="1.0" encoding="UTF-8"?>\n${source}`;

  const factor = Math.max(1, Math.min(6, scale));
  const cw = Math.round(w * factor);
  const ch = Math.round(h * factor);
  if (cw * ch > 40_000_000) throw new Error("图形过大，无法导出");

  const img = new Image();
  img.decoding = "sync";
  const loaded = new Promise<void>((resolve, reject) => {
    img.onload = () => resolve();
    img.onerror = () => reject(new Error("图形解码失败"));
  });
  img.src = svgToDataUrl(svgDoc);
  await loaded;

  const canvas = document.createElement("canvas");
  canvas.width = cw;
  canvas.height = ch;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("画布不可用");
  const bg = getComputedStyle(document.documentElement).getPropertyValue("--bg-1").trim() || "#161b22";
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, cw, ch);
  ctx.drawImage(img, 0, 0, cw, ch);

  const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/png"));
  if (!blob) throw new Error("PNG 编码失败");
  return blob;
}

// ---------------------------------------------------------------- 自足 HTML

const HTML_ESCAPE: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" };
/** HTML 文本转义（表头/单元格/标题都要过一遍，防注入与破版）。 */
export function escapeHtml(s: string): string {
  return String(s ?? "").replace(/[&<>"]/g, (c) => HTML_ESCAPE[c] ?? c);
}

/** 读取当前主题的关键色（导出 HTML 内联用，保证脱离应用后视觉一致）。 */
function themeVars(): Record<string, string> {
  const cs = getComputedStyle(document.documentElement);
  const names = [
    "--bg-0", "--bg-1", "--bg-2", "--bg-3", "--border",
    "--text-0", "--text-1", "--text-2", "--accent", "--green", "--red", "--yellow",
  ];
  const out: Record<string, string> = {};
  for (const n of names) {
    const v = cs.getPropertyValue(n).trim();
    if (v) out[n] = v;
  }
  return out;
}

export interface ExportTable {
  columns: string[];
  rows: string[][];
  /** 数值列下标（右对齐），缺省空。 */
  numeric?: number[];
}

/**
 * 生成自足的导出 HTML：内联主题变量 + 图形 SVG + 可折叠数据表 + 导出工具条。
 * 双击即可在浏览器打开；不需要任何外部资源。
 */
export function buildExportHtml(opts: {
  title?: string;
  wrap: HTMLElement;
  table?: ExportTable;
  filename: string;
  svgFor?: (svg: SVGElement, svgString: string) => string;
}): string {
  const vars = themeVars();
  const cssVars = Object.entries(vars).map(([k, v]) => `  ${k}: ${v};`).join("\n");
  const svg = opts.wrap.querySelector("svg");
  if (!svg) throw new Error("未找到可导出的图形");
  const clone = svg.cloneNode(true) as SVGElement;
  inlineSvgVars(clone, document.documentElement);
  clone.removeAttribute("class");
  clone.setAttribute("width", "100%");
  clone.setAttribute("height", "auto");
  clone.setAttribute("preserveAspectRatio", "xMidYMid meet");
  let svgString = new XMLSerializer().serializeToString(clone);
  if (opts.svgFor) svgString = opts.svgFor(clone, svgString);

  const title = escapeHtml(opts.title || "图表");
  const table = opts.table;
  const tableHtml = table
    ? `<details${table.rows.length <= 12 ? " open" : ""}>\n<summary>数据表（${table.rows.length} 行）</summary>\n` +
      `<table>\n<thead><tr>${table.columns
        .map((c) => `<th>${escapeHtml(c)}</th>`)
        .join("")}</tr></thead>\n<tbody>\n${table.rows
        .map(
          (r) =>
            `<tr>${table.columns
              .map((_, ci) => {
                const isNum = table.numeric?.includes(ci) ?? false;
                return `<td${isNum ? ' class="num"' : ""}>${escapeHtml(r[ci] ?? "")}</td>`;
              })
              .join("")}</tr>`,
        )
        .join("\n")}\n</tbody>\n</table>\n</details>`
    : "";

  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${title}</title>
<style>
:root {
${cssVars}
  --font-sans: -apple-system, "PingFang SC", "Segoe UI", "Microsoft YaHei", sans-serif;
  --font-mono: "SF Mono", "JetBrains Mono", "Fira Code", Consolas, monospace;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 28px 24px 44px;
  background: var(--bg-0, #0d1117);
  color: var(--text-0, #e6edf3);
  font-family: var(--font-sans);
  font-size: 14px; line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}
figure { margin: 0 auto; max-width: 720px; }
figcaption {
  font-size: 16px; font-weight: 600; margin-bottom: 14px;
  color: var(--text-0, #e6edf3);
}
.chart-card {
  border: 1px solid var(--border, #262e3f);
  border-radius: 12px;
  background: var(--bg-1, #161b22);
  padding: 16px 18px;
}
svg { display: block; width: 100%; height: auto; }
svg text { font-family: var(--font-sans); }
details {
  margin-top: 18px; border: 1px solid var(--border, #262e3f);
  border-radius: 10px; background: var(--bg-1, #161b22); overflow: hidden;
}
summary {
  cursor: pointer; padding: 9px 14px; font-size: 12.5px;
  color: var(--text-1, #a8b3c0); user-select: none;
}
summary:hover { background: var(--bg-2, #1c2330); }
table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
th {
  text-align: left; font-size: 11.5px; font-weight: 500;
  color: var(--text-2, #6e7a8c); padding: 8px 14px;
  border-bottom: 1px solid var(--border, #262e3f);
}
td {
  padding: 7px 14px; color: var(--text-1, #a8b3c0);
  border-bottom: 1px solid var(--bg-2, #1c2330);
}
tr:last-child td { border-bottom: 0; }
td.num {
  text-align: right; color: var(--text-0, #e6edf3);
  font-weight: 600; font-variant-numeric: tabular-nums;
  font-family: var(--font-mono);
}
.toolbar {
  display: flex; gap: 8px; justify-content: flex-end;
  max-width: 720px; margin: 2px auto 14px;
}
.toolbar button, .toolbar a {
  font: inherit; font-size: 12.5px; padding: 6px 14px; cursor: pointer;
  border: 1px solid var(--border, #262e3f); border-radius: 8px;
  background: var(--bg-2, #1c2330); color: var(--text-1, #a8b3c0);
  text-decoration: none;
}
.toolbar button:hover, .toolbar a:hover { background: var(--bg-3, #21283a); color: var(--text-0, #e6edf3); }
@media print {
  body { background: #fff; color: #000; padding: 0; }
  .toolbar { display: none; }
  .chart-card { border-color: #ddd; background: #fff; }
  details { display: none; }
}
</style>
</head>
<body>
<div class="toolbar">
  <button id="dl-png" type="button">下载 PNG</button>
  <button id="dl-svg" type="button">下载 SVG</button>
  <button type="button" onclick="window.print()">打印 / 存 PDF</button>
</div>
<figure>
  <figcaption>${title}</figcaption>
  <div class="chart-card" id="chart-host">
${svgString}
  </div>
${tableHtml}
</figure>
<script>
(function () {
  var name = ${JSON.stringify(safeFileName(stripExt(opts.filename) || "chart", "chart"))};
  function save(blob, filename) {
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }
  var svgEl = document.querySelector("#chart-host svg");
  document.getElementById("dl-svg").addEventListener("click", function () {
    var clone = svgEl.cloneNode(true);
    clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    var s = '<?xml version="1.0" encoding="UTF-8"?>\\n' + new XMLSerializer().serializeToString(clone);
    save(new Blob([s], { type: "image/svg+xml" }), name + ".svg");
  });
  document.getElementById("dl-png").addEventListener("click", function () {
    var vb = (svgEl.getAttribute("viewBox") || "0 0 640 300").split(/[\\s,]+/).map(Number);
    var w = vb[2] || 640, h = vb[3] || 300, k = 3;
    var clone = svgEl.cloneNode(true);
    clone.setAttribute("width", String(w)); clone.setAttribute("height", String(h));
    var s = '<?xml version="1.0" encoding="UTF-8"?>\\n' + new XMLSerializer().serializeToString(clone);
    var img = new Image();
    img.onload = function () {
      var c = document.createElement("canvas");
      c.width = w * k; c.height = h * k;
      var ctx = c.getContext("2d");
      var css = getComputedStyle(document.body);
      ctx.fillStyle = css.getPropertyValue("--bg-1").trim() || "#161b22";
      ctx.fillRect(0, 0, c.width, c.height);
      ctx.drawImage(img, 0, 0, c.width, c.height);
      c.toBlob(function (b) { if (b) save(b, name + ".png"); }, "image/png");
    };
    img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(s);
  });
})();
</script>
</body>
</html>`;
}

// ---------------------------------------------------------------- CSV

/** Blob → base64（走 ArrayBuffer，不经字符串，二进制安全）。 */
export async function blobToBase64(blob: Blob): Promise<string> {
  const buf = new Uint8Array(await blob.arrayBuffer());
  let bin = "";
  const CHUNK = 0x8000; // 分块防 apply 参数上限
  for (let i = 0; i < buf.length; i += CHUNK) {
    bin += String.fromCharCode.apply(null, Array.from(buf.subarray(i, i + CHUNK)));
  }
  return btoa(bin);
}

export function textToBlob(text: string, type = "text/plain;charset=utf-8"): Blob {
  return new Blob([text], { type });
}

/** 浏览器下载（无 Electron 桥时的降级路径）。 */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** RFC 4180 字段转义：含逗号/引号/换行时用双引号包裹，内部引号翻倍。 */
export function csvField(v: string): string {
  const s = String(v ?? "");
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/** 表格 → CSV 文本（前置 BOM，Excel 打开中文不乱码）。 */
export function buildCsv(spec: ExportTable): string {
  const lines = [
    spec.columns.map(csvField).join(","),
    ...spec.rows.map((r) => spec.columns.map((_, ci) => csvField(r[ci] ?? "")).join(",")),
  ];
  return `\uFEFF${lines.join("\r\n")}\r\n`;
}

export function buildTsv(spec: ExportTable): string {
  const cell = (v: string) => String(v ?? "").replace(/[\t\r\n]/g, " ");
  return [
    spec.columns.map(cell).join("\t"),
    ...spec.rows.map((r) => spec.columns.map((_, ci) => cell(r[ci] ?? "")).join("\t")),
  ].join("\n");
}
