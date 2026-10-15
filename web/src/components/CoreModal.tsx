// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 「Core 连接」弹窗：本地 / 已保存远程列表 + 添加表单。
// - Electron：点选即切换（core-switch 写 client.json 后应用自动重启）；
// - 浏览器/PWA（Linux Core 等）：无 Electron IPC，改为展示本 Core 的
//   配对二维码（/api/pairing），手机扫码即加入。

import { useCallback, useEffect, useState } from "react";
import QRCode from "qrcode";
import type { CoreEntry, CoreModeInfo } from "../types";
import { apiUrl, authHeaders } from "../lib/connection";

function CoreLogo({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" className="core-logo" aria-hidden>
      <defs>
        <linearGradient id="coreGrad" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#4f8cff" />
          <stop offset="1" stopColor="#8b5cf6" />
        </linearGradient>
      </defs>
      <circle cx="12" cy="12" r="9" fill="none" stroke="url(#coreGrad)" strokeWidth="2.5" />
      <circle cx="12" cy="12" r="4" fill="none" stroke="#3fb950" strokeWidth="2.5" />
    </svg>
  );
}

/** 浏览器/PWA 形态：本 Core 配对二维码（手机扫码加入）。 */
function PairingSection() {
  const [origin, setOrigin] = useState("");
  const [token, setToken] = useState("");
  const [qr, setQr] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const res = await fetch(apiUrl("/api/pairing"), { headers: authHeaders() });
        if (!res.ok) { setError(res.status === 401 ? "鉴权失败" : `服务器响应异常（${res.status}）`); return; }
        const data = (await res.json()) as { origin: string; token: string };
        setOrigin(data.origin);
        setToken(data.token);
        if (data.token) {
          const url = `${data.origin}/#litework-token=${data.token}`;
          setQr(await QRCode.toDataURL(url, { margin: 1, width: 240 }));
        }
      } catch {
        setError("无法获取配对信息");
      }
    })();
  }, []);

  if (error) return <div className="core-connect-error">⚠ {error}</div>;
  return (
    <div className="core-pairing">
      <div className="core-pairing-qr">
        {qr ? <img src={qr} alt="手机配对二维码" /> : <div className="phone-connect-qr-loading">二维码生成中…</div>}
      </div>
      <p className="core-pairing-desc">
        手机相机扫码 → 打开配对页自动连接 → 建议「分享 → 添加到主屏幕」。
        {origin && <>当前 Core：<code>{origin.replace(/^https?:\/\//, "")}</code></>}
      </p>
    </div>
  );
}

export default function CoreModal({ onClose }: { onClose: () => void }) {
  const [info, setInfo] = useState<CoreModeInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 重启倒计时：core-switch 成功后应用即将 relaunch，期间锁 UI
  const [restarting, setRestarting] = useState(false);

  // 添加表单
  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [token, setToken] = useState("");
  const [formBusy, setFormBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const inElectron = !!window.liteWork?.getCoreMode;

  const refresh = useCallback(async () => {
    if (!inElectron) return;
    try {
      setInfo(await window.liteWork!.getCoreMode());
    } catch (e) {
      setError((e as Error).message);
    }
  }, [inElectron]);

  useEffect(() => { void refresh(); }, [refresh]);

  const currentUrl = info?.mode === "remote" ? info.url : "";

  const switchTo = async (targetUrl: string) => {
    if (!info || busy || restarting) return;
    if (targetUrl === currentUrl) { onClose(); return; }
    if (!window.confirm(targetUrl ? "切换到该远程 Core？应用将自动重启。" : "切回本地 Core？应用将自动重启。")) return;
    setBusy(true);
    setError(null);
    try {
      const r = await window.liteWork!.coreSwitch(targetUrl);
      if (!r.ok) { setError(r.error ?? "切换失败"); return; }
      setRestarting(true);
      // relaunch 已在主进程排程；这里只提示，窗口会随应用退出关闭
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  /** 探测远程可达性与令牌有效性：/api/health（免鉴权）+ 401 区分 */
  const probe = async (base: string, tok: string): Promise<string | null> => {
    try {
      const health = await fetch(`${base}/api/health`, { signal: AbortSignal.timeout(8000) });
      if (!health.ok) return `服务器响应异常（${health.status}）`;
      const res = await fetch(`${base}/api/status`, {
        headers: { Authorization: `Bearer ${tok}` },
        signal: AbortSignal.timeout(8000),
      });
      if (res.status === 401) return "令牌无效或已更换";
      if (!res.ok) return `服务器响应异常（${res.status}）`;
      return null;
    } catch {
      return "无法连接服务器，请检查网络与地址";
    }
  };

  const addEntry = async () => {
    if (formBusy) return;
    const n = name.trim();
    const u = url.trim().replace(/\/+$/, "");
    const t = token.trim();
    if (!n) { setFormError("请填写名称"); return; }
    if (!u) { setFormError("请填写服务器地址"); return; }
    if (!t) { setFormError("请填写访问令牌"); return; }
    setFormBusy(true);
    setFormError(null);
    try {
      const probeErr = await probe(u, t);
      if (probeErr) { setFormError(probeErr); return; }
      const r = await window.liteWork!.coreAdd({ name: n, url: u, token: t });
      if (!r.ok) { setFormError(r.error ?? "保存失败"); return; }
      setName(""); setUrl(""); setToken(""); setShowForm(false);
      await refresh();
    } catch (e) {
      setFormError((e as Error).message);
    } finally {
      setFormBusy(false);
    }
  };

  const removeEntry = async (entry: CoreEntry) => {
    if (busy || restarting) return;
    if (!window.confirm(`移除「${entry.name}」？${entry.url === currentUrl ? "（这是当前连接，移除后将回到本地）" : ""}`)) return;
    setBusy(true);
    try {
      await window.liteWork!.coreRemove(entry.url);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={restarting ? undefined : onClose}>
      <div className="modal core-connect-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>
            <CoreLogo /> Core 连接
            {info && (
              <span className="core-current-badge">
                当前：{info.mode === "local" ? "本地" : info.label}
              </span>
            )}
          </h2>
          <button className="modal-close" onClick={onClose} disabled={restarting} title="关闭">✕</button>
        </div>
        <div className="modal-body core-connect-body">
          {error && <div className="core-connect-error">⚠ {error}</div>}
          {restarting && (
            <div className="core-connect-restart">正在切换连接，应用即将重启…</div>
          )}
          {!inElectron ? (
            /* 浏览器/PWA：本 Core 配对二维码（手机扫码加入），无切换语义 */
            <PairingSection />
          ) : !info ? (
            <div className="core-connect-hint">读取配置中…</div>
          ) : (
            <>
              <button
                className={`core-opt ${info.mode === "local" ? "sel" : ""}`}
                disabled={busy || restarting}
                onClick={() => void switchTo("")}
              >
                <span className="core-opt-icon local">
                  <svg viewBox="0 0 24 24" aria-hidden>
                    <rect x="3" y="5" width="18" height="12" rx="2" fill="none" stroke="#3fb950" strokeWidth="2" />
                    <path d="M8 5V3h8v2" fill="none" stroke="#3fb950" strokeWidth="2" />
                  </svg>
                </span>
                <span className="core-opt-text">
                  <span className="core-opt-title">本地 Core</span>
                  <span className="core-opt-sub">本机运行 · 数据在本机</span>
                </span>
                <span className="core-radio" />
              </button>

              {info.cores.map((c) => (
                <div
                  key={c.url}
                  className={`core-opt ${c.url === currentUrl ? "sel" : ""}`}
                  role="button"
                  tabIndex={0}
                  onClick={() => void switchTo(c.url)}
                  onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") void switchTo(c.url); }}
                >
                  <span className="core-opt-icon remote">
                    <svg viewBox="0 0 24 24" aria-hidden>
                      <path d="M7 18a4 4 0 0 1 0-8 5.5 5.5 0 0 1 10.7-1.5A4 4 0 0 1 17 18H7z"
                        fill="none" stroke="#79c0ff" strokeWidth="2" />
                    </svg>
                  </span>
                  <span className="core-opt-text">
                    <span className="core-opt-title">{c.name}</span>
                    <span className="core-opt-sub">{c.url}</span>
                  </span>
                  <button
                    className="core-opt-del"
                    disabled={busy || restarting}
                    title="移除"
                    onClick={(e) => { e.stopPropagation(); void removeEntry(c); }}
                  >
                    🗑
                  </button>
                  <span className="core-radio" />
                </div>
              ))}

              {showForm ? (
                <div className="core-add-form">
                  <label>添加远程 Core</label>
                  <input
                    className="form-input"
                    placeholder="名称（如：公司服务器）"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    disabled={formBusy}
                  />
                  <input
                    className="form-input"
                    placeholder="https://litework.your-tailnet.ts.net"
                    value={url}
                    onChange={(e) => setUrl(e.target.value)}
                    disabled={formBusy}
                  />
                  <input
                    className="form-input"
                    type="password"
                    placeholder="访问令牌（服务器上 docker logs … | grep 访问令牌）"
                    value={token}
                    onChange={(e) => setToken(e.target.value)}
                    disabled={formBusy}
                  />
                  {formError && <div className="core-connect-error">⚠ {formError}</div>}
                  <div className="core-add-form-actions">
                    <button className="btn-ghost" disabled={formBusy} onClick={() => { setShowForm(false); setFormError(null); }}>
                      取消
                    </button>
                    <button className="btn-primary" disabled={formBusy} onClick={() => void addEntry()}>
                      {formBusy ? "验证中…" : "验证并添加"}
                    </button>
                  </div>
                </div>
              ) : (
                <button className="core-add-toggle" disabled={busy || restarting} onClick={() => setShowForm(true)}>
                  ＋ 添加远程 Core…
                </button>
              )}
            </>
          )}
        </div>
        <div className="modal-note">
          {inElectron
            ? <>点选即切换（验证后写配置并重启应用）；<b>远程模式下文件、终端与审批都发生在服务器端</b>，仅连接可信来源。</>
            : <>手机扫码加入本 Core；已加入过的 Core 在手机「Core」列表里长期保存，点选即连、无需重复扫码。</>}
        </div>
      </div>
    </div>
  );
}
