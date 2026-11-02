// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// Core 意外退出横幅。
//
// 回归点：Core 就绪后崩溃此前完全静默——不写日志、不通知窗口，用户只看到
// 「窗口还在但什么都不动」，而日志一行不留，「闪退」长期查无实据。
// 这条链路补上后，必须保证三件事：
//   1. 收到 core-lost 就给出可见提示（不再静默）；
//   2. 本地模式给「重启 Core」，远程模式不给（重启按钮对远程无意义）；
//   3. 重启失败要能看到原因，不能点完就没了。

import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
// @ts-expect-error vi.mock 扩展了 ./api 的导出（__mocks/__defaults 仅供本测试使用）
import { __mocks, __defaults } from "./api";

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

beforeEach(() => {
  for (const [k, v] of Object.entries(__defaults)) {
    __mocks[k].mockReset().mockResolvedValue(v);
  }
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete (window as unknown as { liteWork?: unknown }).liteWork;
});

/** 注入 preload 桥；返回触发 core-lost 的句柄。 */
function installBridge(options: {
  restartResult?: { ok: boolean; url?: string; error?: string };
  withRestart?: boolean;
} = {}) {
  let fire: ((info: unknown) => void) | null = null;
  const restartCore = vi.fn().mockResolvedValue(options.restartResult ?? { ok: true, url: "http://127.0.0.1:1" });
  (window as unknown as { liteWork?: unknown }).liteWork = {
    platform: "darwin",
    version: "test",
    onCoreLost: (cb: (info: unknown) => void) => {
      fire = cb;
      return () => { fire = null; };
    },
    ...(options.withRestart === false ? {} : { restartCore }),
  };
  return {
    restartCore,
    emit: (info: unknown) => {
      act(() => fire?.(info));
    },
    hasSubscriber: () => fire !== null,
  };
}

describe("Core 意外退出横幅", () => {
  it("收到 core-lost：顶部出现断连提示，含退出码与存活时长", async () => {
    const bridge = installBridge();
    render(<App />);
    await waitFor(() => expect(__mocks.status).toHaveBeenCalled());
    await waitFor(() => expect(bridge.hasSubscriber()).toBe(true));

    bridge.emit({ code: 1, signal: null, uptimeMs: 123000, restartable: true });

    const banner = await screen.findByRole("alert");
    expect(banner.textContent).toContain("后端 Core 已退出");
    expect(banner.textContent).toContain("退出码 1");
    expect(banner.textContent).toContain("存活 123s");
    // 给出排查线索（日志目录），否则用户只能重启后继续等下次复现
    expect(banner.textContent).toContain(".lite-work/logs");
  });

  it("被信号杀死时优先显示信号名（SIGSEGV 指向原生崩溃）", async () => {
    const bridge = installBridge();
    render(<App />);
    await waitFor(() => expect(bridge.hasSubscriber()).toBe(true));

    bridge.emit({ code: null, signal: "SIGSEGV", uptimeMs: 800, restartable: true });

    const banner = await screen.findByRole("alert");
    expect(banner.textContent).toContain("信号 SIGSEGV");
  });

  it("本地模式点「重启 Core」：调用 restartCore", async () => {
    const user = userEvent.setup();
    const bridge = installBridge();
    render(<App />);
    await waitFor(() => expect(bridge.hasSubscriber()).toBe(true));

    bridge.emit({ code: 1, signal: null, uptimeMs: 5000, restartable: true });
    await user.click(await screen.findByRole("button", { name: "重启 Core" }));

    await waitFor(() => expect(bridge.restartCore).toHaveBeenCalledTimes(1));
  });

  it("远程模式（restartable=false）：不显示重启按钮，避免点了也失败", async () => {
    const bridge = installBridge();
    render(<App />);
    await waitFor(() => expect(bridge.hasSubscriber()).toBe(true));

    bridge.emit({ code: 1, signal: null, uptimeMs: 5000, restartable: false });

    await screen.findByRole("alert");
    expect(screen.queryByRole("button", { name: "重启 Core" })).toBeNull();
  });

  it("重启失败：横幅保留并给出失败原因（不能点完就消失）", async () => {
    const user = userEvent.setup();
    const bridge = installBridge({ restartResult: { ok: false, error: "有 2 个任务运行中" } });
    render(<App />);
    await waitFor(() => expect(bridge.hasSubscriber()).toBe(true));

    bridge.emit({ code: 1, signal: null, uptimeMs: 5000, restartable: true });
    await user.click(await screen.findByRole("button", { name: "重启 Core" }));

    await waitFor(() => expect(bridge.restartCore).toHaveBeenCalled());
    // 横幅仍在
    expect(await screen.findByRole("alert")).toBeTruthy();
    // 失败原因就在横幅里（不能只丢到 currentChat.error —— 没会话时它不渲染）
    expect(await screen.findByText(/重启失败：有 2 个任务运行中/)).toBeTruthy();
    // 按钮恢复可点，允许用户稍后重试
    const btn = screen.getByRole("button", { name: "重启 Core" });
    expect(btn.hasAttribute("disabled")).toBe(false);
  });
});
