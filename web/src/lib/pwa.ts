// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// PWA：Service Worker 注册 + Web Push 订阅（远程/PWA 场景）。
// Electron 本地模式不注册（桌面通知走原生通道，且 file:// 下 SW 不可用）。

import { apiUrl, authHeaders, isElectron } from "./connection";

/** 是否以「已安装的 PWA」形态运行（standalone）。 */
export function isStandalone(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  const nav = window.navigator as Navigator & { standalone?: boolean };
  return window.matchMedia("(display-mode: standalone)").matches || nav.standalone === true;
}

export async function registerServiceWorker(): Promise<ServiceWorkerRegistration | null> {
  if (typeof window === "undefined" || !("serviceWorker" in navigator)) return null;
  // file://（Electron loadFile）与 Electron 壳内一律不注册
  if (isElectron() || !/^https?:$/.test(window.location.protocol)) return null;
  try {
    return await navigator.serviceWorker.register("/sw.js");
  } catch (err) {
    console.warn("[PWA] Service Worker 注册失败:", err);
    return null;
  }
}

/** base64url（VAPID 公钥）→ applicationServerKey 需要的 Uint8Array。 */
function urlB64ToUint8Array(base64: string): Uint8Array<ArrayBuffer> {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const b64 = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(b64);
  const arr = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
  return arr;
}

export type PushState = "unsupported" | "insecure" | "denied" | "off" | "on";

/** 当前订阅状态（bell 图标四态：不支持 / 非安全上下文 / 已拒 / 关 / 开）。 */
export async function getPushState(): Promise<PushState> {
  if (typeof window === "undefined" || !("serviceWorker" in navigator) || !("PushManager" in window)) {
    return "unsupported";
  }
  // 局域网 http 直连等非安全上下文：Service Worker 注册必被浏览器拒绝。
  // 严格 === false：jsdom 等未定义该属性的环境不做误判（它们通常也缺 SW API）
  if (typeof window !== "undefined" && window.isSecureContext === false) return "insecure";
  if (typeof Notification !== "undefined" && Notification.permission === "denied") return "denied";
  try {
    const reg = await navigator.serviceWorker.getRegistration();
    const sub = await reg?.pushManager.getSubscription();
    return sub ? "on" : "off";
  } catch {
    return "off";
  }
}

/** 订阅设备备注名（服务端订阅簿展示用，纯装饰）。 */
export function deviceLabel(): string {
  const ua = navigator.userAgent;
  const isIOS = /iP(hone|ad|od)/.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const name = isIOS ? (/iPad/.test(ua) ? "iPad" : "iPhone") : /Android/.test(ua) ? "Android" : "浏览器";
  return isStandalone() ? `${name} · 主屏应用` : name;
}

export async function subscribePush(): Promise<{ ok: boolean; error?: string }> {
  try {
    if (typeof window === "undefined" || !("serviceWorker" in navigator) || !("PushManager" in window)) {
      return { ok: false, error: "此浏览器不支持推送通知" };
    }
    if (typeof Notification !== "undefined" && Notification.permission !== "granted") {
      const perm = await Notification.requestPermission();
      if (perm !== "granted") {
        return {
          ok: false,
          error: perm === "denied" ? "通知权限已被拒绝，请在系统设置中允许 lite-work 通知" : "未授权通知",
        };
      }
    }
    const reg = await navigator.serviceWorker.getRegistration();
    if (!reg) return { ok: false, error: "Service Worker 未注册，请刷新后重试" };
    // 已有订阅：直接补登记（服务器侧可能已清理，如重启换卷）
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      const keyRes = await fetch(apiUrl("/api/push/vapid-key"), { headers: authHeaders() });
      if (!keyRes.ok) return { ok: false, error: "无法获取推送公钥" };
      const { public_key: publicKey } = (await keyRes.json()) as { public_key: string };
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlB64ToUint8Array(publicKey),
      });
    }
    const json = sub.toJSON();
    const res = await fetch(apiUrl("/api/push/subscribe"), {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ endpoint: json.endpoint ?? "", keys: json.keys ?? {}, device: deviceLabel() }),
    });
    if (!res.ok) return { ok: false, error: "订阅登记失败" };
    return { ok: true };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : String(err) };
  }
}

export async function unsubscribePush(): Promise<{ ok: boolean }> {
  try {
    const reg = await navigator.serviceWorker.getRegistration();
    const sub = await reg?.pushManager.getSubscription();
    if (sub) {
      // 先通知服务器移除（尽力而为），再本地注销
      await fetch(apiUrl("/api/push/unsubscribe"), {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ endpoint: sub.endpoint }),
      }).catch(() => {});
      await sub.unsubscribe();
    }
    return { ok: true };
  } catch {
    return { ok: false };
  }
}
