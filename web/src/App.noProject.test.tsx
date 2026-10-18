// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 手机端未打开项目时的兜底（用户反馈：点「＋」直接开始对话却提示「请先选择项目」）：
// 手机 PWA 首次连上 Core 时该端没有项目视图，此前一律被「请先打开项目」拦下，
// 而手机上也没有原生目录对话框。现在「✏️ 对话 / ＋ 新建任务 / 发送」在无项目时
// 默认落个人区（~/lite-work/personal）直接开工；桌面（Electron / 宽屏浏览器）
// 保持原行为——那里有目录选择器，不该替用户决定产物落哪。

import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
// @ts-expect-error vi.mock 扩展了 ./api 的导出（__mocks/__defaults 仅供本测试使用）
import { __mocks, __defaults } from "./api";

const PERSONAL = "/home/me/lite-work/personal";

vi.mock("./api", () => {
  const __defaults: Record<string, unknown> = {
    // 未打开项目（手机端首次连接 Core）
    status: { workspace: "", version: "2.0.1" },
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
    stopTask: { ok: true },
    getSession: { messages: [], metadata: {} },
    worktreeStatus: { exists: false },
    sessionModel: null,
    contextStats: null,
    getTodos: null,
    pendingApprovals: { approvals: [] },
    fileDownloadUrl: "/mock/download",
    fileRawUrl: "/mock/raw",
    // 兜底落盘位置（个人区）
    personalWorkspace: { path: "/home/me/lite-work/personal" },
    setWorkspace: { ok: true, workspace: "/home/me/lite-work/personal" },
    setSessionGoal: { ok: true },
    seedTodos: { ok: true },
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
    constructor(url: string) {
      this.url = url;
    }
    close() {
      this.readyState = MockEventStream.CLOSED;
    }
  }
  return {
    SSE_CLOSED: 2,
    connectEventStream: (url: string) => new MockEventStream(url),
  };
});

/** 布局开关：?mobile=1 强制移动壳 / ?mobile=0 强制桌面（见 hooks/useIsMobile）。 */
function setLayout(mobile: boolean) {
  window.history.replaceState({}, "", mobile ? "/?mobile=1" : "/?mobile=0");
}

beforeEach(() => {
  localStorage.clear();
  for (const [k, v] of Object.entries(__defaults)) __mocks[k].mockReset().mockResolvedValue(v);
});

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
  window.history.replaceState({}, "", "/");
  delete (window as unknown as { liteWork?: unknown }).liteWork;
});

describe("手机端未打开项目时的兜底", () => {
  it("点「✏️ 对话」：自动切到个人区并进入对话（不弹「请先打开项目」）", async () => {
    setLayout(true);
    const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});
    render(<App />);
    await waitFor(() => expect(__mocks.status).toHaveBeenCalled());
    // 手机壳首页：顶部显示「未打开项目」
    await screen.findByText("未打开项目");

    fireEvent.click(screen.getByText("✏️ 对话"));

    await waitFor(() => expect(__mocks.setWorkspace).toHaveBeenCalledWith(PERSONAL));
    expect(alertSpy).not.toHaveBeenCalled();
  });

  it("点「＋」：向导默认落个人区（「当前项目」置灰），提交后按个人区建会话", async () => {
    setLayout(true);
    const user = userEvent.setup();
    render(<App />);
    await waitFor(() => expect(__mocks.status).toHaveBeenCalled());
    await screen.findByText("未打开项目");

    fireEvent.click(screen.getByTitle("新建任务"));

    // 未打开项目 → 「当前项目」不可选、默认勾选个人区
    const current = await screen.findByRole("radio", { name: /当前项目/ });
    expect(current).toBeDisabled();
    expect(screen.getByRole("radio", { name: /个人/ })).toBeChecked();

    await user.type(screen.getByRole("textbox", { name: "任务目标" }), "整理照片");
    await user.click(screen.getByRole("button", { name: "开始任务" }));

    await waitFor(() => expect(__mocks.setWorkspace).toHaveBeenCalledWith(PERSONAL));
    // 会话按个人区创建（metadata 归属个人区，任务随之钉在该工作区）
    await waitFor(() => expect(__mocks.createSession).toHaveBeenCalledWith(undefined, PERSONAL));
  });
});

describe("桌面端未打开项目：保持原行为", () => {
  it("渲染后不自动落个人区（桌面走「打开项目」入口，由用户选目录）", async () => {
    setLayout(false);
    render(<App />);
    await waitFor(() => expect(__mocks.status).toHaveBeenCalled());
    // 桌面无项目时落在「项目」列表视图：看到的是「打开项目」入口
    await screen.findByText("打开项目");

    // 给异步兜底留出时间窗：桌面端不应被自动切到个人区
    await new Promise((r) => setTimeout(r, 50));
    expect(__mocks.setWorkspace).not.toHaveBeenCalled();
    expect(__mocks.personalWorkspace).not.toHaveBeenCalled();
  });
});
