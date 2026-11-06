// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

// 折叠滑窗语义的纯函数测试：不依赖 DOM，直接锁住「溢出多少折多少」与
// 「绝不整段折叠」两条不变量。

import { describe, expect, it } from "vitest";
import { computeFoldFrom } from "./ChatView";

/** 造 n 轮、每轮 msgsPerTurn 条消息的 turns（模拟 RenderTurn 的 msgCount） */
function turns(n: number, msgsPerTurn = 1) {
  return Array.from({ length: n }, () => ({ msgCount: msgsPerTurn }));
}

describe("computeFoldFrom（折叠滑窗）", () => {
  it("阈值内不折叠", () => {
    expect(computeFoldFrom(turns(500), 500, 500)).toBe(0);
    expect(computeFoldFrom(turns(10, 40), 500, 500)).toBe(0);
  });

  it("超出一条就只折最早的一条（用户明确要求的语义）", () => {
    // 501 条 → 折 1；510 条 → 折 10
    expect(computeFoldFrom(turns(501), 1000, 500)).toBe(1);
    expect(computeFoldFrom(turns(510), 1000, 500)).toBe(10);
  });

  it("按轮数维度滑窗：超多少轮折多少轮", () => {
    expect(computeFoldFrom(turns(501), 500, 0)).toBe(1);
    expect(computeFoldFrom(turns(640), 500, 0)).toBe(140);
  });

  it("按消息数维度滑窗：高工具密度（少轮次多消息）也能触发", () => {
    // 20 轮 × 50 条 = 1000 条，阈值 500 → 折掉最早的 10 轮（500 条）
    expect(computeFoldFrom(turns(20, 50), 500, 500)).toBe(10);
  });

  it("两个维度取更激进者", () => {
    // 轮数：600 → 折 100；消息数：每轮 10 条 = 6000 条 → 折掉 550 轮
    expect(computeFoldFrom(turns(600, 10), 500, 500)).toBe(550);
  });

  it("绝不整段折叠：至少保留一轮（历史事故回归）", () => {
    // 1 轮 10000 条消息：只折 0（总共就一轮，折了就空白）
    expect(computeFoldFrom(turns(1, 10000), 500, 500)).toBe(0);
    // 2 轮 10000 条：最多折 1 轮，必有可见内容
    expect(computeFoldFrom(turns(2, 10000), 500, 500)).toBe(1);
    // 34 轮 × 42 条 = 1428 条（真实会话形态）：折叠后仍有保留区
    const from = computeFoldFrom(turns(34, 42), 500, 500);
    expect(from).toBeGreaterThan(0);
    expect(from).toBeLessThan(34);
  });

  it("单轮/空会话不折叠", () => {
    expect(computeFoldFrom([], 500, 500)).toBe(0);
    expect(computeFoldFrom(turns(1), 500, 500)).toBe(0);
  });

  it("阈值 ≤0 视为关闭该维度", () => {
    expect(computeFoldFrom(turns(1000), 0, 0)).toBe(0);
    expect(computeFoldFrom(turns(1000), 0, 500)).toBe(500);
  });
});
