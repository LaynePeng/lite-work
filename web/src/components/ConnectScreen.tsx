// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 连接屏：远程模式令牌录入。任何 API 返回 401 时由 App 渲染（Electron 有
// session 级令牌注入，不会触发）。令牌校验通过后写 localStorage 并整页刷新。

import { useState } from "react";
import { apiUrl, authHeaders, setAuthToken } from "../lib/connection";

export default function ConnectScreen() {
  const [token, setToken] = useState("");
  const [show, setShow] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    const t = token.trim();
    if (!t || connecting) {
      if (!t) setError("请输入访问令牌");
      return;
    }
    setConnecting(true);
    setError(null);
    setAuthToken(t);
    try {
      // 立即校验：免得写入坏令牌后整页刷新又弹回来
      const res = await fetch(apiUrl("/api/status"), { headers: authHeaders() });
      if (res.status === 401) {
        setAuthToken("");
        setError("令牌无效或已更换");
        return;
      }
      if (!res.ok) {
        setError(`服务器响应异常（${res.status}）`);
        return;
      }
      window.location.reload();
    } catch {
      setError("无法连接服务器，请检查网络");
    } finally {
      setConnecting(false);
    }
  };

  return (
    <div className="connect-screen">
      <div className="connect-card">
        <img className="connect-icon" src="/icons/icon-192.png" alt="lite-work" />
        <h1>连接到 lite-work</h1>
        <p className="connect-origin">{apiUrl("").replace(/^https?:\/\//, "") || window.location.host}</p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <div className="connect-input-row">
            <input
              type={show ? "text" : "password"}
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="粘贴访问令牌"
              autoComplete="off"
              autoCapitalize="off"
              autoCorrect="off"
              spellCheck={false}
              autoFocus
            />
            <button
              type="button"
              className="connect-eye"
              onClick={() => setShow((v) => !v)}
              title={show ? "隐藏令牌" : "显示令牌"}
            >
              {show ? "🙈" : "👁"}
            </button>
          </div>
          {error && <div className="connect-error">{error}</div>}
          <button className="connect-submit" type="submit" disabled={connecting}>
            {connecting ? "连接中…" : "连接"}
          </button>
        </form>
        <p className="connect-hint">
          令牌在服务器上：<code>docker exec lite-work-core cat /data/.lite-work/server-token</code>
        </p>
      </div>
    </div>
  );
}
