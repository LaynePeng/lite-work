// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 「关闭对话 / 关闭项目」要连后端一起回收（用户报障：关掉对话或切走项目后
// Core 不回收，得整个软件重启）。
//
// 现象：关页签只清前端 chatStates，Core 里该会话的子 Agent 消息链、统计、
// 审批规则一直挂着；切项目也只换视图，旧目录的 ShellPlugin / git 缓存永不淘汰。
// 修复后的不变量：
//   1. 关掉会话页签 → 调 POST /api/sessions/{id}/release（不是删除会话）；
//   2. 关闭项目 → 调 POST /api/workspace/close，本端回到「未打开项目」；
//   3. 两者都失败也不能卡住界面（释放是尽力而为）。

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
// @ts-expect-error vi.mock 扩展了 ./api 的导出（__mocks/__defaults 仅供本测试使用）
import { __mocks, __defaults } from "./api";

vi.mock("./api", () => {
  const __defaults: Record<string, unknown> = {
    status: { workspace: "/ws", version: "2.0.5" },
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
    sessionActiveTask: { session_id: "s_a", task_id: "", running: false },
    // 新接口
    releaseSession: { ok: true, session_id: "s_a", released: { agents: true } },
    closeWorkspace: { ok: true, workspace: "", closed: "/ws" },
    // 显式给出：断言「关页签不能走删除接口」需要这个 spy 存在
    deleteSession: { ok: true },
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
  return { SSE_CLOSED: 2, connectEventStream: (url: string) => new MockEventStream(url) };
});

const SESSIONS = [
  { session_id: "s_a", created_at: 1_000, updated_at: 2_000, message_count: 2, title: "会话A", metadata: { workspace: "/ws" } },
];

beforeEach(() => {
  localStorage.clear();
  for (const [k, v] of Object.entries(__defaults)) __mocks[k].mockReset().mockResolvedValue(v);
  __mocks.sessions.mockResolvedValue(SESSIONS);
  __mocks.getSession.mockResolvedValue({
    messages: [{ role: "user", content: "会话A的内容" }],
    metadata: { workspace: "/ws" },
  });
});

afterEach(() => {
  localStorage.clear();
});

async function openSessionA() {
  render(<App />);
  const item = await screen.findByText("会话A", undefined, { timeout: 3000 });
  fireEvent.click(item);
  await waitFor(() => expect(__mocks.getSession).toHaveBeenCalled());
}

describe("关闭对话页签 → 后端释放会话内存态", () => {
  it("关掉会话A 页签时调 releaseSession（不是删除会话）", async () => {
    await openSessionA();

    // 至少保留一个页签（前端约束）：先新建再关
    fireEvent.click(screen.getByText("＋ 新建会话"));
    await waitFor(() => expect(document.querySelectorAll(".tab-item").length).toBe(2));

    const tabA = Array.from(document.querySelectorAll(".tab-item"))
      .find((el) => el.querySelector(".tab-title")?.textContent === "会话A");
    expect(tabA).toBeTruthy();
    fireEvent.click(tabA!.querySelector(".tab-close")!);

    await waitFor(() => expect(__mocks.releaseSession).toHaveBeenCalledWith("s_a"));
    // 绝不能走删除接口——关页签不等于删对话
    expect(__mocks.deleteSession).not.toHaveBeenCalled();
  });

  it("release 接口失败也不影响关页签（尽力而为）", async () => {
    __mocks.releaseSession.mockRejectedValue(new Error("409 有任务在跑"));
    await openSessionA();

    fireEvent.click(screen.getByText("＋ 新建会话"));
    await waitFor(() => expect(document.querySelectorAll(".tab-item").length).toBe(2));
    const tabA = Array.from(document.querySelectorAll(".tab-item"))
      .find((el) => el.querySelector(".tab-title")?.textContent === "会话A");
    fireEvent.click(tabA!.querySelector(".tab-close")!);

    // 页签确实关掉了（界面不被后端失败卡住）
    await waitFor(() => expect(
      Array.from(document.querySelectorAll(".tab-item"))
        .some((el) => el.querySelector(".tab-title")?.textContent === "会话A"),
    ).toBe(false));
  });
});

describe("关闭项目 → 后端释放项目缓存 + 回到未打开项目", () => {
  /** 进入某项目的会话视图（侧栏「← 项目列表」→ 点项目）。 */
  async function enterProjectView() {
    render(<App />);
    await screen.findByText("会话A", undefined, { timeout: 3000 });
  }

  it("项目视图里提供「关闭项目」按钮并调用 closeWorkspace", async () => {
    await enterProjectView();

    const btn = await screen.findByTitle(/关闭项目/);
    fireEvent.click(btn);

    await waitFor(() => expect(__mocks.closeWorkspace).toHaveBeenCalled());
  });

  it("关闭项目失败也回到项目列表（不把用户锁在项目里）", async () => {
    __mocks.closeWorkspace.mockRejectedValue(new Error("boom"));
    await enterProjectView();

    fireEvent.click(await screen.findByTitle(/关闭项目/));

    await waitFor(() => expect(__mocks.closeWorkspace).toHaveBeenCalled());
    // 依然回到「项目列表」（列表里能看到「最近打开」区块）
    await waitFor(() => expect(screen.getByText(/最近打开/)).toBeInTheDocument());
  });
});
