// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 「手机 / 局域网连接」面板（仅桌面 Electron 渲染）：
// 开关局域网共享 → 展示内网地址 + 配对二维码。手机相机扫码打开
// http://<内网IP>:<端口>/#litework-token=xxx，前端 consumeTokenFromUrl
// 自动配对，无需手抄令牌。

import { useCallback, useEffect, useMemo, useState } from "react";
import QRCode from "qrcode";
import type { LanProbeResult, LanShareInfo } from "../types";

/** 防火墙提示按平台分支：Windows 的「专用网络」在 macOS 上根本不存在，
 *  照抄会让 Mac 用户找不到地方（macOS 侧只要在弹窗点「允许」即可）。
 *  刻意不在模块加载时求值——平台来自注入的 bridge，须随渲染实时读取。 */
function firewallHint(): string {
  return window.liteWork?.platform === "darwin"
    ? "如弹出「是否允许接受传入网络连接」请点允许；被拒可在「系统设置 → 网络 → 防火墙 → 选项」中改"
    : "首次开启需在系统防火墙弹窗中允许（专用网络）";
}

export default function PhoneConnectModal({ onClose }: { onClose: () => void }) {
  const bridge = window.liteWork;
  const [info, setInfo] = useState<LanShareInfo | null>(null);
  const [qr, setQr] = useState("");
  const [busy, setBusy] = useState(false);
  // 开关已保存但 Core 未重启（绑定参数在启动时生效）
  const [needsRestart, setNeedsRestart] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  // 多网卡多网段时由用户点选；缺省第一个（主进程已把 iPhone 热点排在首位）
  const [selectedIp, setSelectedIp] = useState("");
  // 连通性探测结果（对当前选中网段）；null=未测过
  const [probe, setProbe] = useState<LanProbeResult | null>(null);
  const [probing, setProbing] = useState(false);

  const refresh = useCallback(async () => {
    const bridge = window.liteWork;
    if (!bridge) return;
    try {
      const i = await bridge.getLanShare();
      setInfo(i);
      const list = i.ips?.length ? i.ips : (i.addresses ?? []).map((a) => a.ip);
      setSelectedIp((prev) => (list.includes(prev) ? prev : list[0] ?? ""));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // 换网段即作废上一次探测结论（否则「热点是通的」会挂在已切走的网段上）
  useEffect(() => {
    setProbe(null);
  }, [selectedIp]);

  /** 展示与编码进二维码的地址：以主进程下发的 url 为底，切网段时换 hostname。 */
  const displayUrl = useMemo(() => {
    if (!info?.url) return "";
    if (!selectedIp) return info.url;
    try {
      const u = new URL(info.url);
      u.hostname = selectedIp;
      return u.href.replace(/\/$/, "");
    } catch {
      return info.url;
    }
  }, [info, selectedIp]);

  /** 网段明细（主进程给的 addresses；旧版主进程只回 ips 时退化为纯 IP）。 */
  const addressRows = useMemo(() => {
    if (info?.addresses?.length) return info.addresses;
    const seen = new Set<string>();
    return (info?.ips ?? [])
      .filter((ip) => !seen.has(ip) && seen.add(ip))
      .map((ip) => ({ ip, iface: "", kind: "lan" as const, label: "网络" }));
  }, [info]);

  // 配置开关与"这个进程是不是真按共享参数起来的"不一致 → 必须提示重启。
  // 只信 info.applied 才能挡住最坑人的失败模式：开关已存但没重启，Core 仍绑
  // 127.0.0.1，面板照旧渲染二维码，而那个码根本不可能通（用户只会看到手机报错）。
  const restartPending = !!info?.enabled && !info?.applied;
  const restartPendingOff = !!info?.applied && info?.enabled === false;

  // 二维码跟随选中网段重生成；卸载/网段切换后不落地旧结果
  useEffect(() => {
    const token = info?.token;
    if (!displayUrl || !token) {
      setQr("");
      return;
    }
    let alive = true;
    void QRCode.toDataURL(`${displayUrl}/#litework-token=${token}`, {
      width: 240,
      margin: 1,
      color: { dark: "#0d1117", light: "#ffffff" },
    })
      .then((data) => {
        if (alive) setQr(data);
      })
      .catch(() => {
        if (alive) setQr("");
      });
    return () => {
      alive = false;
    };
  }, [displayUrl, info]);

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

  /** 探测当前选中网段：主进程从该地址请求 Core 的 /api/health。
   *  「地址够不着」与「地址选错」在浏览器里长得一模一样（都是通用报错），
   *  这一步把结论直接说出来。 */
  const runProbe = async () => {
    const bridge = window.liteWork;
    if (!bridge?.probeLanShare || !selectedIp || probing) return;
    setProbing(true);
    try {
      setProbe(await bridge.probeLanShare(selectedIp));
    } catch (e) {
      setProbe({ ok: false, error: e instanceof Error ? e.message : String(e) });
    } finally {
      setProbing(false);
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
    if (!displayUrl) return;
    try {
      await navigator.clipboard.writeText(displayUrl);
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
          {(needsRestart || restartPending) && (
            <div className="phone-connect-restart">
              <span>
                {restartPending
                  ? "已保存。重启 Core 后才能接受局域网连接。"
                  : restartPendingOff
                    ? "已关闭，但重启前这个 Core 仍接受局域网连接（端口还没收回）。"
                    : "已保存。重启 Core 后生效——期间维持重启前状态（刚关闭的话，重启前仍接受局域网连接）。"}
              </span>
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
                <li>电脑需保持开机，手机连同一 WiFi（或连本机开的 iPhone 热点）</li>
                <li>{firewallHint()}</li>
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
              ) : restartPending ? (
                // 未生效时**不渲染二维码**：那个码注定连不通，展示出来只会误导
                <>
                  <div className="phone-connect-hint">
                    下面这些地址现在就 <b>连不通</b>——本 Core 还没按共享参数重启
                    （仍是仅本机可访问）。点上方「立即重启」后再扫码。
                  </div>
                  <button className="phone-connect-toggle off" disabled={busy} onClick={() => void toggle(false)}>
                    {busy ? "保存中…" : "取消开启"}
                  </button>
                </>
              ) : (
                <>
                  <div className="phone-connect-qr">
                    {qr ? (
                      <img src={qr} alt="手机连接二维码" />
                    ) : (
                      <div className="phone-connect-qr-loading">二维码生成中…</div>
                    )}
                  </div>
                  <div className="phone-connect-url-row">
                    <code className="phone-connect-url">{displayUrl}</code>
                    <button onClick={() => void copyUrl()}>{copied ? "已复制" : "复制"}</button>
                  </div>
                  {info.ips.length > 1 && (
                    <div className="phone-connect-ips">
                      <div className="phone-connect-ip-chips">
                        {addressRows.map((a) => (
                          <button
                            key={a.ip}
                            className={`ip-chip ${a.ip === selectedIp ? "active" : ""}`}
                            onClick={() => setSelectedIp(a.ip)}
                            title={a.iface ? `网卡 ${a.iface}` : undefined}
                          >
                            {a.ip}
                            <span className={`ip-chip-kind kind-${a.kind}`}>{a.label}</span>
                          </button>
                        ))}
                      </div>
                      <div>手机连哪个网络就选哪个网段，二维码会跟着切换</div>
                    </div>
                  )}
                  {bridge?.probeLanShare && (
                    <div className="phone-connect-probe">
                      <button disabled={probing || !selectedIp} onClick={() => void runProbe()}>
                        {probing ? "检测中…" : "检测连接"}
                      </button>
                      {probe && (
                        <span className={`phone-connect-probe-result ${probe.ok ? "ok" : "bad"}`}>
                          {probe.ok
                            ? `✓ ${selectedIp} 可达${probe.version ? `（Core ${probe.version}）` : ""}`
                            : `✗ ${probe.error ?? "不可达"}`}
                        </span>
                      )}
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
