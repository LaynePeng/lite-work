// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import ToolPanel from "./ToolPanel";
import type { ContextHistoryPoint, ContextStats } from "../types";

// 单轮 12,000 prompt、30 轮任务：累计 360,017 ≠ 当前上下文 12,000
const STATS: ContextStats = {
  model: "deepseek-flash",
  context_window: 1_000_000,
  pricing: { input_per_mtok: 0.15, output_per_mtok: 0.6, cache_hit_per_mtok: 0.003 },
  task: {
    prompt_tokens: 360_017,
    output_tokens: 1_500,
    cache_hit_tokens: 324_000,
    cache_miss_tokens: 36_017,
    cache_hit_rate: 0.9,
    compression_count: 0,
    compressed_tokens: 0,
    usage_ratio: 0.012,
    last_prompt_tokens: 12_000,
    turns: 12,
    tool_calls: 29,
    blocked: 0,
    cost_estimate: 0.0225,
    last: {
      prompt_tokens: 12_000,
      output_tokens: 50,
      cache_hit_tokens: 10_800,
      cache_miss_tokens: 1_200,
      cost_estimate: 0.0003,
    },
  },
  session: {
    prompt_tokens: 1_800_085,
    output_tokens: 7_500,
    cache_hit_tokens: 1_620_000,
    cache_miss_tokens: 180_085,
    cache_hit_rate: 0.9,
    compression_count: 0,
    compressed_tokens: 0,
    tool_calls: 145,
    blocked: 0,
    cost_estimate: 0.1125,
  },
};

const HISTORY: ContextHistoryPoint[] = Array.from({ length: 12 }, (_, i) => ({
  p: 3_000 + i * 900,
}));

/** 读取面板全文（含 SVG title 等不可见文本），便于断言信息存在与不重复出现。 */
function textOf(container: HTMLElement): string {
  return container.textContent ?? "";
}

/** 统计某段文本出现次数（用于断言同一行不重复上报）。 */
function countOf(container: HTMLElement, needle: string): number {
  return (container.textContent ?? "").split(needle).length - 1;
}

describe("ToolPanel · 上下文面板（仪表 + 账单 + 平铺明细）", () => {
  it("实时仪表：环形水位 + 当前上下文大数字 + 运行状态", () => {
    const { container } = render(
      <ToolPanel contextStats={STATS} contextHistory={HISTORY} running mcpServers={[]} tools={[]} todos={[]} />
    );
    // 环形仪表存在且显示百分比
    expect(container.querySelector(".ctx2-gauge svg")).toBeTruthy();
    expect(container.querySelector(".ctx2-gv b")?.textContent).toBe("1.2%");
    // 大数字与窗口刻度
    expect(textOf(container)).toContain("12,000");
    expect(textOf(container)).toContain("/ 1.00M");
    // 运行状态 chip（含轮数）
    expect(textOf(container)).toContain("任务进行中 · 第 12 轮");
    // 趋势图存在
    expect(container.querySelector(".ctx2-spark")).toBeTruthy();
  });

  it("账单卡：会话累计成本大字 + 本任务/本次调用 chip + 缓存命中率与省钱提示", () => {
    const { container } = render(
      <ToolPanel contextStats={STATS} mcpServers={[]} tools={[]} todos={[]} />
    );
    expect(container.querySelector(".ctx2-cost")?.textContent).toBe("$0.1125");
    expect(textOf(container)).toContain("本会话累计成本");
    expect(textOf(container)).toContain("$0.0225");   // 本任务
    expect(textOf(container)).toContain("$0.0003");   // 本次调用
    expect(textOf(container)).toContain("90.0%");     // 缓存命中率
    expect(textOf(container)).toContain("缓存帮你省下");
    expect(textOf(container)).toContain("≈ $0.0476"); // 324k × (0.15-0.003)/1M
  });

  it("平铺明细（B 式 kv，不做折叠）：本任务 / 会话累计 / 计费单价", () => {
    const { container } = render(
      <ToolPanel contextStats={STATS} mcpServers={[]} tools={[]} todos={[]} />
    );
    // 不允许出现折叠交互
    expect(container.querySelector("details")).toBeNull();
    expect(textOf(container)).toContain("本任务累计");
    expect(textOf(container)).toContain("会话累计");
    expect(textOf(container)).toContain("360,017 / 1,500");
    expect(textOf(container)).toContain("1,800,085 / 7,500");
    expect(textOf(container)).toContain("工具调用 / 安全拦截");
    // 计费单价
    expect(textOf(container)).toContain("$0.15 / $0.6 / $0.003");
  });

  it("空闲态：无任务时状态 chip 显示「空闲」且趋势图隐藏（无历史）", () => {
    const { container } = render(
      <ToolPanel contextStats={STATS} mcpServers={[]} tools={[]} todos={[]} />
    );
    expect(textOf(container)).toContain("空闲");
    expect(container.querySelector(".ctx2-spark")).toBeNull();
  });

  it("旧载荷（无 last / pricing / turns）仍可渲染，退化为累计值", () => {
    const legacy: ContextStats = {
      model: "deepseek-flash",
      context_window: 1_000_000,
      task: {
        prompt_tokens: 360_017,
        output_tokens: 1_500,
        cache_hit_tokens: 324_000,
        cache_miss_tokens: 36_017,
        cache_hit_rate: 0.9,
        compression_count: 0,
        compressed_tokens: 0,
        usage_ratio: 0.012,
        tool_calls: 29,
        blocked: 0,
        cost_estimate: 0.0225,
      },
      session: STATS.session,
    };
    const { container } = render(
      <ToolPanel contextStats={legacy} running mcpServers={[]} tools={[]} todos={[]} />
    );
    // last 缺失 → 本次调用退化为任务累计成本
    expect(countOf(container, "$0.0225")).toBeGreaterThanOrEqual(2);
    // 无 pricing → 不显示计费单价行
    expect(textOf(container)).not.toContain("计费单价");
    // 无 turns → chip 不带具体轮数（占位「…」）
    expect(textOf(container)).toContain("任务进行中 · 第 … 轮");
  });

  it("口径去重：同一数值不会以两个不同口径重复出现（本次调用 ≠ 任务累计）", () => {
    const { container } = render(
      <ToolPanel contextStats={STATS} mcpServers={[]} tools={[]} todos={[]} />
    );
    // 12,000（本次调用/水位）只应出现在仪表区；任务累计 360,017 只在明细区
    expect(countOf(container, "12,000")).toBe(1);
    expect(countOf(container, "360,017")).toBe(1);
    expect(countOf(container, "$0.1125")).toBe(1);
  });
});

