// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 全局类型补充：Electron preload 可注入远程 API base（P2 桌面远端模式）。

declare global {
  interface Window {
    /** 远程模式 API 前缀（如 https://core.example.com）；缺省同源直连。 */
    __LITEWORK_API_BASE__?: string;
    /** Electron preload 注入的桥（仅桌面端存在）；key 同 preload.js。 */
    electronAPI?: {
      setTitleBarTheme?: (theme: "classic" | "swiss" | "neubrutalism" | "bauhaus") => void;
      [key: string]: unknown;
    };
  }
}

export {};
