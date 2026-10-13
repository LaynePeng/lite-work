// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 盯梢通知订阅：应用级 SSE（与任务无关）。
//
// 为什么不复用任务 SSE（/api/tasks/{id}/events）：盯梢通知由调度器在自己
// 的时刻产生——纯定时、指纹变化、探针失败——此时可能**根本没有任务在跑**，
// 没有 task_id 可订阅。
//
// 断线重连：EventSource 原生重连会带**原始 URL**（不会带上我们后加的 query），
// 因此这里在重连时自行重建连接并带上 `after=<已收到的最大 id>`，让服务端回放
// 缺口。配合按 id 去重，做到「不丢也不重」。

import { useCallback, useEffect, useRef, useState } from "react";

/** 一条盯梢通知（服务端 WatchNotificationBus 的推送体）。 */
export interface WatchNotification {
  /** 单调递增 id（回放游标与去重都用它） */
  id: number;
  at: number;
  schedule_id: string;
  name: string;
  /** ok / changed / unchanged / error / skipped */
  status: string;
  detail: string;
  task_id?: string | null;
  /** 计划的 notify 配置，如 ["in_app", "system"] */
  notify?: string[];
}

export interface WatchNotificationsApi {
  /** 最近通知（新→旧） */
  items: WatchNotification[];
  /** SSE 是否已连上 */
  connected: boolean;
  /** 清空本地与远端历史 */
  clear: () => void;
}

/** status → 展示用的语气（决定颜色/图标）。 */
export function notificationTone(status: string): "good" | "warn" | "bad" | "muted" {
  switch (status) {
    case "changed":
      return "good";
    case "ok":
      return "good";
    case "unchanged":
      return "muted";
    case "skipped":
      return "warn";
    case "error":
      return "bad";
    default:
      return "muted";
  }
}

const STATUS_LABEL: Record<string, string> = {
  changed: "检测到变化",
  ok: "已触发",
  unchanged: "无变化",
  skipped: "已跳过",
  error: "出错",
};

export function notificationLabel(status: string): string {
  return STATUS_LABEL[status] ?? status;
}

/** 是否应该弹系统通知：计划声明了 system，且状态值得打扰用户。 */
export function wantsSystemNotification(n: WatchNotification): boolean {
  if (!n.notify?.includes("system")) return false;
  // 「无变化」不该打扰用户（那正是盯梢的意义）
  return n.status !== "unchanged";
}

/** 保留最近多少条（面板展示用；服务端另有历史） */
const KEEP = 50;

export function useWatchNotifications(pollHistory = true): WatchNotificationsApi {
  const [items, setItems] = useState<WatchNotification[]>([]);
  const [connected, setConnected] = useState(false);
  const seenRef = useRef<Set<number>>(new Set());
  const lastIdRef = useRef<number>(0);
  const esRef = useRef<EventSource | null>(null);
  const timerRef = useRef<number | null>(null);
  const closedRef = useRef(false);

  const accept = useCallback((n: WatchNotification) => {
    if (!n || typeof n.id !== "number") return;
    if (seenRef.current.has(n.id)) return;   // 去重：回放与实时可能重叠
    seenRef.current.add(n.id);
    if (n.id > lastIdRef.current) lastIdRef.current = n.id;
    setItems((prev) => [n, ...prev].slice(0, KEEP));
  }, []);

  const clear = useCallback(() => {
    seenRef.current = new Set();
    lastIdRef.current = 0;
    setItems([]);
    void fetch("/api/watch/notifications/clear", { method: "POST" }).catch(() => {
      /* 清远端失败不影响本地已清空 */
    });
  }, []);

  const connect = useCallback(() => {
    if (closedRef.current) return;
    esRef.current?.close();
    const cursor = lastIdRef.current;
    const url = cursor > 0 ? `/api/watch/events?after=${cursor}` : "/api/watch/events";
    const es = new EventSource(url);
    esRef.current = es;
    es.onopen = () => setConnected(true);
    es.onmessage = (e) => {
      try {
        accept(JSON.parse(e.data) as WatchNotification);
      } catch {
        /* 忽略坏帧 */
      }
    };
    es.onerror = () => {
      setConnected(false);
      es.close();
      if (closedRef.current) return;
      // 自行重连并带上游标，让服务端回放断线窗口内的通知
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
      timerRef.current = window.setTimeout(() => connect(), 3000);
    };
  }, [accept]);

  useEffect(() => {
    closedRef.current = false;
    connect();
    // 首次拉一次历史：覆盖「页面在盯梢触发之前就已打开」的场景
    if (pollHistory) {
      void fetch("/api/watch/notifications?limit=50")
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => {
          const list: WatchNotification[] = d?.notifications ?? [];
          // 历史是旧→新，倒序喂给 accept（它按 id 去重，顺序不影响最终展示）
          for (const n of [...list].reverse()) accept(n);
        })
        .catch(() => {
          /* 历史拉取失败不影响实时订阅 */
        });
    }
    return () => {
      closedRef.current = true;
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
      esRef.current?.close();
      esRef.current = null;
    };
  }, [connect, accept, pollHistory]);

  return { items, connected, clear };
}

/** 弹系统通知（桌面端；浏览器需用户已授权）。失败静默。 */
export function showSystemNotification(n: WatchNotification): void {
  try {
    if (typeof Notification === "undefined") return;
    if (Notification.permission === "granted") {
      // eslint-disable-next-line no-new
      new Notification(`盯梢：${n.name}`, { body: n.detail || notificationLabel(n.status) });
    } else if (Notification.permission !== "denied") {
      void Notification.requestPermission().then((p) => {
        if (p === "granted") {
          // eslint-disable-next-line no-new
          new Notification(`盯梢：${n.name}`, { body: n.detail || notificationLabel(n.status) });
        }
      });
    }
  } catch {
    /* 系统通知不可用时忽略（应用内提示仍可见） */
  }
}
