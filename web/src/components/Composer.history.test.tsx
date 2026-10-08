// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 输入历史翻阅：空输入按 ↑ 逐条往前翻（**可连续翻**），↓ 往回、到底清空。
// 回归：↑ 的守卫曾写成 `!text`——翻出第一条后输入框已非空，再按 ↑ 被挡，
// 表现为「只能往前翻一条」，与注释声明的语义（可翻阅）不符。
import { render, fireEvent } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Composer from "./Composer";
import { api } from "../api";

vi.mock("../api", () => ({
  api: { commands: vi.fn(), skills: vi.fn(), outputs: vi.fn() },
}));

const baseProps = {
  running: false,
  agents: [
    { id: "build", mode: "primary", description: "开发" },
    { id: "office", mode: "primary", description: "办公" },
  ] as never,
  currentAgent: "office",
  onSelectAgent: () => {},
  onSend: () => {},
  onStop: () => {},
  llmConfig: null,
  sessionModel: null,
  onSessionModelChange: () => {},
  reasoningEffort: "",
  onReasoningEffortChange: () => {},
};

// 历史数组按「最新在前」存储（pushHistory 用 unshift）
const HISTORY = ["最新指令", "中间指令", "最早指令"];

function setup(): HTMLTextAreaElement {
  const { container } = render(<Composer {...baseProps} />);
  const el = container.querySelector("textarea") as HTMLTextAreaElement;
  el.focus();
  return el;
}

const up = (el: HTMLElement) => fireEvent.keyDown(el, { key: "ArrowUp" });
const down = (el: HTMLElement) => fireEvent.keyDown(el, { key: "ArrowDown" });

describe("Composer · 输入历史翻阅", () => {
  beforeEach(() => {
    localStorage.clear();
    // setup.ts 的 vi.restoreAllMocks() 会清掉 mock 实现 → 逐用例重设
    (api.commands as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ commands: [] });
    (api.skills as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ skills: [] });
    (api.outputs as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ groups: [], total: 0 });
    localStorage.setItem("litework.inputHistory", JSON.stringify(HISTORY));
  });

  it("连续按 ↑ 可逐条往前翻，↓ 往回并在到底时清空", () => {
    const el = setup();
    expect(el.value).toBe("");

    up(el);
    expect(el.value).toBe("最新指令");
    up(el); // ← 回归点：修复前会卡死在「最新指令」
    expect(el.value).toBe("中间指令");
    up(el);
    expect(el.value).toBe("最早指令");
    up(el); // 已到最早 → 保持不变
    expect(el.value).toBe("最早指令");

    down(el);
    expect(el.value).toBe("中间指令");
    down(el);
    expect(el.value).toBe("最新指令");
    down(el); // 翻过头 → 清空并退出翻阅态
    expect(el.value).toBe("");
  });

  it("手动编辑退出翻阅态后，↑ 不再劫持已有文本", () => {
    const el = setup();
    up(el);
    expect(el.value).toBe("最新指令");

    fireEvent.change(el, { target: { value: "手写内容" } }); // 触发 onChange → 退出翻阅态
    up(el);
    expect(el.value).toBe("手写内容");
  });

  it("无历史时 ↑ 不吞按键、不改输入", () => {
    localStorage.setItem("litework.inputHistory", JSON.stringify([]));
    const el = setup();
    up(el);
    expect(el.value).toBe("");
  });
});
