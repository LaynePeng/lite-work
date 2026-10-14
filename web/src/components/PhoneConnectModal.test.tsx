// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 手机/局域网面板：开关 → setLanShare + 重启提示；已开启 → 连接地址 +
// 配对二维码（QR 内容含令牌）。qrcode 与 Electron bridge 全部 mock。

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";

vi.mock("qrcode", () => ({
  default: {
    toDataURL: vi.fn(async (text: string) => `data:image/png;base64,${btoa(text)}`),
  },
}));

import PhoneConnectModal from "./PhoneConnectModal";

const getLanShare = vi.fn();
const setLanShare = vi.fn();
const restartCore = vi.fn();

function mockBridge() {
  (window as unknown as { liteWork?: unknown }).liteWork = {
    getLanShare,
    setLanShare,
    restartCore,
  };
}

describe("PhoneConnectModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockBridge();
  });
  afterEach(() => {
    delete (window as unknown as { liteWork?: unknown }).liteWork;
  });

  it("未开启：说明与风险提示；点击开启 → setLanShare(true) + 重启提示", async () => {
    // 有状态 mock：setLanShare 落配置后，后续 getLanShare 反映新状态
    let enabled = false;
    getLanShare.mockImplementation(async () => ({
      enabled,
      localCore: true,
      url: enabled ? "http://192.168.1.5:51234" : "",
      token: enabled ? "tok-abc_123" : "",
      ips: ["192.168.1.5"],
    }));
    setLanShare.mockImplementation(async (v: boolean) => {
      enabled = v;
      return { ok: true };
    });
    render(<PhoneConnectModal onClose={vi.fn()} />);

    expect(await screen.findByText(/同一局域网内的手机/)).toBeInTheDocument();
    // 明确提示限制：无推送
    expect(screen.getByText(/没有推送通知/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "开启局域网共享" }));
    await waitFor(() => expect(setLanShare).toHaveBeenCalledWith(true));
    expect(await screen.findByText(/重启 Core 后生效/)).toBeInTheDocument();
  });

  it("已开启：展示连接地址与二维码（QR 内容带配对令牌）", async () => {
    getLanShare.mockResolvedValue({
      enabled: true,
      localCore: true,
      url: "http://192.168.1.5:51234",
      token: "tok-abc_123",
      ips: ["192.168.1.5", "10.0.0.3"],
    });
    render(<PhoneConnectModal onClose={vi.fn()} />);

    const img = await screen.findByAltText("手机连接二维码");
    // toDataURL 的入参被编码进 mock 产出的 data URL，借此断言配对链接正确
    await waitFor(() =>
      expect(img.getAttribute("src")).toContain(btoa("http://192.168.1.5:51234/#litework-token=tok-abc_123"))
    );
    expect(screen.getByText("http://192.168.1.5:51234")).toBeInTheDocument();
    // 双网段：两个 IP 都展示为 chip
    expect(screen.getByRole("button", { name: "10.0.0.3" })).toBeInTheDocument();
  });

  it("双网段：点选第二个 IP，URL 与二维码跟随切换", async () => {
    getLanShare.mockResolvedValue({
      enabled: true,
      localCore: true,
      url: "http://192.168.1.5:51234",
      token: "tok-abc_123",
      ips: ["192.168.1.5", "10.0.0.3"],
    });
    render(<PhoneConnectModal onClose={vi.fn()} />);

    await screen.findByText("http://192.168.1.5:51234");
    fireEvent.click(screen.getByRole("button", { name: "10.0.0.3" }));

    expect(await screen.findByText("http://10.0.0.3:51234")).toBeInTheDocument();
    const img = screen.getByAltText("手机连接二维码");
    await waitFor(() =>
      expect(img.getAttribute("src")).toContain(btoa("http://10.0.0.3:51234/#litework-token=tok-abc_123"))
    );
  });

  it("写入失败：展示错误信息", async () => {
    getLanShare.mockResolvedValue({ enabled: false, localCore: true, url: "", token: "", ips: [] });
    setLanShare.mockResolvedValue({ ok: false, error: "disk full" });
    render(<PhoneConnectModal onClose={vi.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: "开启局域网共享" }));
    expect(await screen.findByText(/disk full/)).toBeInTheDocument();
  });

  it("远程 Core 直连（localCore=false）：提示不适用、无开关", async () => {
    getLanShare.mockResolvedValue({ enabled: false, localCore: false, url: "", token: "", ips: [] });
    render(<PhoneConnectModal onClose={vi.fn()} />);

    expect(await screen.findByText(/远程 Core/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "开启局域网共享" })).not.toBeInTheDocument();
  });

  it("从开启切回关闭：仍提示需重启（旧绑定重启前持续生效）", async () => {
    let enabled = true;
    getLanShare.mockImplementation(async () => ({
      enabled,
      localCore: true,
      url: enabled ? "http://192.168.1.5:51234" : "",
      token: enabled ? "tok-abc_123" : "",
      ips: ["192.168.1.5"],
    }));
    setLanShare.mockImplementation(async (v: boolean) => {
      enabled = v;
      return { ok: true };
    });
    render(<PhoneConnectModal onClose={vi.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: "关闭局域网共享" }));
    await waitFor(() => expect(setLanShare).toHaveBeenCalledWith(false));
    // 关闭同样要重启才生效：横幅必须还在（否则用户误以为已断开）
    expect(await screen.findByText(/重启前仍接受局域网连接/)).toBeInTheDocument();
  });
});
