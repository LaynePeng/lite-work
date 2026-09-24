// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import { render, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ToolPanel from "./ToolPanel";
import { api } from "../api";
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
    expect(textOf(container)).toContain("90.0%");     // 缓存命中率（会话累计）
    expect(textOf(container)).toContain("缓存帮你省下（会话累计）");
    expect(textOf(container)).toContain("≈ $0.2381"); // 1.62M × (0.15-0.003)/1M（会话累计口径）
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

  it("空闲时段：顶部单价显示空闲档（半价）而非高峰价", () => {
    const offpeak: ContextStats = {
      ...STATS,
      pricing: {
        input_per_mtok: 0.3, output_per_mtok: 1.2, cache_hit_per_mtok: 0.006,
        off_peak: { input_per_mtok: 0.15, output_per_mtok: 0.6, cache_hit_per_mtok: 0.003 },
        off_peak_active: true,
      },
    };
    const { container } = render(
      <ToolPanel contextStats={offpeak} mcpServers={[]} tools={[]} todos={[]} />
    );
    const price = container.querySelector(".ctx2-price")?.textContent ?? "";
    expect(price).toContain("空闲·半价");
    // 顶部「已生效单价」= 空闲档，不再显示高峰 $0.3/$1.2
    expect(price).toContain("$0.15/$0.6");
    expect(price).not.toContain("$0.3/$1.2");
    // 明细区仍给出两档 + 当前生效档
    expect(textOf(container)).toContain("高峰全价");
    expect(textOf(container)).toContain("空闲半价");
    expect(textOf(container)).toContain("当前生效档");
    // 两档标签已精简：不再带「（输入 / 输出 / 缓存命中）」括号说明（列宽有限，值是同一行的三段数）
    expect(textOf(container)).not.toContain("（输入 / 输出 / 缓存命中）");
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

  it("点击环形仪表触发 onCompact 回调", () => {
    const onCompact = vi.fn();
    const { container } = render(
      <ToolPanel contextStats={STATS} mcpServers={[]} tools={[]} todos={[]}
        onCompact={onCompact} />
    );
    const btn = container.querySelector(".ctx2-gauge") as HTMLButtonElement;
    expect(btn).toBeTruthy();
    btn.click();
    expect(onCompact).toHaveBeenCalledTimes(1);
  });

  it("compacting 状态禁用按钮并显示压缩中…", () => {
    const { container } = render(
      <ToolPanel contextStats={STATS} mcpServers={[]} tools={[]} todos={[]}
        compacting onCompact={() => {}} />
    );
    const btn = container.querySelector(".ctx2-gauge") as HTMLButtonElement;
    expect(btn?.disabled).toBe(true);
    expect(textOf(container)).toContain("压缩中…");
  });
});

// ---------------------------------------------------------------- TODOs 面板

const TODOS = [
  { content: "已完成的第一步", status: "completed" as const, updated_at: 1_700_000_000 },
  { content: "正在做的第二步", status: "in_progress" as const, updated_at: 1_700_000_500 },
  { content: "还没做的第三步", status: "pending" as const },
  { content: "还没做的第四步", status: "pending" as const },
];

