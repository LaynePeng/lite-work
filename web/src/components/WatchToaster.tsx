// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 盯梢通知提示条：右下角浮层。
//
// 与普通 toast 的差别：盯梢通知**可能一次来好几条**（多条计划同时到点），
// 所以做成可堆叠列表而不是单条替换；同时给「打开会话」入口——用户看到
// 「检测到变化」后最想做的是跳到那条计划的会话里看结果。

import { useEffect, useRef } from "react";

import {
  notificationLabel,
  notificationTone,
  showSystemNotification,
  wantsSystemNotification,
  type WatchNotification,
} from "../hooks/useWatchNotifications";

interface Props {
  items: WatchNotification[];
  onDismiss: (id: number) => void;
  onOpenSession?: (sessionId: string) => void;
  /** schedule_id → session_id（用于「查看」跳转；缺省则不显示按钮） */
  sessionOf?: (scheduleId: string) => string | undefined;
  /** 钉住不自动收起的 id（从盯梢页签「重新弹出」的条目：用户要看，别 6 秒就没了） */
  pinned?: number[];
}

/** 自动收起时长（ms）：错误留久一点，让用户看清。 */
const TTL_OK = 6000;
const TTL_ERR = 12000;

export function WatchToaster({ items, onDismiss, onOpenSession, sessionOf, pinned }: Props) {
  const timers = useRef(new Map<number, number>());
  const sysShown = useRef(new Set<number>());

  // 自动收起：每个 id 只挂一个定时器；卸载/手动关闭时清理
  useEffect(() => {
    const pinnedIds = new Set(pinned ?? []);
    for (const n of items) {
      if (pinnedIds.has(n.id)) {
        // 钉住的项：清掉可能已挂的定时器，不再自动收起
        const t = timers.current.get(n.id);
        if (t !== undefined) {
          window.clearTimeout(t);
          timers.current.delete(n.id);
        }
        continue;
      }
      if (timers.current.has(n.id)) continue;
      const ttl = notificationTone(n.status) === "bad" ? TTL_ERR : TTL_OK;
      const t = window.setTimeout(() => {
        timers.current.delete(n.id);
        onDismiss(n.id);
      }, ttl);
      timers.current.set(n.id, t);
    }
    return () => {
      // 注意：清理只针对已消失的项，不能整体清空（否则每次 items 变化
      // 都会把仍在展示的项的定时器重置，导致永不自动收起）
      for (const [id, t] of timers.current) {
        if (!items.some((n) => n.id === id)) {
          window.clearTimeout(t);
          timers.current.delete(id);
        }
      }
    };
  }, [items, onDismiss, pinned]);

  // 系统通知：每条只弹一次
  useEffect(() => {
    for (const n of items) {
      if (sysShown.current.has(n.id)) continue;
      sysShown.current.add(n.id);
      if (wantsSystemNotification(n)) showSystemNotification(n);
    }
  }, [items]);

  if (!items.length) return null;

  return (
    <div className="watch-toaster" role="status" aria-live="polite">
      {items.map((n) => {
        const tone = notificationTone(n.status);
        const sessionId = sessionOf?.(n.schedule_id);
        return (
          <div key={n.id} className={`watch-toast ${tone}`}>
            <div className="watch-toast-head">
              <span className="watch-toast-badge">{notificationLabel(n.status)}</span>
              <span className="watch-toast-name" title={n.name}>{n.name}</span>
              <button
                className="watch-toast-close"
                title="关闭"
                aria-label="关闭通知"
                onClick={() => onDismiss(n.id)}
              >
                ✕
              </button>
            </div>
            {n.detail && <div className="watch-toast-detail">{n.detail}</div>}
            {sessionId && onOpenSession && (
              <button
                className="watch-toast-open"
                onClick={() => {
                  onOpenSession(sessionId);
                  onDismiss(n.id);
                }}
              >
                打开会话 →
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}
