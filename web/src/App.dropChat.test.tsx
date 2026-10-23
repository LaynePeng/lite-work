// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 页签关闭后的会话状态释放（内存回归）。
// 背景：chatStates 按会话缓存整份 messages（长会话几 MB），此前只增不减——
// 关页签后状态仍驻留，开开关关十几次渲染进程就白占几十上百 MB。
// 现在关页签（无其他同会话页签且任务不在跑）会 dropChat 释放全部相关状态；
// 重开会话时 getSession 重新拉取（既验证释放、也验证重开路径完好）。

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
// @ts-expect-error vi.mock 扩展了 ./api 的导出（__mocks/__defaults 仅供本测试使用）
import { __mocks, __defaults } from "./api";

vi.mock("./api", () => {
  const __defaults: Record<string, unknown> = {
    status: { workspace: "/ws", version: "1.9.10" },
    agents: [{ id: "build", mode: "primary", description: "开发" }],
    llmConfig: { active: "deepseek", providers: { deepseek: { name: "DeepSeek", model: "deepseek-chat", reasoning_effort: "", has_key: true } } },
    llmProviders: [],
    mcpStatus: { servers: [] },
    tools: [],
    collabModes: { modes: [] },
    config: null,
    sessions: [],
    pricingStatus: { models_dev: null },
    recentProjects: { items: [] },
    worktreeList: { worktrees: [], main_branch: "main", main_head: "" },
    createSession: { session_id: "s1" },
    chat: { task_id: "t1" },
    getSession: { messages: [], metadata: {} },
    worktreeStatus: { exists: false },
    sessionModel: null,
    contextStats: null,
    getTodos: null,
    pendingApprovals: { approvals: [] },
    fileDownloadUrl: "/mock/download",
    fileRawUrl: "/mock/raw",
  };
  const __mocks: Record<string, ReturnType<typeof vi.fn>> = {};
  for (const [k, v] of Object.entries(__defaults)) __mocks[k] = vi.fn().mockResolvedValue(v);
  return {
    api: new Proxy(__mocks, {
      get: (t, p) => {
        if (typeof p !== "string") return undefined;
        if (!(p in t)) t[p] = vi.fn().mockResolvedValue({ ok: true });
        return t[p];
      },
    }),
    __mocks,
    __defaults,
  };
});

vi.mock("./lib/sse", () => {
  class MockEventStream {
    static CLOSED = 2;
    onopen: (() => void) | null = null;
    onmessage: ((e: { data: string }) => void) | null = null;
    onerror: (() => void) | null = null;
    readyState = 0;
    url: string;
    constructor(url: string) { this.url = url; }
    close() { this.readyState = MockEventStream.CLOSED; }
  }
  return {
    SSE_CLOSED: 2,
    connectEventStream: (url: string) => new MockEventStream(url),
  };
});

const SESSIONS = [
  { session_id: "s_a", created_at: 1_000, updated_at: 2_000, message_count: 2, title: "会话A", metadata: { workspace: "/ws" } },
];

beforeEach(() => {
  localStorage.clear();
  for (const [k, v] of Object.entries(__defaults)) __mocks[k].mockReset().mockResolvedValue(v);
});

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

async function tabTitles(): Promise<string[]> {
  return Array.from(document.querySelectorAll(".tab-title")).map((e) => e.textContent ?? "");
}

describe("页签关闭的会话状态释放", () => {
  it("关掉会话页签后重开同一会话：getSession 被再次调用（状态已释放，重新拉取历史）", async () => {
    __mocks.sessions.mockResolvedValue(SESSIONS);
    __mocks.getSession.mockResolvedValue({
      messages: [{ role: "user", content: "会话A的内容" }],
      metadata: { workspace: "/ws" },
    });

    const { unmount } = render(<App />);

    // 1. 新建第二个页签（满足 closeTab 的「至少保留一个」），选中会话A
    const newTabBtn = await screen.findByTitle(/新建|新会话|新标签/, undefined, { timeout: 3000 });
    fireEvent.click(newTabBtn);
    // 侧栏里点开会话A（Mobile 判定外必为桌面布局；侧栏会话项以标题渲染）
    const item = await screen.findByText("会话A", undefined, { timeout: 3000 });
    fireEvent.click(item);
    await waitFor(() => expect(__mocks.getSession).toHaveBeenCalledWith("s_a"));
    expect(await tabTitles()).toContain("会话A");

    // 2. 关掉「会话A」页签：定位该页签（.tab-title 文本）所在的 tab-item，
    //    点它自己的 .tab-close
    const tabItem = Array.from(document.querySelectorAll(".tab-item"))
      .find((el) => el.querySelector(".tab-title")?.textContent === "会话A");
    expect(tabItem).toBeTruthy();
    fireEvent.click(tabItem!.querySelector(".tab-close")!);
    // 关掉后页签列表不再含「会话A」（读 DOM 不必 await tabTitles 的 Promise）
    await waitFor(() => {
      const titles = Array.from(document.querySelectorAll(".tab-title"))
        .map((e) => e.textContent ?? "");
      expect(titles).not.toContain("会话A");
    });

    // 3. 重开同一会话：若状态未释放（仍命中 chatStatesRef），selectSession 不会
    //    再拉 getSession——释放后必须重新拉取
    const before = __mocks.getSession.mock.calls.filter((c: unknown[]) => c[0] === "s_a").length;
    const item2 = await screen.findByText("会话A", undefined, { timeout: 3000 });
    fireEvent.click(item2);
    await waitFor(() => {
      const after = __mocks.getSession.mock.calls.filter((c: unknown[]) => c[0] === "s_a").length;
      expect(after).toBeGreaterThan(before);
    }, { timeout: 3000 });

    unmount();
  });
});