// ---------------------------------------------------------------- TODOs 面板

const TODOS = [
  { content: "已完成的第一步", status: "completed" as const, updated_at: 1_700_000_000 },
  { content: "正在做的第二步", status: "in_progress" as const, updated_at: 1_700_000_500 },
  { content: "还没做的第三步", status: "pending" as const },
  { content: "还没做的第四步", status: "pending" as const },
];

describe("ToolPanel · TODOs 面板（进度条 + 平铺排序）", () => {
  it("进度条与百分比；进行中置顶、完成沉底（组内保持提交顺序）", () => {
    const { container } = render(
      <ToolPanel contextStats={null} mcpServers={[]} tools={[]} todos={TODOS} activeTab="todos" />
    );
    expect(container.querySelector(".ctx2-meter")).toBeTruthy();
    expect(container.querySelector(".todo2-title")?.textContent).toContain("任务进度 1/4");
    expect(container.querySelector(".todo2-title")?.textContent).toContain("进行中 1");
    const order = Array.from(container.querySelectorAll(".todos-list .todo-item"))
      .map((el) => el.querySelector(".todo-content")?.textContent);
    // 平铺但有序：进行中 → 待办 → 完成
    expect(order).toEqual([
      "正在做的第二步", "还没做的第三步", "还没做的第四步", "已完成的第一步",
    ]);
  });

  it("悬停元信息：显示更新时间；完成项划线弱化", () => {
    const { container } = render(
      <ToolPanel contextStats={null} mcpServers={[]} tools={[]} todos={TODOS} activeTab="todos" />
    );
    const item = Array.from(container.querySelectorAll(".todo-item"))
      .find((el) => el.textContent?.includes("正在做的第二步"));
    expect(item?.getAttribute("title")).toContain("更新于");
    expect(item?.className).toContain("todo-in_progress");
    expect(item?.querySelector(".todo-spin")).toBeTruthy();   // 进行中呼吸动画
    const done = Array.from(container.querySelectorAll(".todo-item"))
      .find((el) => el.textContent?.includes("已完成的第一步"));
    expect(done?.className).toContain("todo-completed");
  });

  it("全部完成：收尾态（✅ 全部完成 + 100%）", () => {
    const { container } = render(
      <ToolPanel
        contextStats={null} mcpServers={[]} tools={[]} activeTab="todos"
        todos={[
          { content: "a", status: "completed" },
          { content: "b", status: "completed" },
        ]}
      />
    );
    expect(container.querySelector(".todo2-title")?.textContent).toContain("✅ 全部完成");
    expect(container.querySelector(".todo2-alldone")).toBeTruthy();
    expect(container.querySelector(".ctx2-meter > i")?.getAttribute("style")).toContain("100%");
  });
});
