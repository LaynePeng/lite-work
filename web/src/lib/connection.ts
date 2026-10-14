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
