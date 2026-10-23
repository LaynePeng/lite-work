// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 「打开项目」默认落点：自动进入该项目**最近一条**会话（无历史则新建空对话）。
// 背景：换项目后停在空白新会话会让人以为历史丢了，也会误以为要从头开始。

import { render, screen, waitFor } from "@testing-library/react";
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

// SSE 走 fetch 流式客户端（lib/sse.ts）：mock 成永远挂起的空流，
// 避免 jsdom 里真发请求（回调不触发即「连接中」）。类必须在工厂内定义：
// vi.mock 提升到文件顶部，工厂执行时外部定义的类尚未求值。
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

/** 三条历史会话：**故意按「最老在前」返回**，验证取的是 updated_at 最大者（不依赖接口顺序）。 */
const SESSIONS = [
  { session_id: "s_old", created_at: 1_000, updated_at: 1_000, message_count: 2, title: "最老的对话", metadata: { workspace: "/projB" } },
  { session_id: "s_mid", created_at: 2_000, updated_at: 2_000, message_count: 2, title: "中间的对话", metadata: { workspace: "/projB" } },
  { session_id: "s_new", created_at: 3_000, updated_at: 3_000, message_count: 2, title: "最新的对话", metadata: { workspace: "/projB" } },
];

beforeEach(() => {
  localStorage.clear();
  for (const [k, v] of Object.entries(__defaults)) __mocks[k].mockReset().mockResolvedValue(v);
});

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("打开项目后的默认落点", () => {
  it("带「打开项目」意图时：自动打开**最近一条**会话（不是最老的），且不留下多余的新会话页签", async () => {
    __mocks.sessions.mockResolvedValue(SESSIONS);
    __mocks.getSession.mockResolvedValue({
      messages: [{ role: "user", content: "NEWEST-内容" }],
      metadata: { workspace: "/projB" },
    });
    // 「打开项目」重载前写入的一次性意图
    localStorage.setItem("litework.openLatestSession", "/projB");

    render(<App />);

    // 打开的是 updated_at 最大那条（接口顺序是最老在前 → 必须按 updated_at 取最大值）
    await waitFor(() => expect(__mocks.getSession).toHaveBeenCalledWith("s_new"));
    expect(__mocks.getSession).not.toHaveBeenCalledWith("s_old");
    // 页签标题用会话名；且只应有一个页签（复用了启动时空对话 tab，没有多余「新会话」）
    await waitFor(() => {
      const titles = Array.from(document.querySelectorAll(".tab-title")).map((e) => e.textContent);
      expect(titles).toEqual(["最新的对话"]);
    });
    // 一次性意图已消费：不应残留
    expect(localStorage.getItem("litework.openLatestSession")).toBeNull();
  });

  it("没有该意图（普通启动）：不自动打开任何历史会话，仍是新建空对话", async () => {
    __mocks.sessions.mockResolvedValue(SESSIONS);

    render(<App />);

    expect(__mocks.getSession).not.toHaveBeenCalled();
    // 输入框与「新会话」页签可能不同帧就绪（慢速 CI 上页签 effect 晚于
    // 输入框渲染）——都收进 waitFor，避免时序 flaky
    await waitFor(() => {
      expect(screen.getByPlaceholderText(/下达任务/)).toBeTruthy();
      const titles = Array.from(document.querySelectorAll(".tab-title")).map((e) => e.textContent);
      expect(titles).toEqual(["新会话"]);
    });
  });

  it("带意图但项目没有历史会话：退化为新建空对话（不报错、不长草）", async () => {
    __mocks.sessions.mockResolvedValue([]);
    localStorage.setItem("litework.openLatestSession", "/empty-proj");

    render(<App />);

    await waitFor(() => expect(__mocks.sessions).toHaveBeenCalled());
    expect(__mocks.getSession).not.toHaveBeenCalled();
    await waitFor(() => {
      const titles = Array.from(document.querySelectorAll(".tab-title")).map((e) => e.textContent);
      expect(titles).toEqual(["新会话"]);
    });
  });

  it("会话列表拉取失败：退化为新建空对话，不阻塞启动", async () => {
    __mocks.sessions.mockRejectedValue(new Error("boom"));
    localStorage.setItem("litework.openLatestSession", "/projB");

    render(<App />);

    await waitFor(() => {
      const titles = Array.from(document.querySelectorAll(".tab-title")).map((e) => e.textContent);
      expect(titles).toEqual(["新会话"]);
    });
    expect(__mocks.getSession).not.toHaveBeenCalled();
  });
});