describe("ToolPanel · TODOs 面板（竖排脊线看板）", () => {
  it("进度条与百分比；三泳道竖排（进行中 → 待办 → 已完成），组内保持提交顺序", () => {
    const { container } = render(
      <ToolPanel contextStats={null} mcpServers={[]} tools={[]} todos={TODOS} activeTab="todos" />
    );
    expect(container.querySelector(".ctx2-meter")).toBeTruthy();
    expect(container.querySelector(".todo2-title")?.textContent).toContain("任务进度 1/4");
    expect(container.querySelector(".todo2-title")?.textContent).toContain("进行中 1");
    const lanes = Array.from(container.querySelectorAll(".todo-lane"));
    expect(lanes.map((el) => el.className)).toEqual([
      "todo-lane lane-in_progress", "todo-lane lane-pending", "todo-lane lane-completed",
    ]);
    // 计数徽章：1 / 2 / 1
    const counts = lanes.map((el) => el.querySelector(".todo-lane-count")?.textContent);
    expect(counts).toEqual(["1", "2", "1"]);
    // 组内卡片保持提交顺序
    const cardsOf = (lane: Element) =>
      Array.from(lane.querySelectorAll(".todo-content")).map((el) => el.textContent);
    expect(cardsOf(lanes[0])).toEqual(["正在做的第二步"]);
    expect(cardsOf(lanes[1])).toEqual(["还没做的第三步", "还没做的第四步"]);
    expect(cardsOf(lanes[2])).toEqual(["已完成的第一步"]);
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

// ---------------------------------------------------------------- Agents 看板

import { fireEvent } from "@testing-library/react";
import type { SubAgentProgress } from "../types";

const RUNNING_AGENT: SubAgentProgress = {
  subagentId: "sa_cancel1",
  role: "explorer",
  task: "调研竞品",
  turn: 2,
  steps: [
    { tool: "webfetch", status: "done", durationMs: 812 },
    { tool: "search_code", status: "running" },
  ],
  status: "running",
  startedAt: Date.now() - 45_000,
  streaming_text: "正在分析搜索结果…实时输出内容 ABC123",
  mode: "orchestrate",
};

const DONE_AGENT: SubAgentProgress = {
  subagentId: "sa_done1",
  role: "critic",
  task: "审查方案",
  turn: 3,
  steps: [],
  status: "done",
  summary: "方案可行",
  mode: "orchestrate",
};

describe("ToolPanel · Agents 看板（手动取消 + 实时输出详情）", () => {
  it("运行中卡片显示 ✕ 取消按钮，完成后不显示", () => {
    const onCloseAgent = vi.fn();
    const { container } = render(
      <ToolPanel contextStats={null} mcpServers={[]} tools={[]} todos={[]}
        activeTab="agents" agentBoard={[RUNNING_AGENT, DONE_AGENT]}
        onCloseAgent={onCloseAgent} />
    );
    const btns = container.querySelectorAll(".agent-cancel-btn");
    expect(btns.length).toBe(1);  // 仅 running 卡片有
  });

  it("点击 ✕ 确认后调用 onCloseAgent（agentId 正确）", () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const onCloseAgent = vi.fn();
    const { container } = render(
      <ToolPanel contextStats={null} mcpServers={[]} tools={[]} todos={[]}
        activeTab="agents" agentBoard={[RUNNING_AGENT]}
        onCloseAgent={onCloseAgent} />
    );
    fireEvent.click(container.querySelector(".agent-cancel-btn")!);
    expect(confirmSpy).toHaveBeenCalled();
    expect(onCloseAgent).toHaveBeenCalledWith("sa_cancel1");
    confirmSpy.mockRestore();
  });

  it("确认框点「取消」不触发关闭", () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    const onCloseAgent = vi.fn();
    const { container } = render(
      <ToolPanel contextStats={null} mcpServers={[]} tools={[]} todos={[]}
        activeTab="agents" agentBoard={[RUNNING_AGENT]}
        onCloseAgent={onCloseAgent} />
    );
    fireEvent.click(container.querySelector(".agent-cancel-btn")!);
    expect(onCloseAgent).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it("点击运行卡片展开实时输出：步骤历史 + 全量流式文本（截尾 100 字符→全文）", () => {
    const { container } = render(
      <ToolPanel contextStats={null} mcpServers={[]} tools={[]} todos={[]}
        activeTab="agents" agentBoard={[RUNNING_AGENT]} />
    );
    // 未展开：只有尾部摘要
    expect(container.querySelector(".agent-live-detail")).toBeNull();
    const live = container.querySelector(".agent-live")!;
    fireEvent.click(live);
    // 展开：详情区出现，步骤与全量文本可见
    const detail = container.querySelector(".agent-live-detail");
    expect(detail).toBeTruthy();
    expect(container.querySelectorAll(".agent-steps-row").length).toBe(2);
    expect(container.querySelector(".agent-steps-tool")?.textContent).toBe("webfetch");
    expect(container.querySelector(".agent-stream-full")?.textContent).toContain("ABC123");
    // 再点收起
    fireEvent.click(container.querySelector(".agent-live")!);
    expect(container.querySelector(".agent-live-detail")).toBeNull();
  });

  it("未传 onCloseAgent 时不渲染取消按钮（功能可关）", () => {
    const { container } = render(
      <ToolPanel contextStats={null} mcpServers={[]} tools={[]} todos={[]}
        activeTab="agents" agentBoard={[RUNNING_AGENT]} />
    );
    expect(container.querySelector(".agent-cancel-btn")).toBeNull();
  });

  it("页签顺序：MCP 排在「工具」之后（低频配置类入口靠后）", () => {
    const { container } = render(
      <ToolPanel contextStats={STATS} mcpServers={[]} tools={[]} todos={[]} />
    );
    const labels = Array.from(container.querySelectorAll(".panel-tab")).map((b) => b.textContent);
    expect(labels).toEqual(["上下文", "TODOs", "Agents", "后台", "工具", "MCP"]);
  });

  it("不装插件（api.plugins 返回空）时，右栏不出现任何插件 tab / 面板 / lite-tree", async () => {
    // 关键：即使 api.plugins 异步成功返回**空列表**，也不得追加插件 tab
    vi.spyOn(api, "plugins").mockResolvedValue({ plugins: [] } as never);
    const { container } = render(
      <ToolPanel contextStats={null} mcpServers={[]} tools={[]} todos={[]} />
    );
    // 等异步 effect 跑完
    await waitFor(() => expect(api.plugins).toHaveBeenCalled());
    const labels = Array.from(container.querySelectorAll(".panel-tab")).map((b) => b.textContent);
    expect(labels).toEqual(["上下文", "TODOs", "Agents", "后台", "工具", "MCP"]);
    // 也没有任何"插件面板"渲染容器 / 刷新按钮
    expect(container.querySelector(".plugin-panel-md")).toBeNull();
    vi.restoreAllMocks();
  });

  it("装了插件且面板返回 lite-tree 时，渲染判定卡片（新设计）", async () => {
    const plugin = {
      name: "jev", path: "plugins/jev", is_dir: true, tools: [],
      description: "判定层", version: "0.6.5", source: "local", kind: "tool",
      contributes: { panels: [{ id: "judgements", title: "判断" }] },
      status: { state: "running", reason: "" },
    } as never;
    const tree = {
      type: "lite-tree",
      status: { text: "运行中", tone: "ok", meta: ["jev-1.13", "阈值 0.60"], chips: ["工具", "门禁"] },
      trees: [{
        nodes: [
          { label: "工具执行前 delete_file", type: "trigger" },
          { label: "choice", type: "kind" },
          { label: "拦截", type: "verdict" },
          { label: "0.91", type: "confidence", value: 0.91, threshold: 0.6, bar: 9 },
          { label: "已采信", type: "accepted", pass: true },
          { label: "拦截", type: "action", action: "block" },
        ],
      }],
      total: 1,
    };
    vi.spyOn(api, "plugins").mockResolvedValue({ plugins: [plugin] } as never);
    vi.spyOn(api, "pluginPanel").mockResolvedValue({
      name: "jev", panel: "judgements", title: "判断",
      markdown: JSON.stringify(tree),
    } as never);
    const { container } = render(
      <ToolPanel contextStats={null} mcpServers={[]} tools={[]} todos={[]}
        activeTab="plugin:jev:judgements" />
    );
    await waitFor(() => expect(api.pluginPanel).toHaveBeenCalled());
    // 新设计：判定卡片出现（来源 + 结论徽章 + 链条 chips），刷新在头部同一行
    expect(container.querySelectorAll(".panel-tab").length).toBeGreaterThan(6);
    expect(container.textContent).toContain("delete_file");
    expect(container.textContent).toContain("拦截");
    expect(container.textContent).toContain("0.91");
    expect(container.textContent).toContain("0.60");
    expect(container.textContent).toContain("已采信");
    vi.restoreAllMocks();
  });
});

// ---------------------------------------------------------------- 本轮 token 速度

/** 带速度数据的载荷（task.last 为 usage 校准后的精确值）。 */
const SPEED_STATS: ContextStats = {
  ...STATS,
  task: {
    ...STATS.task!,
    last: {
      ...STATS.task!.last!,
      ttft_ms: 820,
      gen_ms: 1_130,
      e2e_ms: 1_950,
      tps_gen: 44.1,
      tps_e2e: 25.6,
      tps_estimated: false,
    },
    avg_tps: 36.4,
    speed_turns: 12,
  },
};

/** 带速度轨迹的历史点（趋势图数据源）。 */
const SPEED_HISTORY: ContextHistoryPoint[] = Array.from({ length: 12 }, (_, i) => ({
  p: 3_000 + i * 900,
  tps: 30 + (i % 5) * 3,
}));

describe("ToolPanel · 本轮 token 速度（变体 B 速度条 + 趋势图）", () => {
  it("本轮结束：显示精确速度 + 首字 / 本任务均，且不带「估算」标", () => {
    const { container } = render(
      <ToolPanel contextStats={SPEED_STATS} contextHistory={SPEED_HISTORY} mcpServers={[]} tools={[]} todos={[]} />
    );
    expect(container.querySelector(".ctx2-speedbig b")?.textContent).toBe("44.1");
    expect(textOf(container)).toContain("tok/s");
    expect(textOf(container)).toContain("0.82s");      // 首字
    expect(textOf(container)).toContain("36.4");       // 本任务平均
    expect(textOf(container)).not.toContain("估算");    // usage 校准后不再标估算
    expect(container.querySelector(".ctx2-esttag")).toBeNull();
    // 状态文案与端到端速度已移除：它们字数随状态变化，会挤动右侧趋势图宽度
    expect(textOf(container)).not.toContain("本轮生成速度");
    expect(textOf(container)).not.toContain("本轮生成中");
    expect(textOf(container)).not.toContain("端到端");
    // 趋势图在此状态存在（与下面「生成中」对照，两态都必须在）
    expect(container.querySelector(".ctx2-speedrow .ctx2-spark")).toBeTruthy();
  });

  it("生成中：实时估算值带「≈」与「估算」标，且布局与结束态一致（无状态文案）", () => {
    const { container } = render(
      <ToolPanel
        contextStats={SPEED_STATS}
        contextHistory={SPEED_HISTORY}
        running
        liveSpeed={{ est_tokens: 128, chars: 260, chunks: 9, ttft_ms: 700, gen_ms: 2_100, tps: 61.0 }}
        mcpServers={[]} tools={[]} todos={[]}
      />
    );
    expect(container.querySelector(".ctx2-speedbig b")?.textContent).toBe("≈ 61.0");
    expect(container.querySelector(".ctx2-speedbig b")?.className).toBe("est");
    expect(textOf(container)).toContain("估算");
    expect(textOf(container)).toContain("0.70s");
    // 生成中不报端到端（25.6 是上一轮的精确值，不该冒充本轮）
    expect(textOf(container)).not.toContain("25.6");
    // 状态文案已移除（否则两态字数不同会挤动趋势图宽度）
    expect(textOf(container)).not.toContain("本轮生成中");
    expect(textOf(container)).not.toContain("本轮生成速度");
    expect(textOf(container)).not.toContain("端到端");
    // 生成中趋势图必须在（与「本轮结束」态同一布局尺寸）
    expect(container.querySelector(".ctx2-speedrow .ctx2-spark")).toBeTruthy();
  });

  it("速度趋势图：近 N 轮速度线 + 平均（与水位线共存、配色区分）", () => {
    const { container } = render(
      <ToolPanel contextStats={SPEED_STATS} contextHistory={SPEED_HISTORY} mcpServers={[]} tools={[]} todos={[]} />
    );
    const speedLine = container.querySelector(".ctx2-sparkline.speed");
    expect(speedLine).toBeTruthy();
    expect(speedLine?.getAttribute("points")?.length).toBeGreaterThan(0);
    expect(textOf(container)).toContain("近 12 轮速度");
    expect(textOf(container)).toContain("平均");
    // 水位线仍独立存在（两条线不互相覆盖）
    expect(container.querySelectorAll(".ctx2-spark").length).toBe(2);
  });

  it("无速度数据（旧载荷 / 适配器未计量）：整块不渲染，不占位", () => {
    const { container } = render(
      <ToolPanel contextStats={STATS} contextHistory={HISTORY} mcpServers={[]} tools={[]} todos={[]} />
    );
    expect(container.querySelector(".ctx2-speedrow")).toBeNull();
    // 其余区块照常
    expect(container.querySelector(".ctx2-card")).toBeTruthy();
  });
});
