// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import { describe, expect, it } from "vitest";
import { formatMsgTime, formatMsgTimeFull } from "./msgTime";

/** 构造本地时区的日期（避免 CI 时区差异导致用例飘） */
const at = (y: number, mo: number, d: number, h: number, mi: number, s: number) =>
  new Date(y, mo - 1, d, h, mi, s).getTime();

describe("formatMsgTime", () => {
  const now = new Date(2026, 9, 25, 14, 32, 19); // 2026-10-25 14:32:19 本地

  it("今天只显示时分秒（到秒，补零）", () => {
    expect(formatMsgTime(at(2026, 10, 25, 9, 5, 3), now)).toBe("09:05:03");
    expect(formatMsgTime(at(2026, 10, 25, 0, 0, 0), now)).toBe("00:00:00");
    expect(formatMsgTime(at(2026, 10, 25, 23, 59, 59), now)).toBe("23:59:59");
  });

  it("跨天补上月-日", () => {
    expect(formatMsgTime(at(2026, 10, 24, 23, 58, 41), now)).toBe("10-24 23:58:41");
    expect(formatMsgTime(at(2025, 1, 2, 3, 4, 5), now)).toBe("01-02 03:04:05");
  });

  it("缺失/非法 ts → 空串（调用方据此不渲染）", () => {
    expect(formatMsgTime(undefined, now)).toBe("");
    expect(formatMsgTime(null, now)).toBe("");
    expect(formatMsgTime(0, now)).toBe("");
    expect(formatMsgTime(-5, now)).toBe("");
    expect(formatMsgTime(Number.NaN, now)).toBe("");
  });

  it("文案长度恒定（今日 8 字符 / 跨天 14 字符）", () => {
    expect(formatMsgTime(at(2026, 10, 25, 1, 2, 3), now)).toHaveLength(8);
    expect(formatMsgTime(at(2026, 10, 1, 1, 2, 3), now)).toHaveLength(14);
  });
});

describe("formatMsgTimeFull", () => {
  it("完整年月日时分秒（悬停 tooltip）", () => {
    expect(formatMsgTimeFull(at(2026, 10, 24, 23, 58, 41))).toBe("2026-10-24 23:58:41");
  });

  it("非法值 → 空串", () => {
    expect(formatMsgTimeFull(undefined)).toBe("");
    expect(formatMsgTimeFull(0)).toBe("");
  });
});
