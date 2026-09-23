// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// Esc 停止当前任务（与输入区「停止」按钮同一路径）——防误触为「连按两次」：
// 首次只进入待确认态（提示行「再按一次 Esc 停止任务」），窗口内再按一次才停止；
// 超时/任务结束/切换会话都会解除待确认态；弹窗打开时让位给弹窗自身的 Esc 关闭。

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App, { ESC_STOP_CONFIRM_MS } from "./App";
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
    // 显式声明：本测试要断言它的调用与否，不能依赖 Proxy 懒创建
    stopTask: { ok: true },
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
  for (const [k, v] of Object.entries(__defaults)) {
    __mocks[k].mockReset().mockResolvedValue(v);
  }
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  delete (window as unknown as { liteWork?: unknown }).liteWork;
});

/** 起一个运行中的任务：Alt+N 新建会话 → 输入 → 发送（api.chat 返回 task_id=t1）。 */
async function startRunningTask(user: ReturnType<typeof userEvent.setup>) {
  await user.keyboard("{Alt>}n{/Alt}");
  const input = await screen.findByPlaceholderText(/下达任务/);
  await user.type(input, "你好");
  await user.click(screen.getByRole("button", { name: "➤" }));
  await waitFor(() => expect(__mocks.chat).toHaveBeenCalledTimes(1));
  // 运行中：输入区出现「停止任务」按钮
  await screen.findByTitle("停止任务");
}

describe("Esc 停止当前任务（连按两次）", () => {
  it("连按两次 Esc：第二次才真正停止后端任务", async () => {
    const user = userEvent.setup();
    render(<App />);
    await waitFor(() => expect(__mocks.status).toHaveBeenCalled());
    await startRunningTask(user);

    // 第一次：只进入待确认态，提示行给出「再按一次」
    await user.keyboard("{Escape}");
    expect(await screen.findByText(/再按一次 Esc 停止任务/)).toBeInTheDocument();
    expect(__mocks.stopTask).not.toHaveBeenCalled();

    // 第二次（窗口内）：真正停止
    await user.keyboard("{Escape}");
    await waitFor(() => expect(__mocks.stopTask).toHaveBeenCalledWith("t1"));
  });

  it("只按一次 Esc：不停止任务", async () => {
    const user = userEvent.setup();
    render(<App />);
    await waitFor(() => expect(__mocks.status).toHaveBeenCalled());
    await startRunningTask(user);

    await user.keyboard("{Escape}");

    // 等待一拍：确认不是「延迟停止」
    await act(async () => { await Promise.resolve(); });
    expect(__mocks.stopTask).not.toHaveBeenCalled();
  });

  it("极快连按两次（同一 tick）：也能停止（不依赖 effect 重新注册监听器）", async () => {
    const user = userEvent.setup();
    render(<App />);
    await waitFor(() => expect(__mocks.status).toHaveBeenCalled());
    await startRunningTask(user);

    act(() => {
      fireEvent.keyDown(window, { key: "Escape" });
      fireEvent.keyDown(window, { key: "Escape" });
    });

    await waitFor(() => expect(__mocks.stopTask).toHaveBeenCalledWith("t1"));
  });

  it("两次 Esc 间隔超过确认窗口：待确认态自动解除，不停止任务", async () => {
    const user = userEvent.setup();
    render(<App />);
    await waitFor(() => expect(__mocks.status).toHaveBeenCalled());
    await startRunningTask(user);

    // 只在「按键 → 超时 → 再按键」这段用假定时器，避免影响渲染期的真实异步
    vi.useFakeTimers();
    try {
      fireEvent.keyDown(window, { key: "Escape" });
      expect(screen.getByText(/再按一次 Esc 停止任务/)).toBeInTheDocument();

      act(() => { vi.advanceTimersByTime(ESC_STOP_CONFIRM_MS + 50); });
      expect(screen.queryByText(/再按一次 Esc 停止任务/)).toBeNull();

      // 超时后这次只是重新武装，仍然不停止
      fireEvent.keyDown(window, { key: "Escape" });
      expect(__mocks.stopTask).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("空闲（无运行任务）按 Esc：不请求停止", async () => {
    const user = userEvent.setup();
    render(<App />);
    await waitFor(() => expect(__mocks.status).toHaveBeenCalled());

    await user.keyboard("{Escape}");
    await user.keyboard("{Escape}");

    expect(__mocks.stopTask).not.toHaveBeenCalled();
  });

  it("「关于」弹窗打开时按 Esc：只关闭弹窗，不打断任务", async () => {
    let showAbout: (() => void) | null = null;
    (window as unknown as { liteWork?: unknown }).liteWork = {
      onShowAbout: (cb: () => void) => {
        showAbout = cb;
        return () => {};
      },
    };
    const user = userEvent.setup();
    render(<App />);
    await waitFor(() => expect(__mocks.status).toHaveBeenCalled());
    await startRunningTask(user);

    // 桌面菜单「关于」→ 打开弹窗（此处直接触发 preload 回调）
    await waitFor(() => expect(showAbout).not.toBeNull());
    act(() => showAbout?.());
    expect(await screen.findByText("关于 lite-work")).toBeTruthy();

    // 弹窗打开时的 Esc 被弹窗吃掉（不武装）；关掉后再按一次也只是武装
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByText("关于 lite-work")).toBeNull());
    await user.keyboard("{Escape}");

    expect(__mocks.stopTask).not.toHaveBeenCalled();
  });
});
