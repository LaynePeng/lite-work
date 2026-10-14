// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// fetch 版 SSE 客户端：分帧解析、鉴权头、错误语义（单次 onerror + 终态
// CLOSED）、close() 静默中止。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { connectEventStream } from "./sse";
import { setAuthToken } from "./connection";

function sseResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) controller.enqueue(encoder.encode(c));
      controller.close();
    },
  });
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

/** 永不结束的流：模拟长连接（close() 用例）。 */
function pendingResponse(): Response {
  const body = new ReadableStream<Uint8Array>({ start() { /* 不入队不关闭 */ } });
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

describe("connectEventStream", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    localStorage.clear();
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it("解析 data 帧：注释忽略、多行拼接、[DONE] 原样透传", async () => {
    fetchMock.mockResolvedValue(sseResponse([
      ": keepalive\n\n",
      'data: {"a": 1}\n\n',
      "data: line1\ndata: line2\n\n",
      "data: [DONE]\n\n",
    ]));
    const es = connectEventStream("/api/tasks/t1/events");
    const opened = vi.fn();
    const messages: string[] = [];
    es.onopen = opened;
    es.onmessage = (e) => messages.push(e.data);
    await vi.waitFor(() => expect(messages).toEqual(['{"a": 1}', "line1\nline2", "[DONE]"]));
    expect(opened).toHaveBeenCalledTimes(1);
  });

  it("帧跨 chunk 分片时正确缓冲拼接", async () => {
    fetchMock.mockResolvedValue(sseResponse([
      'data: {"a"',
      ': 1}\n\ndata: [DONE]\n\n',
    ]));
    const es = connectEventStream("/x");
    const messages: string[] = [];
    es.onmessage = (e) => messages.push(e.data);
    await vi.waitFor(() => expect(messages).toEqual(['{"a": 1}', "[DONE]"]));
  });

  it("携带 localStorage 令牌与 Accept 头", async () => {
    setAuthToken("secret-token");
    fetchMock.mockResolvedValue(pendingResponse());
    connectEventStream("/x");
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [url, init] = fetchMock.mock.calls[0] as [string, { headers: Record<string, string> }];
    expect(url).toBe("/x");
    expect(init.headers.Authorization).toBe("Bearer secret-token");
    expect(init.headers.Accept).toBe("text/event-stream");
  });

  it("非 2xx：onerror 一次、终态 CLOSED、不 onopen", async () => {
    fetchMock.mockResolvedValue(new Response("nope", { status: 401 }));
    const es = connectEventStream("/x");
    const onopen = vi.fn();
    const onerror = vi.fn();
    es.onopen = onopen;
    es.onerror = onerror;
    await vi.waitFor(() => expect(onerror).toHaveBeenCalledTimes(1));
    expect(onopen).not.toHaveBeenCalled();
    expect(es.readyState).toBe(2);
  });

  it("服务端未发 [DONE] 就关流：onerror 交上层探测收尾", async () => {
    fetchMock.mockResolvedValue(sseResponse(['data: {"a": 1}\n\n']));
    const es = connectEventStream("/x");
    const onerror = vi.fn();
    es.onerror = onerror;
    await vi.waitFor(() => expect(onerror).toHaveBeenCalledTimes(1));
    expect(es.readyState).toBe(2);
  });

  it("close() 主动中止：不触发 onerror", async () => {
    fetchMock.mockResolvedValue(pendingResponse());
    const es = connectEventStream("/x");
    const onerror = vi.fn();
    es.onerror = onerror;
    es.close();
    expect(es.readyState).toBe(2);
    await new Promise((r) => setTimeout(r, 30));
    expect(onerror).not.toHaveBeenCalled();
  });
});
