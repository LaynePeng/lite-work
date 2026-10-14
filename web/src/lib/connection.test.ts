// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 扫码配对：#litework-token=xxx → localStorage，地址栏剥除。

import { afterEach, describe, expect, it } from "vitest";
import { authToken, consumeTokenFromUrl } from "./connection";

describe("consumeTokenFromUrl", () => {
  afterEach(() => {
    window.localStorage.clear();
    window.history.replaceState(null, "", "/");
  });

  it("从配对链接取令牌并从地址栏剥除（不进历史）", () => {
    window.history.replaceState(null, "", "/#litework-token=abc123_XY-9");
    consumeTokenFromUrl();
    expect(authToken()).toBe("abc123_XY-9");
    expect(window.location.hash).toBe("");
  });

  it("无配对 hash 时不动作、不覆盖已存令牌", () => {
    window.localStorage.setItem("litework.auth.token", "keep-me");
    window.history.replaceState(null, "", "/");
    consumeTokenFromUrl();
    expect(authToken()).toBe("keep-me");
  });

  it("hash 里是其它内容时不误吞", () => {
    window.history.replaceState(null, "", "/#section-1");
    consumeTokenFromUrl();
    expect(authToken()).toBe("");
    expect(window.location.hash).toBe("#section-1");
  });
});
