// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 上传请求头回归：FormData 的 Content-Type（含 multipart boundary）必须由浏览器
// 生成。曾在 req() 里无条件写死 application/json，把 boundary 覆盖掉，后端
// /api/upload 解析不出 file 字段，附件上传全部 400。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";

function okJson(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("api.uploadFile", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn(async () => okJson({ path: "素材/数据.csv", name: "数据.csv", size: 3 }));
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("不给 FormData 请求写死 Content-Type（否则丢 boundary）", async () => {
    const file = new File(["a,b\n"], "数据.csv", { type: "text/csv" });
    await api.uploadFile(file);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    const headers = init.headers as Record<string, string>;
    expect(headers["Content-Type"]).toBeUndefined();
    expect(init.body).toBeInstanceOf(FormData);
    // 鉴权头仍需保留（多端鉴权依赖它）
    expect(headers["X-Client-Id"]).toBeDefined();
  });

  it("普通 JSON 请求仍然带 application/json", async () => {
    fetchMock.mockResolvedValueOnce(okJson({ ok: true }));
    await api.status();

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  });
});
