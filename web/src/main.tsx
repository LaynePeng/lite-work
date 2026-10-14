// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import ErrorBoundary from "./components/ErrorBoundary";
import { registerServiceWorker } from "./lib/pwa";
import { consumeTokenFromUrl } from "./lib/connection";
import "highlight.js/styles/github-dark.css";
import "./styles.css";

// 扫码配对：首帧前把 #litework-token=xxx 落盘并从地址栏剥除
consumeTokenFromUrl();

window.addEventListener("unhandledrejection", (e) => {
  console.error("[全局] 未处理的 Promise 拒绝:", e.reason);
});
window.addEventListener("error", (e) => {
  console.error("[全局] 运行时错误:", e.error || e.message);
});

// PWA：注册 Service Worker（Web Push 通道）。Electron/file:// 下自动跳过。
void registerServiceWorker();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>
);