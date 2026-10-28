// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import { fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
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

  it("页签顺序：盯梢在「后台」之后，MCP 在「工具」之后（低频/配置类入口靠后）", () => {
    const { container } = render(
      <ToolPanel contextStats={STATS} mcpServers={[]} tools={[]} todos={[]} />
    );
    const labels = Array.from(container.querySelectorAll(".panel-tab")).map((b) => b.textContent);
    expect(labels).toEqual(["上下文", "TODOs", "Agents", "后台", "盯梢", "工具", "MCP"]);
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
    expect(labels).toEqual(["上下文", "TODOs", "Agents", "后台", "盯梢", "工具", "MCP"]);
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
        title: "门禁判定", context: "工具执行前 delete_file", kind: "choice",
        question: "该工具调用应当放行、需人确认，还是拦截？",
        options: [
          { label: "拦截", prob: 0.88, tone: "danger", chosen: true },
          { label: "放行", prob: 0.09, tone: "ok", chosen: false },
          { label: "请人确认", prob: 0.03, tone: "warn", chosen: false },
        ],
        confidence: 0.91, threshold: 0.6, accepted: true,
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

  it("插件面板返回富围栏（kpi/board/progress）时渲染成原生组件而非源码", async () => {
    // 此前面板的 ReactMarkdown 没传 components，```kpi 等富围栏被当普通代码块
    // 显示纯 JSON 源码（聊天区却能渲染成组件）——两条通道行为不一致。
    const plugin = {
      name: "astock-plugin", path: "plugins/astock-plugin", is_dir: true, tools: [],
      description: "A股", version: "1.1.0", source: "github", kind: "tool",
      contributes: { panels: [{ id: "portfolio", title: "持仓" }] },
      status: { state: "running", reason: "" },
    } as never;
    const md = [
      "**持仓总览**",
      "",
      "```kpi",
      JSON.stringify({ items: [
        { label: "总资产", value: "15,201.95", delta: "+6.00", trend: "up" },
        { label: "可用资金", value: "7,749.95", trend: "flat" },
      ] }),
      "```",
      "",
      "```progress",
      JSON.stringify({ items: [
        { label: "仓位", value: 49, note: "上限 35%" },
        { label: "距止损", value: 100 },
      ] }),
      "```",
    ].join("\n");
    vi.spyOn(api, "plugins").mockResolvedValue({ plugins: [plugin] } as never);
    vi.spyOn(api, "pluginPanel").mockResolvedValue({
      name: "astock-plugin", panel: "portfolio", title: "持仓", markdown: md,
    } as never);
    const { container } = render(
      <ToolPanel contextStats={null} mcpServers={[]} tools={[]} todos={[]}
        activeTab="plugin:astock-plugin:portfolio" />
    );
    await waitFor(() => expect(api.pluginPanel).toHaveBeenCalled());
    // md 段照常渲染
    expect(container.textContent).toContain("持仓总览");
    // kpi 块：标签与数值出现（渲染成组件，而不是 JSON 源码文本）
    expect(container.textContent).toContain("总资产");
    expect(container.textContent).toContain("15,201.95");
    // progress 块
    expect(container.textContent).toContain("仓位");
    expect(container.textContent).toContain("距止损");
    // 关键：富围栏不该以源码形式整段出现
    expect(container.textContent).not.toContain('{"items"');
    vi.restoreAllMocks();
  });

  it("插件面板富围栏 JSON 非法时降级为 Markdown 代码块（不崩）", async () => {
    const plugin = {
      name: "p2", path: "plugins/p2", is_dir: true, tools: [],
      description: "", version: "1.0.0", source: "local", kind: "tool",
      contributes: { panels: [{ id: "panel", title: "面板" }] },
      status: { state: "running", reason: "" },
    } as never;
    const md = [
      "前置文字",
      "",
      "```kpi",
      "{ 这不是合法 JSON",
      "```",
      "",
      "后置文字",
    ].join("\n");
    vi.spyOn(api, "plugins").mockResolvedValue({ plugins: [plugin] } as never);
    vi.spyOn(api, "pluginPanel").mockResolvedValue({
      name: "p2", panel: "panel", title: "面板", markdown: md,
    } as never);
    const { container } = render(
      <ToolPanel contextStats={null} mcpServers={[]} tools={[]} todos={[]}
        activeTab="plugin:p2:panel" />
    );
    await waitFor(() => expect(api.pluginPanel).toHaveBeenCalled());
    // 前后 md 段正常渲染，坏 JSON 原样显示（不抛错、不吞内容）
    expect(container.textContent).toContain("前置文字");
    expect(container.textContent).toContain("后置文字");
    expect(container.textContent).toContain("这不是合法 JSON");
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

// ---------------------------------------------------------------- 工具 tab 指标整合（W3）

describe("ToolPanel · 工具 tab 指标整合（W3）", () => {
  const TOOLS = [
    { name: "read_file", description: "读文件", domain: "read" },
    { name: "write_file", description: "写文件", domain: "edit" },
    { name: "docx_create", description: "创建文档", domain: "office" },
  ];

  const METRICS = {
    tools: {
      read_file:  { calls: 42, errors: 1, cancelled: 0, success_rate: 0.976, avg_ms: 120 },
      write_file: { calls: 18, errors: 5, cancelled: 0, success_rate: 0.72, avg_ms: 89 },
    },
    skills: {
      "ppt-master": { uses: 12, auto: 8, explicit: 4, tools: { pptx_create: 6 } },
      "weekly-report": { uses: 0, auto: 0, explicit: 0, tools: {} },
    },
    tool_count: 2,
    skill_count: 2,
    worst_tools: [
      { tool: "write_file", calls: 18, errors: 5, cancelled: 0, success_rate: 0.72, avg_ms: 89 },
    ],
    events: 60,
    path: "/tmp/metrics.jsonl",
    enabled: true,
  };

  afterEach(() => {
    vi.restoreAllMocks();
  });

  function renderToolsPanel() {
    // 默认 tab 是 context，需要切到 tools——ToolPanel 支持受控 activeTab
    return render(
      <ToolPanel
        contextStats={STATS}
        mcpServers={[]}
        tools={TOOLS}
        todos={[]}
        activeTab="tools"
      />
    );
  }

  it("无指标数据：回退旧版清单（工具名+点），显示提示文案", async () => {
    vi.spyOn(api, "metricsTools").mockResolvedValue({
      tools: {}, skills: {}, tool_count: 0, skill_count: 0,
      worst_tools: [], events: 0, path: "", enabled: true,
    });
    const { container } = renderToolsPanel();
    // 等异步指标加载完成
    await waitFor(() => expect(api.metricsTools).toHaveBeenCalled());
    // 回退旧版文案
    expect(container.textContent).toContain("已注册工具（3）");
    // 工具名都在
    expect(container.textContent).toContain("read_file");
    expect(container.textContent).toContain("docx_create");
    // 无数据提示
    expect(container.textContent).toContain("运行任务后自动积累");
    // 不显示"需要关注"段
    expect(container.textContent).not.toContain("需要关注");
  });

  it("有指标数据：总览带数字，工具行带 次数·成功率，worst 段出现", async () => {
    vi.spyOn(api, "metricsTools").mockResolvedValue(METRICS);
    const { container } = renderToolsPanel();

    await waitFor(() => expect(container.textContent).toContain("调用 60 次"));
    // 工具行带指标
    expect(container.textContent).toContain("42·98%");
    expect(container.textContent).toContain("18·72%");
    // 未使用的工具显示 —
    expect(container.textContent).toContain("—");
    // 需要关注段
    expect(container.textContent).toContain("需要关注");
    expect(container.textContent).toContain("write_file");
  });

  it("技能使用：uses=0 警示，展开后可见", async () => {
    vi.spyOn(api, "metricsTools").mockResolvedValue(METRICS);
    const { container } = renderToolsPanel();

    await waitFor(() => expect(container.textContent).toContain("技能使用"));
    // 默认折叠：技能名不直接可见
    expect(container.textContent).not.toContain("ppt-master");
    // 但标题上有计数
    expect(container.textContent).toContain("技能使用（2）");
  });

  it("清空按钮：有数据才出现；点击调 clear 接口，面板回落无数据文案", async () => {
    vi.spyOn(api, "metricsTools").mockResolvedValue(METRICS);
    const clear = vi.spyOn(api, "metricsToolsClear").mockResolvedValue({ ok: true });
    const { container } = renderToolsPanel();

    // 无数据态（events=0）不显示清空按钮
    await waitFor(() => expect(container.querySelector(".tm-clear")).toBeTruthy());
    // 点击清空 → 调接口 → 本地指标清空，回到「已注册工具」与提示文案
    fireEvent.click(container.querySelector(".tm-clear")!);
    await waitFor(() => expect(clear).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(container.textContent).toContain("运行任务后自动积累"));
    expect(container.textContent).not.toContain("需要关注");
    // 清空后按钮随无数据态消失
    expect(container.querySelector(".tm-clear")).toBeNull();
  });

  it("无指标数据：不显示清空按钮（没有可清的东西）", async () => {
    vi.spyOn(api, "metricsTools").mockResolvedValue({
      tools: {}, skills: {}, tool_count: 0, skill_count: 0,
      worst_tools: [], events: 0, path: "", enabled: true,
    });
    const clear = vi.spyOn(api, "metricsToolsClear");
    const { container } = renderToolsPanel();
    await waitFor(() => expect(container.textContent).toContain("已注册工具（3）"));
    expect(container.querySelector(".tm-clear")).toBeNull();
    expect(clear).not.toHaveBeenCalled();
  });

  it("30s 轮询：组件卸载后清理定时器", async () => {
    vi.spyOn(api, "metricsTools").mockResolvedValue(METRICS);
    const { unmount } = renderToolsPanel();
    await waitFor(() => expect(api.metricsTools).toHaveBeenCalled());
    unmount();
    // 卸载后不再调用（轮询已清理）
    const calls = (api.metricsTools as ReturnType<typeof vi.fn>).mock.calls.length;
    await new Promise((r) => setTimeout(r, 100));
    expect((api.metricsTools as ReturnType<typeof vi.fn>).mock.calls.length).toBe(calls);
  });

  it("多级工具分级展示：按职责域分组，复合工具带 ★，组头可折叠", async () => {
    vi.spyOn(api, "metricsTools").mockResolvedValue(METRICS);
    const { container } = render(
      <ToolPanel
        contextStats={STATS}
        mcpServers={[]}
        tools={[
          { name: "edit_files", description: "批量编辑", domain: "edit" },
          { name: "apply_search_replace", description: "单补丁", domain: "edit" },
          { name: "read_manual", description: "手册", domain: "read" },
          { name: "execute_command", description: "命令", domain: "execute" },
        ]}
        todos={[]}
        activeTab="tools"
      />
    );
    await waitFor(() => expect(container.textContent).toContain("调用 60 次"));
    // 域组头按既定顺序出现：编辑 → 执行 → 阅读
    const titles = Array.from(container.querySelectorAll(".tm-section-title"))
      .map((el) => el.textContent ?? "");
    const editIdx = titles.findIndex((t) => t.startsWith("编辑（"));
    const execIdx = titles.findIndex((t) => t.startsWith("执行（"));
    const readIdx = titles.findIndex((t) => t.startsWith("阅读（"));
    expect(editIdx).toBeGreaterThanOrEqual(0);
    expect(execIdx).toBeGreaterThan(editIdx);
    expect(readIdx).toBeGreaterThan(execIdx);
    // 复合工具带 ★ 徽标
    expect(container.querySelector(".tm-composite")).toBeTruthy();
    // 无 domain 的后端不会出现未知组（组按 TOOL_DOMAINS 过滤）
    expect(container.textContent).not.toContain("其他（");
  });

  it("旧后端（无 domain 字段）：全部落「其他」组，仍能渲染", async () => {
    vi.spyOn(api, "metricsTools").mockResolvedValue(METRICS);
    const { container } = render(
      <ToolPanel
        contextStats={STATS}
        mcpServers={[]}
        tools={[
          { name: "read_file", description: "读文件" },
          { name: "write_file", description: "写文件" },
        ]}
        todos={[]}
        activeTab="tools"
      />
    );
    await waitFor(() => expect(container.textContent).toContain("调用 60 次"));
    // 无 domain → misc →「其他」组兜底
    expect(container.textContent).toContain("其他（2）");
    expect(container.textContent).toContain("read_file");
  });
});

describe("ToolPanel · 盯梢通知中心", () => {
  const notices = [
    { id: 1, at: Math.floor(Date.now() / 1000) - 120, schedule_id: "sch_1",
      name: "盯部署", status: "changed", detail: "main 有 3 个新提交", task_id: "t_1" },
    { id: 2, at: Math.floor(Date.now() / 1000) - 30, schedule_id: "sch_2",
      name: "盯邮箱", status: "unchanged", detail: "3 封未读", task_id: null },
    { id: 3, at: Math.floor(Date.now() / 1000) - 5, schedule_id: "sch_3",
      name: "盯构建", status: "error", detail: "抓取失败", task_id: null },
  ];

  const renderWatch = (over: Record<string, unknown> = {}) =>
    render(
      <ToolPanel
        contextStats={null}
        mcpServers={[]}
        tools={[]}
        todos={[]}
        activeTab="watch"
        watchNotifications={notices as never}
        watchConnected
        onClearWatchNotifications={vi.fn()}
        {...over}
      />,
    );

  it("列出通知（状态标签 / 摘要 / 相对时间）", () => {
    const { container } = renderWatch();
    expect(container.textContent).toContain("盯部署");
    expect(container.textContent).toContain("main 有 3 个新提交");
    expect(container.textContent).toContain("检测到变化");
    expect(container.textContent).toContain("出错");
    expect(container.textContent).toContain("2 分钟前");
  });

  it("角标只数「值得看」的，「无变化」不计入", () => {
    const { container } = renderWatch();
    // 3 条里 changed + error = 2 条值得看
    expect(container.textContent).toContain("盯梢 (2)");
    expect(container.textContent).toContain("值得看 2");
  });

  it("全部为「无变化」时不显示角标数字", () => {
    const { container } = renderWatch({
      watchNotifications: [notices[1]] as never,
    });
    expect(container.textContent).toContain("盯梢");
    expect(container.textContent).not.toContain("盯梢 (");
  });

  it("显示通知通道连接状态", () => {
    const { container } = renderWatch({ watchConnected: false });
    expect(container.textContent).toContain("断开");
    const { container: c2 } = renderWatch({ watchConnected: true });
    expect(c2.textContent).toContain("已连接");
  });

  it("空列表显示空态并提示通道状态", () => {
    const { container } = renderWatch({ watchNotifications: [] as never, watchConnected: false });
    expect(container.textContent).toContain("暂无盯梢通知");
    expect(container.textContent).toContain("通知通道未连接");
  });

  // 详情卡 portal 到 body（避开 .tool-panel 的 overflow:hidden），所以查 document
  const dialogEl = () => document.querySelector('[role="dialog"]') as HTMLElement | null;

  it("点条目弹出完整详情卡（浮层几秒就没，这里是回看入口）", () => {
    const { container } = renderWatch();
    expect(dialogEl()).toBeNull();
    fireEvent.click(container.querySelectorAll(".watch-item")[0] as Element);
    const dialog = dialogEl();
    expect(dialog).not.toBeNull();
    expect(dialog?.textContent).toContain("盯部署");
    expect(dialog?.textContent).toContain("main 有 3 个新提交");
    expect(dialog?.textContent).toContain("检测到变化");
    expect(dialog?.textContent).toContain("计划 sch_1");
    expect(dialog?.textContent).toContain("任务 t_1");
  });

  it("详情卡可关（Esc 与「打开会话」都行）", () => {
    const onOpenSession = vi.fn();
    const { container } = renderWatch({
      onOpenSession,
      watchSessionOf: (sid: string) => (sid === "sch_1" ? "sess_9" : undefined),
    });
    fireEvent.click(container.querySelectorAll(".watch-item")[0] as Element);
    fireEvent.click(document.querySelector(".watch-detail-btn.primary") as Element);
    expect(onOpenSession).toHaveBeenCalledWith("sess_9");
    expect(dialogEl()).toBeNull();

    fireEvent.click(container.querySelectorAll(".watch-item")[2] as Element);
    expect(dialogEl()).not.toBeNull();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(dialogEl()).toBeNull();
  });

  it("详情卡可「重新弹出提示」；无关联会话时说明原因而不是给死按钮", () => {
    const onResurface = vi.fn();
    const { container } = renderWatch({ onResurfaceWatch: onResurface, watchSessionOf: () => undefined });
    fireEvent.click(container.querySelectorAll(".watch-item")[1] as Element);
    expect(document.querySelector(".watch-detail-btn.primary")).toBeNull();
    expect(dialogEl()?.textContent).toContain("没有关联会话");
    fireEvent.click(document.querySelector(".watch-detail-btn") as Element);
    expect(onResurface).toHaveBeenCalledWith(2);
  });
});
