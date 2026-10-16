// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 富块渲染层响应式（compact 主题）：设计稿见 docs/rich-blocks-compact-design.md。
// 核心约定：模型输出的 spec 是纯数据、一份不变；desktop ("d") 参数集与
// 改造前逐值一致（P3 验收线），compact ("m") 是小屏专属画法——W=300 让
// 手机上近乎 1:1 渲染，字号即物理字号（不再被 CSS 等比缩小 53%）。

export interface ChartDims {
  W: number;
  padL: number;
  padR: number;
  padT: number;
  padB: number;
  plotH: number;
}

export interface ChartFonts {
  /** 轴刻度 */
  axis: number;
  /** x 轴类目 / 图例 */
  label: number;
  /** 数据点标注 */
  val: number;
}

/** desktop / compact 二选一的键（组件里 `BAR[compact ? "m" : "d"]`）。 */
export type ModeKey = "d" | "m";

// ---------------------------------------------------------------- 尺寸

/** 柱状 / 折线 / 直方图 / 瀑布 / 堆叠类共用画布。 */
export const PLOT: Record<ModeKey, ChartDims> = {
  d: { W: 640, padL: 50, padR: 14, padT: 16, plotH: 230, padB: 44 },
  m: { W: 300, padL: 34, padR: 10, padT: 14, plotH: 170, padB: 34 },
};

/** 横向柱状图（类目标签占左栏）。 */
export const HBAR: Record<ModeKey, ChartDims> = {
  d: { W: 640, padL: 128, padR: 64, padT: 8, padB: 12, plotH: 230 },
  m: { W: 300, padL: 80, padR: 34, padT: 8, padB: 12, plotH: 170 },
};

/** 双轴组合图（右轴标注占右栏）。 */
export const COMBO: Record<ModeKey, ChartDims> = {
  d: { W: 640, padL: 52, padR: 56, padT: 16, plotH: 230, padB: 44 },
  m: { W: 300, padL: 36, padR: 34, padT: 14, plotH: 170, padB: 34 },
};

/** 散点 / 箱线（底部留备注/图例）。 */
export const SCATTER: Record<ModeKey, ChartDims> = {
  d: { W: 640, padL: 54, padR: 16, padT: 18, plotH: 240, padB: 44 },
  m: { W: 300, padL: 36, padR: 10, padT: 14, plotH: 180, padB: 36 },
};

// ---------------------------------------------------------------- 字号 / 密度

export const FONTS: Record<ModeKey, ChartFonts> = {
  d: { axis: 10, label: 10.5, val: 9.5 },
  m: { axis: 10.5, label: 10.5, val: 10 },
};

/** x 轴类目标签截断长度（trunc 的第二参）。 */
export const LABEL_TRUNC: Record<ModeKey, number> = { d: 10, m: 5 };

/** x 轴隔标签：类目超过该数时隔一个显示一个（紧凑屏防重叠）。 */
export const LABEL_SKIP: Record<ModeKey, number> = { d: Infinity, m: 6 };

/** y 网格线数（YGrid 的 0..n 分段）。 */
export const GRID_LINES: Record<ModeKey, number> = { d: 4, m: 3 };

/** 数据点标注开关阈值：单系列且类目数 ≤ 该值时标数值。 */
export const SHOW_VALS_MAX: Record<ModeKey, number> = { d: 10, m: 6 };

/** 饼图几何（viewBox 260×260 → 220×220）。 */
export const PIE: Record<ModeKey, { size: number; R: number; IR: number; total: number }> = {
  d: { size: 260, R: 92, IR: 58, total: 13 },
  m: { size: 220, R: 80, IR: 50, total: 12 },
};

/** 雷达图几何。 */
export const RADAR: Record<ModeKey, { size: number; R: number; trunc: number; vertex: number }> = {
  d: { size: 300, R: 105, trunc: 8, vertex: 2.6 },
  m: { size: 270, R: 86, trunc: 5, vertex: 3.2 },
};

/** 折线/散点数据点半径（触控命中）。 */
export const POINT_R: Record<ModeKey, number> = { d: 2.6, m: 3.4 };

/** 柱宽上限。 */
export const BAR_W_MAX: Record<ModeKey, number> = { d: 30, m: 24 };
