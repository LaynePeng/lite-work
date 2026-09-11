// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import ToolPanel from "./ToolPanel";
import type { ContextStats } from "../types";

// 单轮 12,000 prompt、跑了 30 轮的任务：累计 360,017 ≠ 当前上下文 12,000
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
    prompt_tokens: 360_017,
    output_tokens: 1_500,
    cache_hit_tokens: 324_000,
    cache_miss_tokens: 36_017,
    cache_hit_rate: 0.9,
    compression_count: 0,
    compressed_tokens: 0,
    tool_calls: 29,
    blocked: 0,
    cost_estimate: 0.0225,
  },
};

/** 取某个分区标题之后、下一个标题之前的所有行文本。 */
function rowsOf(container: HTMLElement, label: string): string {
  const title = Array.from(container.querySelectorAll(".ctx-section-label"))
    .find((el) => el.textContent === label);
  if (!title) throw new Error(`未找到分区：${label}`);
  const out: string[] = [];
  let node: Element | null = title.nextElementSibling;
  while (node && !node.classList.contains("ctx-section-label")) {
    out.push(node.textContent ?? "");
    node = node.nextElementSibling;
  }
  return out.join(" | ");
}

describe("ToolPanel · 上下文面板口径（本次调用 vs 任务累计）", () => {
  it("「本次调用」展示单次用量，累计值单独分区（避免误读成当前上下文）", () => {
    const { container } = render(
      <ToolPanel contextStats={STATS} mcpServers={[]} tools={[]} todos={[]} />
    );

    // 顶部进度条 = 当前上下文水位（最近一次调用的 prompt），不是累计值
    expect(container.textContent).toContain("当前上下文 12,000 · 1.2%");

    const call = rowsOf(container, "本次调用（模型准确返回）");
    expect(call).toContain("12,000");
    expect(call).toContain("$0.0003");
    expect(call).not.toContain("360,017");

    const total = rowsOf(container, "本任务累计（每轮重发上下文）");
    expect(total).toContain("360,017");
    expect(total).toContain("$0.0225");
  });

  it("展示计费单价（成本依据，便于与供应商账单对账）", () => {
    const { container } = render(
      <ToolPanel contextStats={STATS} mcpServers={[]} tools={[]} todos={[]} />
    );
    const rows = rowsOf(container, "计费单价（每 M tokens）");
    expect(rows).toContain("$0.15 / $0.6 / $0.003");
  });

  it("旧载荷（无 last / pricing 字段）仍可渲染，退化为累计值", () => {
    const legacy: ContextStats = {
      model: "deepseek-flash",
      context_window: 1_000_000,
      // 旧载荷：没有 last / last_prompt_tokens / pricing
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
      <ToolPanel contextStats={legacy} mcpServers={[]} tools={[]} todos={[]} />
    );
    expect(rowsOf(container, "本次调用（模型准确返回）")).toContain("360,017");
  });
});
