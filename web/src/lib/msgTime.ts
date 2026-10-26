// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

/**
 * 气泡时间戳格式化。
 *
 * 输入是后端 Message.ts（Unix 毫秒）。规则：
 *   - 今天：`14:32:07`（只到秒，不带日期，避免长会话里每行都赘着年月日）
 *   - 昨天及更早：`10-24 23:58:41`（跨天必须带日期，否则分不清是哪天说的）
 *
 * 长度恒定（8 或 14 字符），配合 CSS 的 tabular-nums 不会让气泡宽度抖动。
 *
 * 为什么不复用 dayjs/toLocaleTimeString：需要在**固定长度**下既做
 * 「今天/跨天」判定又保证补零一致，手写比 locale 变体更可控（不同浏览器/
 * 系统语言下 toLocaleTimeString 可能给出 12 小时制或不同分隔符）。
 */

const pad = (n: number): string => String(n).padStart(2, "0");

/** ts(ms) → 展示文案；ts 缺失/非法 → 空串（调用方据此不渲染时间） */
export function formatMsgTime(ts?: number | null, now: Date = new Date()): string {
  if (typeof ts !== "number" || !Number.isFinite(ts) || ts <= 0) return "";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "";
  const hms = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  if (sameDay) return hms;
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hms}`;
}

/** 完整日期时间（悬停 tooltip 用）：`2026-10-24 23:58:41` */
export function formatMsgTimeFull(ts?: number | null): string {
  if (typeof ts !== "number" || !Number.isFinite(ts) || ts <= 0) return "";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "";
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
    + `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
