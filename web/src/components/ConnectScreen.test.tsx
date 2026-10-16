// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 连接屏：令牌校验通过写 localStorage 并刷新；401 提示无效不落盘。

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ConnectScreen from "./ConnectScreen";
import { authToken } from "../lib/connection";

describe("ConnectScreen", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let reload: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    localStorage.clear();
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    reload = vi.fn();
    Object.defineProperty(window, "location", {
      value: { ...window.location, host: "core.example.com", reload },
      writable: true,
    });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it("空令牌提交：提示输入，不发起请求", async () => {
    render(<ConnectScreen />);
    fireEvent.click(screen.getByRole("button", { name: "连接" }));
    await waitFor(() => expect(screen.getByText("请输入访问令牌")).toBeInTheDocument());
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("令牌无效（401）：提示无效、清空 localStorage、不刷新", async () => {
    fetchMock.mockResolvedValue(new Response("{}", { status: 401 }));
    render(<ConnectScreen />);

    fireEvent.change(screen.getByPlaceholderText("粘贴访问令牌"), { target: { value: "bad-token" } });
    fireEvent.click(screen.getByRole("button", { name: "连接" }));

    await waitFor(() => expect(screen.getByText("令牌无效或已更换")).toBeInTheDocument());
    expect(reload).not.toHaveBeenCalled();
    expect(authToken()).toBe("");
  });

  it("令牌有效（200）：写入 localStorage 并整页刷新", async () => {
    fetchMock.mockResolvedValue(new Response("{}", { status: 200 }));
    render(<ConnectScreen />);

    fireEvent.change(screen.getByPlaceholderText("粘贴访问令牌"), { target: { value: "good-token" } });
    fireEvent.click(screen.getByRole("button", { name: "连接" }));

    await waitFor(() => expect(reload).toHaveBeenCalled());
    expect(authToken()).toBe("good-token");
  });

  it("同源已配对：显示「一键重连」，点击用保存的令牌连接（免重新粘贴）", async () => {
    localStorage.setItem("litework.paired.cores", JSON.stringify([
      { name: "core.example.com", origin: window.location.origin, token: "saved-token", at: 1 },
    ]));
    fetchMock.mockResolvedValue(new Response("{}", { status: 200 }));
    render(<ConnectScreen />);

    fireEvent.click(screen.getByText(/一键重连/));

    await waitFor(() => expect(reload).toHaveBeenCalled());
    expect(authToken()).toBe("saved-token");
    const [, init] = fetchMock.mock.calls[0]!;
    expect((init?.headers as Record<string, string>)?.Authorization).toBe("Bearer saved-token");
  });
});
