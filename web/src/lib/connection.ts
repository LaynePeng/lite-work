// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 连接参数（远程/浏览器直连场景）：API base 与 Bearer 令牌。
//
// 桌面本地模式：UI 由 Core 同源伺服，base 为空（相对路径），令牌由 Electron
// 主进程按 origin 注入（electron/token-injector.js），这里全部不生效。
// 浏览器/PWA 远程模式：同为同源，但没有注入层——登录后令牌写入 localStorage，
// 由 authHeaders() 统一带上；API base 是异源部署的逃生口（preload 注入
// window.__LITEWORK_API_BASE__）。

const TOKEN_KEY = "litework.auth.token";

export function apiBase(): string {
  const base = (typeof window !== "undefined" && window.__LITEWORK_API_BASE__) || "";
  return base.replace(/\/+$/, "");
}

/** 相对路径 → 完整 URL；base 未配置时保持相对路径（同源直连）。 */
export function apiUrl(path: string): string {
  return `${apiBase()}${path}`;
}

export function authToken(): string {
  try {
    return window.localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return ""; // Safari 隐私模式等 localStorage 不可用的环境
  }
}

export function setAuthToken(token: string): void {
  try {
    if (token) window.localStorage.setItem(TOKEN_KEY, token);
    else window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* 写不进则退化为无令牌请求（后端 401，登录流程可重试） */
  }
}

/** 统一请求头：有令牌才带 Authorization。 */
export function authHeaders(): Record<string, string> {
  const token = authToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/** 是否运行在 Electron 桌面壳内（preload 注入 window.liteWork）。 */
export function isElectron(): boolean {
  return typeof window !== "undefined" && !!window.liteWork;
}

// ---------------------------------------------------------------- 已配对 Core（手机/浏览器）
// 扫码/输入令牌配对过的 Core 存 localStorage（origin+令牌），下次免扫码直连。

const CORES_KEY = "litework.paired.cores";

export interface PairedCore {
  /** 显示名：默认 host */
  name: string;
  origin: string;
  token: string;
  /** 最近一次连接时间（ms） */
  at: number;
}

export function pairedCores(): PairedCore[] {
  try {
    const raw = window.localStorage.getItem(CORES_KEY);
    const list = raw ? (JSON.parse(raw) as PairedCore[]) : [];
    return Array.isArray(list) ? list : [];
  } catch {
    return [];
  }
}

/** 记住（或刷新）一个已配对 Core：同 origin 覆盖，并置为最近使用。 */
export function rememberCore(origin: string, token: string, name?: string): void {
  try {
    const base = origin.replace(/\/+$/, "");
    const host = base.replace(/^https?:\/\//, "");
    const list = pairedCores().filter((c) => c.origin !== base);
    list.unshift({ name: name || host, origin: base, token, at: Date.now() });
    window.localStorage.setItem(CORES_KEY, JSON.stringify(list.slice(0, 12)));
  } catch {
    /* localStorage 不可用（隐私模式）：只影响记忆，不影响连接 */
  }
}

export function forgetCore(origin: string): void {
  try {
    window.localStorage.setItem(
      CORES_KEY, JSON.stringify(pairedCores().filter((c) => c.origin !== origin)));
  } catch { /* ignore */ }
}

/** 401 广播：浏览器/PWA 无有效令牌时由 App 弹连接屏。
 *  Electron 本地模式有 session 级令牌注入，永远不会触发。 */
export const UNAUTHORIZED_EVENT = "litework:unauthorized";

/** 扫码配对链接（#litework-token=xxx）：取出令牌写 localStorage 并从地址栏
 *  剥除（不进历史，避免二次分享泄露）。应在应用首帧前调用一次。 */
export function consumeTokenFromUrl(): void {
  if (typeof window === "undefined") return;
  const m = window.location.hash.match(/litework-token=([A-Za-z0-9._-]+)/);
  if (!m) return;
  setAuthToken(m[1]);
  // 扫码配对成功：记住本 Core，下次在连接屏免扫码直连
  rememberCore(window.location.origin, m[1]);
  try {
    history.replaceState(null, "", window.location.pathname + window.location.search);
  } catch {
    /* replaceState 不可用的环境：令牌留在 hash，刷新后重新消费，无害 */
  }
}

let lastUnauthorizedAt = 0;

export function notifyUnauthorized(): void {
  // 页面加载时多个并发请求同时 401，只广播一次
  if (Date.now() - lastUnauthorizedAt < 3000) return;
  lastUnauthorizedAt = Date.now();
  window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
}
