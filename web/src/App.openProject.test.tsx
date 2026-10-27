// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 打开项目/打开历史会话的页签落点语义：
//   1. 打开项目后默认只有一个「新建对话」占位 tab（不再自动进最近历史会话）；
//   2. 侧栏点击/双击历史会话时，若当前活跃的是**空白**新会话 tab（未绑定
//      session ⇒ 无气泡，且输入框无文字/粘贴块），该 tab 原位变成历史会话；
//   3. 新会话 tab 已有输入内容（打字/粘贴块）则不算空白：另开新 tab，不踩草稿。

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

const SESSIONS = [
  { session_id: "s_hist", created_at: 1_000, updated_at: 3_000, message_count: 2, title: "历史对话", metadata: { workspace: "/ws" } },
  { session_id: "s_hist2", created_at: 1_000, updated_at: 2_000, message_count: 4, title: "历史对话2", metadata: { workspace: "/ws" } },
];

const tabTitles = () =>
  Array.from(document.querySelectorAll(".tab-title")).map((e) => e.textContent);

beforeEach(() => {
  localStorage.clear();
  for (const [k, v] of Object.entries(__defaults)) __mocks[k].mockReset().mockResolvedValue(v);
});

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("打开项目后的默认落点", () => {
  it("普通启动 / 打开项目后：默认只有「新会话」占位 tab，不自动打开任何历史会话", async () => {
    __mocks.sessions.mockResolvedValue(SESSIONS);

    render(<App />);

    expect(__mocks.getSession).not.toHaveBeenCalled();
    await waitFor(() => {
      expect(screen.getByPlaceholderText(/下达任务/)).toBeTruthy();
      expect(tabTitles()).toEqual(["新会话"]);
    });
  });

  it("重载意图（litework.openLatestSession 已废弃）：残留旧键不生效，仍是新会话 tab", async () => {
    __mocks.sessions.mockResolvedValue(SESSIONS);
    localStorage.setItem("litework.openLatestSession", "/ws");

    render(<App />);

    expect(__mocks.getSession).not.toHaveBeenCalled();
    await waitFor(() => expect(tabTitles()).toEqual(["新会话"]));
  });
});

describe("侧栏点击/双击历史会话：空白新会话 tab 的接管", () => {
  const renderApp = async () => {
    __mocks.sessions.mockResolvedValue(SESSIONS);
    __mocks.getSession.mockResolvedValue({
      messages: [{ role: "user", content: "历史内容" }],
      metadata: { workspace: "/ws" },
    });
    const utils = render(<App />);
    // 等侧栏会话列表就绪
    await screen.findByText("历史对话");
    return utils;
  };

  it("单击历史会话：空白新会话 tab 原位变成该会话（不另开页签）", async () => {
    await renderApp();
    fireEvent.click(screen.getByText("历史对话"));

    await waitFor(() => expect(__mocks.getSession).toHaveBeenCalledWith("s_hist"));
    await waitFor(() => expect(tabTitles()).toEqual(["历史对话"]));
  });

  it("双击历史会话：与单击一致，空白新会话 tab 原位被接管", async () => {
    await renderApp();
    fireEvent.doubleClick(screen.getByText("历史对话"));

    await waitFor(() => expect(__mocks.getSession).toHaveBeenCalledWith("s_hist"));
    await waitFor(() => expect(tabTitles()).toEqual(["历史对话"]));
    expect(tabTitles()).toHaveLength(1);
  });

  it("新会话 tab 已输入文字：不算空白，历史会话另开新 tab（草稿保留）", async () => {
    await renderApp();
    // 在新会话 tab 的输入框里打字（Composer 内部 state → 上报 App）
    const ta = await screen.findByPlaceholderText(/下达任务/);
    await userEvent.type(ta, "写一半的草稿");

    fireEvent.click(screen.getByText("历史对话"));

    await waitFor(() => expect(__mocks.getSession).toHaveBeenCalledWith("s_hist"));
    await waitFor(() => {
      // 两个页签：草稿 tab 仍是「新会话」，历史会话另开
      expect(tabTitles()).toEqual(["新会话", "历史对话"]);
    });
  });

  it("草稿清空后：该页签重新算空白，历史会话仍原位接管（不让占位页签永久失效）", async () => {
    await renderApp();
    const ta = await screen.findByPlaceholderText(/下达任务/);
    await userEvent.type(ta, "临时草稿");
    await userEvent.clear(ta);
    // 等草稿上报落地（clear 之后 App 侧应已删掉该页签的草稿）
    await waitFor(() => expect((ta as HTMLTextAreaElement).value).toBe(""));

    fireEvent.click(screen.getByText("历史对话"));

    await waitFor(() => expect(tabTitles()).toEqual(["历史对话"]));
  });
});

// 回归（bug 1）：新会话 tab 里点历史记录，仍然另开了一个页签。
// 根因是「空白」判定读的是 App 侧的**残留标记**——切到文件/PPT 页签时 Composer
// 卸载，输入框文字丢失，标记却留在 ref 里；此后该页签永远不算空白。
// 现在草稿由 App 按页签托管（真实内容而非标记），并支持「当前不在空白页签上时
// 退而接管列表里的空占位页签」。
describe("新会话页签与历史记录的落点（bug 1 回归）", () => {
  beforeEach(() => {
    __mocks.sessions.mockResolvedValue(SESSIONS);
    __mocks.getSession.mockResolvedValue({
      messages: [{ role: "user", content: "历史内容" }],
      metadata: { workspace: "/ws" },
    });
  });

  it("在绑定页签打过字后新建会话：新页签输入框是空的（草稿不串台）", async () => {
    render(<App />);
    await screen.findByText("历史对话");
    // 1. 打开历史会话（绑定 session）并在输入框留一段草稿
    fireEvent.click(screen.getByText("历史对话"));
    await waitFor(() => expect(__mocks.getSession).toHaveBeenCalledWith("s_hist"));
    const ta = await screen.findByPlaceholderText(/下达任务/);
    await userEvent.type(ta, "历史会话里的草稿");

    // 2. 新建会话页签：输入框必须是空的（旧实现会把草稿文字一起带过来）
    fireEvent.click(screen.getByText("＋ 新建会话"));
    await waitFor(() => {
      const input = screen.getByPlaceholderText(/下达任务/) as HTMLTextAreaElement;
      expect(input.value).toBe("");
    });
  });

  it("在绑定页签打过字后新建会话，再点历史记录：接管而不是再开第三个页签", async () => {
    render(<App />);
    await screen.findByText("历史对话");
    fireEvent.click(screen.getByText("历史对话"));
    await waitFor(() => expect(__mocks.getSession).toHaveBeenCalledWith("s_hist"));
    const ta = await screen.findByPlaceholderText(/下达任务/);
    await userEvent.type(ta, "历史会话里的草稿");

    fireEvent.click(screen.getByText("＋ 新建会话"));
    // 新会话占位页签是空白的：点历史会话 2 应原位接管它
    fireEvent.click(screen.getByText("历史对话2"));

    await waitFor(() => expect(__mocks.getSession).toHaveBeenCalledWith("s_hist2"));
    await waitFor(() => {
      // 页签仍是两个：历史对话（绑定）+ 历史对话2（接管了空白占位页签）
      expect(tabTitles()).toEqual(["历史对话", "历史对话2"]);
    });
  });
});
