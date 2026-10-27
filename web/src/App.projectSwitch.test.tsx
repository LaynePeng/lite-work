// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 切换项目后「已打开的对话页签」必须原样回来（bug 2 回归）。
//
// 现象：在页签 A 里打开某个对话 → 切到别的项目 → 点回页签 A，对话是空的、
// 也没有连接（消息没了、任务还在后端跑却看不到输出）。
// 根因：切项目走的是「清空全部会话状态」——页签还在，状态却没了；而且
// 「打开历史会话」由 `if (!chatStatesRef.current[sid])` 保护，状态一旦存在
// 就再也不会重新拉取，于是页签永久停在空白。
//
// 修复后的不变量：
//   1. 切项目只释放「页签已关掉」的会话状态，已开页签的消息保持；
//   2. 页签激活时若会话状态缺失则补拉历史（抗任何状态释放路径）；
//   3. 页签激活时若后端该会话仍在跑任务，则补连 SSE（不是只显示快照）。

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
// @ts-expect-error vi.mock 扩展了 ./api 的导出（__mocks/__defaults 仅供本测试使用）
import { __mocks, __defaults } from "./api";

vi.mock("./api", () => {
  const __defaults: Record<string, unknown> = {
    status: { workspace: "/ws", version: "2.0.4" },
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
    createSession: { session_id: "s_new" },
    chat: { task_id: "t1" },
    getSession: { messages: [], metadata: {} },
    worktreeStatus: { exists: false },
    sessionModel: null,
    contextStats: null,
    getTodos: null,
    pendingApprovals: { approvals: [] },
    // 切项目：后端返回新工作区
    openProject: { ok: true, workspace: "/ws2" },
    // 默认没有运行中的任务（补连逻辑读到 running=false 就不连）
    sessionActiveTask: { session_id: "s_a", task_id: "", running: false },
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

// SSE 客户端 mock：记录所有连接 URL，便于断言「补连」是否发生
const sseUrls: string[] = [];
vi.mock("./lib/sse", () => {
  class MockEventStream {
    static CLOSED = 2;
    onopen: (() => void) | null = null;
    onmessage: ((e: { data: string }) => void) | null = null;
    onerror: (() => void) | null = null;
    readyState = 0;
    url: string;
    constructor(url: string) {
      this.url = url;
      sseUrls.push(url);
    }
    close() { this.readyState = MockEventStream.CLOSED; }
  }
  return { SSE_CLOSED: 2, connectEventStream: (url: string) => new MockEventStream(url) };
});

const SESSIONS = [
  { session_id: "s_a", created_at: 1_000, updated_at: 2_000, message_count: 2, title: "会话A", metadata: { workspace: "/ws" } },
];
const RECENT = {
  items: [{ path: "/ws2", name: "ws2", kind: "code", is_git: true, pinned: false, opened_at: "2026-01-01" }],
};

const tabTitles = () =>
  Array.from(document.querySelectorAll(".tab-title")).map((e) => e.textContent ?? "");
const getSessionCalls = (sid: string) =>
  __mocks.getSession.mock.calls.filter((c: unknown[]) => c[0] === sid).length;

beforeEach(() => {
  localStorage.clear();
  sseUrls.length = 0;
  for (const [k, v] of Object.entries(__defaults)) __mocks[k].mockReset().mockResolvedValue(v);
  __mocks.sessions.mockResolvedValue(SESSIONS);
  __mocks.recentProjects.mockResolvedValue(RECENT);
  __mocks.getSession.mockResolvedValue({
    messages: [{ role: "user", content: "会话A的内容" }],
    metadata: { workspace: "/ws" },
  });
});

afterEach(() => {
  localStorage.clear();
});

/** 打开会话A（绑定页签）。 */
async function openSessionA() {
  render(<App />);
  const item = await screen.findByText("会话A", undefined, { timeout: 3000 });
  fireEvent.click(item);
  await waitFor(() => expect(getSessionCalls("s_a")).toBeGreaterThan(0));
  await waitFor(() => expect(tabTitles()).toEqual(["会话A"]));
}

/** 切到另一个项目（最近项目列表里的 ws2）。 */
async function switchToOtherProject() {
  // 侧栏会话视图 →「← 项目列表」
  fireEvent.click(await screen.findByText(/← 项目列表/));
  const project = await screen.findByText("ws2");
  fireEvent.click(project);
  await waitFor(() => expect(__mocks.openProject).toHaveBeenCalledWith("/ws2"));
}

describe("切换项目后已打开的对话页签（bug 2 回归）", () => {
  it("切项目不会抹掉已开页签的对话：消息仍在，不需要重新拉取", async () => {
    await openSessionA();
    expect(await screen.findByText("会话A的内容")).toBeInTheDocument();

    await switchToOtherProject();

    // 点回该页签（页签栏点击 = 切换激活页签）
    const tab = Array.from(document.querySelectorAll(".tab-item"))
      .find((el) => el.querySelector(".tab-title")?.textContent === "会话A");
    expect(tab).toBeTruthy();
    fireEvent.click(tab!);

    // 对话内容还在（旧实现这里是空白页），且没有多余的补拉（状态没被清掉）
    expect(await screen.findByText("会话A的内容")).toBeInTheDocument();
    expect(getSessionCalls("s_a")).toBe(1);
  });

  it("切项目后回到页签：后端仍在跑的任务会被补连（不是只显示历史快照）", async () => {
    __mocks.sessionActiveTask.mockResolvedValue({ session_id: "s_a", task_id: "t9", running: true });
    await openSessionA();

    await switchToOtherProject();

    // 回到该页签 → 触发「补连运行中任务」
    const tab = Array.from(document.querySelectorAll(".tab-item"))
      .find((el) => el.querySelector(".tab-title")?.textContent === "会话A");
    fireEvent.click(tab!);

    await waitFor(() => expect(sseUrls.some((u) => u.includes("/api/tasks/t9/events"))).toBe(true));
    // 运行中：输入区出现「停止任务」按钮（说明 running 状态也补上了）
    await waitFor(() => expect(screen.getByTitle("停止任务")).toBeInTheDocument());
  });

  it("页签状态缺失时（如被释放）激活即补拉历史，而不是显示空白对话", async () => {
    __mocks.sessionActiveTask.mockResolvedValue({ session_id: "s_a", task_id: "", running: false });
    await openSessionA();
    const before = getSessionCalls("s_a");

    // 关掉页签 → 状态被释放（内存回归的既有行为）
    const tab = Array.from(document.querySelectorAll(".tab-item"))
      .find((el) => el.querySelector(".tab-title")?.textContent === "会话A");
    // 至少保留一个页签：先新建一个再关会话A
    fireEvent.click(screen.getByText("＋ 新建会话"));
    const tabAfterNew = Array.from(document.querySelectorAll(".tab-item"))
      .find((el) => el.querySelector(".tab-title")?.textContent === "会话A");
    fireEvent.click((tabAfterNew ?? tab)!.querySelector(".tab-close")!);

    // 重新点开该会话
    fireEvent.click(await screen.findByText("会话A"));
    await waitFor(() => expect(getSessionCalls("s_a")).toBeGreaterThan(before));
    expect(await screen.findByText("会话A的内容")).toBeInTheDocument();
  });
});
