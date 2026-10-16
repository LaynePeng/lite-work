// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 连接屏：远程模式令牌录入。任何 API 返回 401 时由 App 渲染（Electron 有
// session 级令牌注入，不会触发）。令牌校验通过后写 localStorage 并整页刷新。
//
// 已配对过的 Core 列表也在这里展示（localStorage 记忆）：点选即免扫码直连。

import { useState } from "react";
import { apiUrl, authHeaders, forgetCore, pairedCores, rememberCore, setAuthToken } from "../lib/connection";

export default function ConnectScreen() {
  const [token, setToken] = useState("");
  const [show, setShow] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 当前 origin 已配对过的令牌：401 后一键重连，不必重新粘贴（此前这里刻意
  // 把同源项过滤掉了，导致「明明配对过却只能手抄令牌」）。
  const sameOrigin = pairedCores().find((c) => c.origin === window.location.origin);
  const saved = pairedCores().filter((c) => c.origin !== window.location.origin);

  /** 连接已保存的 Core：跳转其他 origin（带 token hash，配对后自动剥除）。 */
  const useSaved = (origin: string, t: string) => {
    window.location.href = `${origin}/#litework-token=${t}`;
  };

  /** 用给定令牌连接本 Core：校验通过才落盘并刷新。 */
  const connectWith = async (t: string) => {
    if (!t || connecting) return;
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
      // 连接成功：记住本 Core，下次免输令牌
      rememberCore(window.location.origin, t);
      window.location.reload();
    } catch {
      setError("无法连接服务器，请检查网络");
    } finally {
      setConnecting(false);
    }
  };

  const submit = () => {
    const t = token.trim();
    if (!t) {
      setError("请输入访问令牌");
      return;
    }
    void connectWith(t);
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
        {sameOrigin && (
          <div className="connect-saved">
            <div className="connect-saved-title">本机已配对过这台 Core</div>
            <button
              className="connect-saved-btn connect-same-origin"
              disabled={connecting}
              onClick={() => void connectWith(sameOrigin.token)}
            >
              <span className="connect-saved-name">🔗 一键重连</span>
              <span className="connect-saved-host">免输令牌（上次配对记录）</span>
            </button>
          </div>
        )}
        {saved.length > 0 && (
          <div className="connect-saved">
            <div className="connect-saved-title">已配对的 Core</div>
            {saved.map((c) => (
              <div key={c.origin} className="connect-saved-item">
                <button className="connect-saved-btn" onClick={() => useSaved(c.origin, c.token)}>
                  <span className="connect-saved-name">{c.name}</span>
                  <span className="connect-saved-host">{c.origin.replace(/^https?:\/\//, "")}</span>
                </button>
                <button
                  className="connect-saved-del"
                  title="忘记此 Core"
                  onClick={() => forgetCore(c.origin)}
                >
                  ✕
                </button>
              </div>
            ))}
          </div>
        )}
        <p className="connect-hint">
          令牌在服务器上：<code>docker exec lite-work-core cat /data/.lite-work/server-token</code>
        </p>
      </div>
    </div>
  );
}
