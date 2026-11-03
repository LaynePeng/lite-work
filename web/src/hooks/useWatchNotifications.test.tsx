// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 盯梢通知订阅：历史回放只补列表、不弹提示（回归「盯梢关了还提醒」）。

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useWatchNotifications, type WatchNotification } from "./useWatchNotifications";

interface FakeStream {
  path: string;
  onopen: (() => void) | null;
  onmessage: ((e: { data: string }) => void) | null;
  onerror: (() => void) | null;
  close: () => void;
}

const streams: FakeStream[] = [];

vi.mock("../lib/connection", () => ({
  apiUrl: (p: string) => p,
  authHeaders: () => ({}),
}));

vi.mock("../lib/sse", () => ({
  connectEventStream: (path: string) => {
    const stream: FakeStream = {
      path,
      onopen: null,
      onmessage: null,
      onerror: null,
      close: () => {},
    };
    streams.push(stream);
    return stream;
  },
}));

const note = (id: number, over: Partial<WatchNotification> = {}): WatchNotification => ({
  id,
  at: 1700000000 + id,
  schedule_id: "sch_1",
  name: "盯部署",
  status: "changed",
  detail: `事件${id}`,
  ...over,
});

afterEach(() => {
  streams.length = 0;
  vi.unstubAllGlobals();
});

describe("useWatchNotifications", () => {
  it("历史只进面板，不弹提示；连上后新到的才进 liveItems", async () => {
    const history = [note(1), note(2)];
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ notifications: history }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useWatchNotifications());

    await waitFor(() => expect(streams).toHaveLength(1));
    // 不阻塞在历史请求上：SSE 立即连，历史并行补列表
    expect(streams[0].path).toBe("/api/watch/events");
    await waitFor(() => expect(result.current.items.map((n) => n.id)).toEqual([2, 1]));
    expect(result.current.liveItems).toEqual([]);

    // 服务端回放（replay 标记）仍然只补列表
    act(() => {
      streams[0].onmessage?.({
        data: JSON.stringify({ ...note(3), replay: true }),
      });
    });
    await waitFor(() => expect(result.current.items.map((n) => n.id)).toEqual([3, 2, 1]));
    expect(result.current.liveItems).toEqual([]);

    // 真正的新通知才提醒
    act(() => {
      streams[0].onmessage?.({ data: JSON.stringify(note(4)) });
    });
    await waitFor(() => expect(result.current.liveItems.map((n) => n.id)).toEqual([4]));
    expect(result.current.items.map((n) => n.id)).toEqual([4, 3, 2, 1]);
  });

  it("历史拉取失败也照常连 SSE（回放帧不会误弹）", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));

    const { result } = renderHook(() => useWatchNotifications());
    await waitFor(() => expect(streams).toHaveLength(1));
    expect(streams[0].path).toBe("/api/watch/events");

    act(() => {
      streams[0].onmessage?.({ data: JSON.stringify({ ...note(9), replay: true }) });
    });
    await waitFor(() => expect(result.current.items.map((n) => n.id)).toEqual([9]));
    expect(result.current.liveItems).toEqual([]);
  });
});
