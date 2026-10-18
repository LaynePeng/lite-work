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
import type { LanShareInfo } from "../types";

const getLanShare = vi.fn();
const setLanShare = vi.fn();
const restartCore = vi.fn();
const probeLanShare = vi.fn();

/** 主进程 get-lan-share 的默认返回（applied=true：进程确实按共享参数起来了）。 */
const lanInfo = (over: Partial<LanShareInfo> = {}): LanShareInfo => ({
  enabled: true,
  localCore: true,
  applied: true,
  url: "http://192.168.1.5:51234",
  token: "tok-abc_123",
  ips: ["192.168.1.5"],
  addresses: [{ ip: "192.168.1.5", iface: "en0", kind: "lan", label: "网络" }],
  port: 51234,
  ...over,
});

function mockBridge(platform = "win32") {
  (window as unknown as { liteWork?: unknown }).liteWork = {
    platform,
    getLanShare,
    setLanShare,
    restartCore,
    probeLanShare,
  };
}

describe("PhoneConnectModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockBridge();
    probeLanShare.mockResolvedValue({ ok: true, target: "http://192.168.1.5:51234", version: "2.0.1" });
  });
  afterEach(() => {
    delete (window as unknown as { liteWork?: unknown }).liteWork;
  });

  it("未开启：说明与风险提示；点击开启 → setLanShare(true) + 重启提示", async () => {
    // 有状态 mock：setLanShare 落配置后，后续 getLanShare 反映新状态
    let enabled = false;
    getLanShare.mockImplementation(async () => (
      enabled ? lanInfo() : lanInfo({ enabled: false, applied: false, url: "", token: "", ips: [], addresses: [] })
    ));
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
    getLanShare.mockResolvedValue(lanInfo({
      ips: ["192.168.1.5", "10.0.0.3"],
      addresses: [
        { ip: "192.168.1.5", iface: "en0", kind: "lan", label: "网络" },
        { ip: "10.0.0.3", iface: "en1", kind: "lan", label: "网络" },
      ],
    }));
    render(<PhoneConnectModal onClose={vi.fn()} />);

    const img = await screen.findByAltText("手机连接二维码");
    // toDataURL 的入参被编码进 mock 产出的 data URL，借此断言配对链接正确
    await waitFor(() =>
      expect(img.getAttribute("src")).toContain(btoa("http://192.168.1.5:51234/#litework-token=tok-abc_123"))
    );
    expect(screen.getByText("http://192.168.1.5:51234")).toBeInTheDocument();
    // 双网段：两个 IP 都展示为 chip
    expect(screen.getByRole("button", { name: /10\.0\.0\.3/ })).toBeInTheDocument();
  });

  it("双网段：点选第二个 IP，URL 与二维码跟随切换", async () => {
    getLanShare.mockResolvedValue(lanInfo({
      ips: ["192.168.1.5", "10.0.0.3"],
      addresses: [
        { ip: "192.168.1.5", iface: "en0", kind: "lan", label: "网络" },
        { ip: "10.0.0.3", iface: "en1", kind: "lan", label: "网络" },
      ],
    }));
    render(<PhoneConnectModal onClose={vi.fn()} />);

    await screen.findByText("http://192.168.1.5:51234");
    fireEvent.click(screen.getByRole("button", { name: /10\.0\.0\.3/ }));

    expect(await screen.findByText("http://10.0.0.3:51234")).toBeInTheDocument();
    const img = screen.getByAltText("手机连接二维码");
    await waitFor(() =>
      expect(img.getAttribute("src")).toContain(btoa("http://10.0.0.3:51234/#litework-token=tok-abc_123"))
    );
  });

  it("开关已存但 Core 未按共享参数重启：不给二维码，改提示重启", async () => {
    // 最坑的失败模式：配置 enabled=true 而进程仍绑 127.0.0.1，
    // 此时渲染出来的二维码**不可能连通**——必须挡住。
    getLanShare.mockResolvedValue(lanInfo({ applied: false }));
    render(<PhoneConnectModal onClose={vi.fn()} />);

    expect(await screen.findByText(/仍是仅本机可访问/)).toBeInTheDocument();
    expect(screen.queryByAltText("手机连接二维码")).not.toBeInTheDocument();
  });

  it("写入失败：展示错误信息", async () => {
    getLanShare.mockResolvedValue(lanInfo({ enabled: false, applied: false, url: "", token: "", ips: [], addresses: [] }));
    setLanShare.mockResolvedValue({ ok: false, error: "disk full" });
    render(<PhoneConnectModal onClose={vi.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: "开启局域网共享" }));
    expect(await screen.findByText(/disk full/)).toBeInTheDocument();
  });

  it("远程 Core 直连（localCore=false）：提示不适用、无开关", async () => {
    getLanShare.mockResolvedValue(lanInfo({
      enabled: false, localCore: false, applied: false, url: "", token: "", ips: [], addresses: [],
    }));
    render(<PhoneConnectModal onClose={vi.fn()} />);

    expect(await screen.findByText(/远程 Core/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "开启局域网共享" })).not.toBeInTheDocument();
  });

  it("从开启切回关闭：仍提示需重启（旧绑定重启前持续生效）", async () => {
    let enabled = true;
    getLanShare.mockImplementation(async () => (
      enabled
        ? lanInfo()
        : lanInfo({ enabled: false, applied: true })  // 进程仍按共享参数在跑
    ));
    setLanShare.mockImplementation(async (v: boolean) => {
      enabled = v;
      return { ok: true };
    });
    render(<PhoneConnectModal onClose={vi.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: "关闭局域网共享" }));
    await waitFor(() => expect(setLanShare).toHaveBeenCalledWith(false));
    // 关闭同样要重启才生效：横幅必须还在（否则用户误以为已断开）
    expect(await screen.findByText(/仍接受局域网连接/)).toBeInTheDocument();
  });

  it("iPhone 热点网段被标为「热点」并默认选中（192.168 排其后）", async () => {
    getLanShare.mockResolvedValue(lanInfo({
      url: "http://172.20.10.2:8787",
      ips: ["172.20.10.2", "192.168.1.5"],
      addresses: [
        { ip: "172.20.10.2", iface: "en7", kind: "hotspot", label: "热点" },
        { ip: "192.168.1.5", iface: "en0", kind: "lan", label: "网络" },
      ],
      port: 8787,
    }));
    render(<PhoneConnectModal onClose={vi.fn()} />);

    // 默认选中热点：二维码里编的必须是 iPhone 够得着的那个地址
    const img = await screen.findByAltText("手机连接二维码");
    await waitFor(() =>
      expect(img.getAttribute("src")).toContain(btoa("http://172.20.10.2:8787/#litework-token=tok-abc_123"))
    );
    expect(screen.getByText("热点")).toBeInTheDocument();
  });

  it("检测连接：可达显示 ✓ 与 Core 版本；不可达显示原因", async () => {
    getLanShare.mockResolvedValue(lanInfo());
    const { unmount } = render(<PhoneConnectModal onClose={vi.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: "检测连接" }));
    expect(await screen.findByText(/192\.168\.1\.5 可达/)).toBeInTheDocument();
    expect(probeLanShare).toHaveBeenCalledWith("192.168.1.5");

    // 不可达：把主进程给的结论原样展示（含防火墙/热点隔离的提示）
    probeLanShare.mockResolvedValue({
      ok: false, reason: "unreachable",
      error: "无法从 192.168.1.5 连到 Core（超时）——多半是系统防火墙未放行，或热点不允许设备互访",
    });
    unmount();
    render(<PhoneConnectModal onClose={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "检测连接" }));
    expect(await screen.findByText(/系统防火墙未放行/)).toBeInTheDocument();
  });

  it("防火墙文案按平台分支：macOS 不提 Windows 的「专用网络」", async () => {
    delete (window as unknown as { liteWork?: unknown }).liteWork;
    mockBridge("darwin");
    getLanShare.mockResolvedValue(lanInfo({ enabled: false, applied: false, url: "", token: "", ips: [], addresses: [] }));
    render(<PhoneConnectModal onClose={vi.fn()} />);

    expect(await screen.findByText(/是否允许接受传入网络连接/)).toBeInTheDocument();
    expect(screen.queryByText(/专用网络/)).not.toBeInTheDocument();
  });
});
