// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
// @ts-expect-error vi.mock 扩展了 ./api 的导出（__mocks/__defaults 仅供本测试使用）
import { api, __mocks, __defaults } from "./api";

// api 模块全量 mock：显式默认值 + 任意方法兜底（resolved { ok: true }），
// 使 App 渲染不依赖后端（SSE 由 EventSource 全局 stub 兜底）。
vi.mock("./api", () => {
  const __defaults: Record<string, unknown> = {
    status: { workspace: "/ws", version: "1.9.7" },
    agents: [{ id: "build", mode: "primary", description: "开发" }],
    llmConfig: {
      active: "deepseek",
      providers: {
        deepseek: { name: "DeepSeek", model: "deepseek-chat", reasoning_effort: "", has_key: true },
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
    createSession: { session_id: "s1" },
    chat: { task_id: "t1" },
    getSession: { messages: [], metadata: {} },
    worktreeStatus: { exists: false },
    sessionModel: null,
    contextStats: null,
    getTodos: null,
    pendingApprovals: { approvals: [] },
    // 字符串 URL 辅助方法（渲染期直接进 href，不能是函数）
    fileDownloadUrl: "/mock/download",
    fileRawUrl: "/mock/raw",
    outputsZipUrl: "/mock/zip",
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

// jsdom 无 EventSource：connectTaskStream 构造时需要，用空实现 stub。
class MockEventSource {
  static CLOSED = 2;
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  readyState = 0;
  url: string;
  constructor(url: string) {
    this.url = url;
  }
  close() {
    this.readyState = MockEventSource.CLOSED;
  }
  addEventListener() {}
  removeEventListener() {}
}

beforeEach(() => {
  vi.stubGlobal("EventSource", MockEventSource);
  // setup.ts 的 afterEach 会 vi.restoreAllMocks() 清掉 mockResolvedValue 实现，
  // 这里在每个用例前重配默认值（同时清空上例的调用记录）。
  for (const [k, v] of Object.entries(__defaults)) {
    __mocks[k].mockReset().mockResolvedValue(v);
  }
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("推理强度（reasoning_effort）切换生效", () => {
  it("draft 路径：新建会话 tab 切换档位后发送，请求带新档位并接力进会话", async () => {
    const user = userEvent.setup();
    render(<App />);
    // 等待启动加载完成（workspace 就绪 → 主界面）
    await waitFor(() => expect(__mocks.status).toHaveBeenCalled());
    await waitFor(() => expect(__mocks.llmConfig).toHaveBeenCalled());

    // Alt+N 新建占位会话 tab（draft 路径）
    await user.keyboard("{Alt>}n{/Alt}");
    const input = await screen.findByPlaceholderText(/下达任务/);

    // 打开推理强度菜单，选「低」
    await user.click(screen.getByTitle(/推理强度/));
    await user.click(screen.getByText("低"));
    await waitFor(() => expect(screen.getByTitle(/推理强度：低/)).toBeTruthy());

    // 发送：api.chat 第 4 参（reasoningEffort）应为 "low"
    await user.type(input, "你好");
    await user.click(screen.getByRole("button", { name: "➤" }));
    await waitFor(() => expect(__mocks.chat).toHaveBeenCalledTimes(1));
    expect(__mocks.chat.mock.calls[0][3]).toBe("low");

    // 建会话时 draft 档位接力进会话：触发器显示「会话级」
    await waitFor(() => expect(screen.getByTitle(/推理强度：低（会话级）/)).toBeTruthy());
  });

  it("会话路径：已建会话切换档位后发送，请求带新档位", async () => {
    __mocks.sessions.mockResolvedValue([{
      session_id: "s1", created_at: 1, updated_at: 1, message_count: 0,
      title: "历史会话", metadata: {},
    }]);
    const user = userEvent.setup();
    render(<App />);
    await waitFor(() => expect(__mocks.status).toHaveBeenCalled());

    // 已有工作区 → 侧边栏直接进「会话列表」，点开历史会话
    await user.click(await screen.findByText("历史会话"));
    const input = await screen.findByPlaceholderText(/下达任务/);

    await user.click(screen.getByTitle(/推理强度/));
    await user.click(screen.getByText("低"));
    await waitFor(() => expect(screen.getByTitle(/推理强度：低/)).toBeTruthy());

    await user.type(input, "继续");
    await user.click(screen.getByRole("button", { name: "➤" }));
    await waitFor(() => expect(__mocks.chat).toHaveBeenCalledTimes(1));
    const args = __mocks.chat.mock.calls[0];
    expect(args[0]).toBe("s1");
    expect(args[3]).toBe("low");
  });
});
