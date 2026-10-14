// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 「手机 / 局域网连接」面板（仅桌面 Electron 渲染）：
// 开关局域网共享 → 展示内网地址 + 配对二维码。手机相机扫码打开
// http://<内网IP>:<端口>/#litework-token=xxx，前端 consumeTokenFromUrl
// 自动配对，无需手抄令牌。

import { useCallback, useEffect, useState } from "react";
import QRCode from "qrcode";
import type { LanShareInfo } from "../types";

export default function PhoneConnectModal({ onClose }: { onClose: () => void }) {
  const [info, setInfo] = useState<LanShareInfo | null>(null);
  const [qr, setQr] = useState("");
  const [busy, setBusy] = useState(false);
  // 开关已保存但 Core 未重启（绑定参数在启动时生效）
  const [needsRestart, setNeedsRestart] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const refresh = useCallback(async () => {
    const bridge = window.liteWork;
    if (!bridge) return;
    try {
      const i = await bridge.getLanShare();
      setInfo(i);
      if (i.enabled && i.url && i.token) {
        setQr(
          await QRCode.toDataURL(`${i.url}/#litework-token=${i.token}`, {
            width: 240,
            margin: 1,
            color: { dark: "#0d1117", light: "#ffffff" },
          })
        );
      } else {
        setQr("");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const toggle = async (enabled: boolean) => {
    const bridge = window.liteWork;
    if (!bridge || busy) return;
    setBusy(true);
    setError(null);
    try {
      const r = await bridge.setLanShare(enabled);
      if (!r.ok) {
        setError(r.error ?? "写入配置失败");
        return;
      }
      setNeedsRestart(true);
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  const restart = async () => {
    const bridge = window.liteWork;
    if (!bridge || busy) return;
    setBusy(true);
    try {
      const r = await bridge.restartCore();
      if (!r.ok) setError(r.error ?? "重启失败");
      // 成功则窗口整页刷新到新 Core，本组件随之卸载
    } finally {
      setBusy(false);
    }
  };

  const copyUrl = async () => {
    if (!info?.url) return;
    try {
      await navigator.clipboard.writeText(info.url);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* 剪贴板不可用时用户手动选中文本 */
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal phone-connect-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>📱 手机 / 局域网连接</h2>
          <button className="modal-close" onClick={onClose} title="关闭">✕</button>
        </div>
        <div className="modal-body phone-connect-body">
          {error && <div className="phone-connect-error">⚠ {error}</div>}
          {needsRestart && (
            <div className="phone-connect-restart">
              <span>已保存。重启 Core 后生效——期间维持重启前状态（刚关闭的话，重启前仍接受局域网连接）。</span>
              <button disabled={busy} onClick={() => void restart()}>
                {busy ? "重启中…" : "立即重启"}
              </button>
            </div>
          )}

          {!info ? (
            <div className="phone-connect-hint">读取配置中…</div>
          ) : !info.localCore ? (
            <div className="phone-connect-hint">
              当前窗口连接的是远程 Core（client.json 直连模式），局域网共享只适用于本机 Core。
            </div>
          ) : !info.enabled ? (
            <>
              <p className="phone-connect-desc">
                开启后，同一局域网内的手机可以直接连接这台电脑上正在运行的 Core：
                手机扫码即完成配对，无需部署服务器。
              </p>
              <ul className="phone-connect-notes">
                <li>电脑需保持开机，手机连同一 WiFi</li>
                <li>首次开启需在系统防火墙弹窗中允许（专用网络）</li>
                <li>纯 HTTP 连接：可正常对话/审批，但<b>没有推送通知</b></li>
                <li>所有请求仍需令牌鉴权；办公/公共网络不建议开启</li>
              </ul>
              <button className="phone-connect-toggle" disabled={busy} onClick={() => void toggle(true)}>
                {busy ? "保存中…" : "开启局域网共享"}
              </button>
            </>
          ) : (
            <>
              {info.ips.length === 0 ? (
                <div className="phone-connect-hint">
                  已开启，但未检测到内网 IPv4 地址——请确认电脑已连入局域网后重开本面板。
                </div>
              ) : (
                <>
                  <div className="phone-connect-qr">
                    <img src={qr} alt="手机连接二维码" />
                  </div>
                  <div className="phone-connect-url-row">
                    <code className="phone-connect-url">{info.url}</code>
                    <button onClick={() => void copyUrl()}>{copied ? "已复制" : "复制"}</button>
                  </div>
                  {info.ips.length > 1 && (
                    <div className="phone-connect-ips">
                      其它内网地址：{info.ips.slice(1).join("、")}（扫码用第一个；多网卡时手机连哪个网段用哪个）
                    </div>
                  )}
                  <p className="phone-connect-desc">
                    手机相机扫码 → Safari 打开 → 自动配对。建议随后「分享 → 添加到主屏幕」。
                  </p>
                  <button className="phone-connect-toggle off" disabled={busy} onClick={() => void toggle(false)}>
                    {busy ? "保存中…" : "关闭局域网共享"}
                  </button>
                </>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
