// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// PWA：订阅状态机与订阅/退订流程（serviceWorker / Notification / fetch 全部打桩）。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getPushState, registerServiceWorker, subscribePush, unsubscribePush } from "./pwa";

function fakeSubcription() {
  return {
    endpoint: "https://push.example.com/sub/123",
    toJSON: () => ({ endpoint: "https://push.example.com/sub/123", keys: { p256dh: "k1", auth: "k2" } }),
    unsubscribe: vi.fn(async () => true),
  };
}

function fakeRegistration(existing = true) {
  return {
    pushManager: {
      getSubscription: vi.fn(async () => (existing ? fakeSubcription() : null)),
      subscribe: vi.fn(async () => fakeSubcription()),
    },
  };
}

describe("pwa", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("Notification", { permission: "granted", requestPermission: vi.fn() });
    // jsdom 两者都没有：PushManager 是 pwa.ts 的能力探测目标
    vi.stubGlobal("PushManager", class PushManager {});
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    Object.defineProperty(window.navigator, "serviceWorker", { value: undefined, configurable: true });
  });

  function stubServiceWorker(reg: unknown) {
    Object.defineProperty(window.navigator, "serviceWorker", {
      value: {
        getRegistration: vi.fn(async () => reg),
        register: vi.fn(async () => reg),
      },
      configurable: true,
    });
  }

  it("registerServiceWorker：file:// 下不注册（Electron loadFile 场景）", async () => {
    // jsdom 默认 about:blank，协议即非 http(s)
    const reg = await registerServiceWorker();
    expect(reg).toBeNull();
  });

  it("getPushState：无订阅 → off；有订阅 → on", async () => {
    window.liteWork = undefined;
    stubServiceWorker(fakeRegistration(false));
    expect(await getPushState()).toBe("off");
    stubServiceWorker(fakeRegistration(true));
    expect(await getPushState()).toBe("on");
  });

  it("getPushState：权限被拒 → denied（优先级高于订阅态）", async () => {
    vi.stubGlobal("Notification", { permission: "denied" });
    stubServiceWorker(fakeRegistration(true));
    expect(await getPushState()).toBe("denied");
  });

  it("subscribePush：已有订阅 → 直接补登记（POST /api/push/subscribe）", async () => {
    stubServiceWorker(fakeRegistration(true));
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }));

    const r = await subscribePush();
    expect(r.ok).toBe(true);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/push/subscribe");
    expect(init.method).toBe("POST");
    const body = JSON.parse(init.body as string);
    expect(body.endpoint).toBe("https://push.example.com/sub/123");
    expect(body.keys).toEqual({ p256dh: "k1", auth: "k2" });
  });

  it("subscribePush：通知权限被拒 → 报错且不发起请求", async () => {
    vi.stubGlobal("Notification", {
      permission: "default",
      requestPermission: vi.fn(async () => "denied"),
    });
    stubServiceWorker(fakeRegistration(true));
    const r = await subscribePush();
    expect(r.ok).toBe(false);
    expect(r.error).toContain("拒绝");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("unsubscribePush：先 POST /api/push/unsubscribe 再本地注销", async () => {
    stubServiceWorker(fakeRegistration(true));
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }));

    const r = await unsubscribePush();
    expect(r.ok).toBe(true);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/push/unsubscribe");
    expect(JSON.parse(init.body as string).endpoint).toBe("https://push.example.com/sub/123");
  });
});
