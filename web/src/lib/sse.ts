// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// fetch 版 SSE 客户端。
//
// 为什么不用 EventSource：它不能携带 Authorization 头。桌面本地模式有
// Electron 的 session 级注入（token-injector.js）不成问题；浏览器/PWA
// 远程模式没有这层，必须由渲染进程自己带令牌，只能换成 fetch 流式读。
//
// 只实现本项目两处 SSE（chat.py / schedules.py）用到的子集：纯 data: 帧、
// 空行分帧；不支持命名 event 与 last-event-id（回放游标走 query after=）。
// 重连交给调用方：出错只回调一次 onerror 并置 CLOSED，上层自行探测存活、
// 退避或放弃（App.tsx 的任务流与 useWatchNotifications 的盯梢流均如此）。

import { apiUrl, authHeaders, notifyUnauthorized } from "./connection";

/** 与 EventSource 的 readyState 取值对齐（调用方只用到 CLOSED 判断）。 */
export const SSE_CLOSED = 2;

export interface EventStream {
  readyState: number;
  onopen: (() => void) | null;
  onmessage: ((e: { data: string }) => void) | null;
  onerror: (() => void) | null;
  close(): void;
}

export function connectEventStream(path: string): EventStream {
  const ctl = new AbortController();
  const stream: EventStream = {
    readyState: 0,
    onopen: null,
    onmessage: null,
    onerror: null,
    close() {
      stream.readyState = SSE_CLOSED;
      ctl.abort();
    },
  };
  void (async () => {
    try {
      const res = await fetch(apiUrl(path), {
        headers: { Accept: "text/event-stream", ...authHeaders() },
        signal: ctl.signal,
      });
      if (!res.ok || !res.body) {
        // 服务端拒绝（401/404/5xx）或无流可读：等价 EventSource 的「已放弃」
        if (res.status === 401) notifyUnauthorized();
        stream.readyState = SSE_CLOSED;
        stream.onerror?.();
        return;
      }
      stream.readyState = 1;
      stream.onopen?.();
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let sep = buf.indexOf("\n\n");
        while (sep >= 0) {
          dispatchFrame(buf.slice(0, sep), stream);
          buf = buf.slice(sep + 2);
          sep = buf.indexOf("\n\n");
        }
      }
      // 流被服务端关闭但没发 [DONE]：视作出错，交上层探测收尾
      if (stream.readyState !== SSE_CLOSED) {
        stream.readyState = SSE_CLOSED;
        stream.onerror?.();
      }
    } catch {
      // close() 主动中断也会走到这里；已 CLOSED 的不再回调
      if (stream.readyState !== SSE_CLOSED) {
        stream.readyState = SSE_CLOSED;
        stream.onerror?.();
      }
    }
  })();
  return stream;
}

/** 解析一帧：data: 行按 SSE 规范拼接，其余行（注释/字段）忽略。 */
function dispatchFrame(frame: string, stream: EventStream): void {
  const data = frame
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).replace(/^ /, ""))
    .join("\n");
  if (data) stream.onmessage?.({ data });
}
