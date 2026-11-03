// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 盯梢通知「打开会话」必须**先切到该会话所属项目**再打开（bug 回归）。
//
// 现象：新版本装上后主动跑盯梢，结果会话确实生成了，但界面上看不到对话。
// 根因有两层：
//   1. 计划会话落在计划自己的 workspace（如 /ws-personal），而用户当前视图
//      可能在另一个项目；会话列表按 workspace 严格过滤 ⇒ 那个会话不显示。
//   2. 通知的 onOpenSession 走的是 `selectSession(sid)`，它**只打开不切项目**，
//      于是即便点了「打开会话」，在当前项目下也只会看到一个空对话。
//
// 修复后的不变量：点通知 → 调到 api.setWorkspace(该会话的 workspace) →
// 再打开会话。用 openSessionWithProject 而不是 selectSession。

import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
// @ts-expect-error vi.mock 扩展了 ./api 的导出（__mocks/__defaults 仅供本测试使用）
import { __mocks, __defaults } from "./api";

vi.mock("./api", () => {
  const __defaults: Record<string, unknown> = {
    status: { workspace: "/ws-current", version: "2.1.1" },
    agents: [{ id: "build", mode: "primary", description: "开发" }],
    llmConfig: {
      active: "deepseek",
      providers: {
        deepseek: {
          name: "DeepSeek", model: "deepseek-chat",
          reasoning_effort: "", has_key: true,
        },
      },
    },
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
    // 切项目：后端返回目标工作区（盯梢会话所属的那个）
    setWorkspace: { ok: true, workspace: "/ws-personal" },
    sessionActiveTask: { session_id: "s_watch", task_id: "", running: false },
    fileDownloadUrl: "/mock/download",
    fileRawUrl: "/mock/raw",
  };
  const __mocks: Record<string, ReturnType<typeof vi.fn>> = {};
  for (const [k, v] of Object.entries(__defaults)) {
    __mocks[k] = vi.fn().mockResolvedValue(v);
  }
  return {
    api: new Proxy(__mocks, {
      get: (t, p) => {
        if (typeof p !== "string") return undefined;
        if (!(p in t)) t[p] = vi.fn().mockResolvedValue({ ok: true });
        return t[p];
      },
    }),
    apiUrl: (p: string) => p,
    authHeaders: () => ({}),
    __mocks,
    __defaults,
  };
});

// 盯梢通知：会话归属 /ws-personal，与当前 /ws-current 不同
vi.mock("./hooks/useWatchNotifications", () => ({
  useWatchNotifications: () => ({
    items: [
      {
        id: 1,
        at: Date.now() / 1000,
        schedule_id: "sch_x",
        session_id: "s_watch",
        name: "A股开盘简报",
        status: "ok",
        detail: "已触发",
        task_id: "t9",
        notify: ["in_app"],
      },
    ],
    liveItems: [
      {
        id: 1,
        at: Date.now() / 1000,
        schedule_id: "sch_x",
        session_id: "s_watch",
        name: "A股开盘简报",
        status: "ok",
        detail: "已触发",
        task_id: "t9",
        notify: ["in_app"],
      },
    ],
    connected: true,
    clear: () => {},
  }),
  notificationLabel: (s: string) => s,
  notificationTone: () => "good",
  showSystemNotification: () => {},
  wantsSystemNotification: () => false,
}));

// 直接渲染通知条上的「打开会话 →」按钮需要它出现：这里改成直接驱动
// App 传给 WatchToaster 的 onOpenSession —— 通过 UI 点击验证真实链路。
describe("盯梢通知打开会话", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    for (const [k, v] of Object.entries(__defaults)) {
      (__mocks[k] as ReturnType<typeof vi.fn>).mockResolvedValue(v);
    }
    // 会话列表：盯梢会话属于另一个项目（App 靠它查 workspace）
    (__mocks.sessions as ReturnType<typeof vi.fn>).mockResolvedValue([
      {
        session_id: "s_watch",
        title: "盯梢：A股开盘简报",
        created_at: Date.now(),
        updated_at: Date.now(),
        message_count: 19,
        metadata: { workspace: "/ws-personal", name: "盯梢：A股开盘简报" },
        running: false,
      },
    ]);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("点击通知里的打开会话会先切项目再进入（不是只 selectSession）", async () => {
    render(<App />);
    // 等首屏 status 落地
    await waitFor(() => expect(__mocks.status).toHaveBeenCalled());

    const openBtn = await screen.findByText("打开会话 →", {}, { timeout: 4000 });
    openBtn.click();

    await waitFor(() => {
      // 关键断言：必须调用 setWorkspace —— 只 selectSession 会让用户看到空对话
      expect(__mocks.setWorkspace).toHaveBeenCalledWith("/ws-personal");
    });
  });
});
