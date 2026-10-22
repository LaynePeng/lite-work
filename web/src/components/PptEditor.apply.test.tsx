// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 「交 AI 应用」点了没反应（用户反馈）的回归测试。
//
// 根因：PPT 编辑器是 kind==="ppt" 的页签，没有 sessionId 槽位。旧 send()
// 直接把 activeTabId 当会话归属页签 → 新建的 session 被绑到 PPT 页签上，
// 而 activeSessionId 只认 kind==="chat" → 界面上没有任何对话出现，用户
// 看到的就是「点了没反应」（其实后端任务已经跑起来了）。
//
// 锁两层：
// 1) resolveChatTab 的落点解析（纯函数）；
// 2) PptEditor 的「交 AI 应用」在无标注时给出指路提示、有标注时触发回调。

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { TabItem } from "../types";
import { resolveChatTab } from "../lib/chatTabTarget";
import PptEditor from "./PptEditor";
import { api } from "../api";

// ---------------------------------------------------------------- 落点解析

const tabs = (list: Partial<TabItem>[]): TabItem[] =>
  list.map((t, i) => ({ id: `t${i}`, kind: "chat", title: `tab${i}`, ...t } as TabItem));

describe("resolveChatTab · 对话必须落到对话页签", () => {
  it("当前就是对话页签：原地发，不新建", () => {
    const t = tabs([{ id: "a", kind: "chat", sessionId: "s1" }]);
    expect(resolveChatTab(t, "a", () => "new")).toEqual({ tabId: "a", create: null });
  });

  it("当前是 PPT 编辑器：不绑到它身上（回归点）", () => {
    const t = tabs([
      { id: "chat", kind: "chat", sessionId: "s1" },
      { id: "ppt", kind: "ppt", pptProject: "p", pptSource: "a.pptx" },
    ]);
    // 已有一个空对话页签没有 → 需要新建；但绝不能返回 "ppt"
    const r = resolveChatTab(t, "ppt", () => "made");
    expect(r.tabId).not.toBe("ppt");
    expect(r.tabId).toBe("made");
    expect(r.create).toMatchObject({ id: "made", kind: "chat", title: "新会话" });
  });

  it("存在未绑定会话的空对话页签：复用它，不再堆一个", () => {
    const t = tabs([
      { id: "draft", kind: "chat" },
      { id: "tpl", kind: "ppt-tpl", title: "从模板做 PPT" },
    ]);
    expect(resolveChatTab(t, "tpl", () => "made")).toEqual({ tabId: "draft", create: null });
  });

  it("文件页签同理", () => {
    const t = tabs([
      { id: "f", kind: "file", filePath: "a.py" },
      { id: "draft", kind: "chat" },
    ]);
    expect(resolveChatTab(t, "f", () => "made").tabId).toBe("draft");
  });
});

// ---------------------------------------------------------------- 交 AI 应用

vi.mock("../api", () => ({
  api: {
    pptAnnotations: vi.fn(),
    pptOutline: vi.fn(async () => ({ project: "p", total: 1, slides: [] })),
    pptSlide: vi.fn(async () => ({ svg: "<svg/>", slide: 1, path: "" })),
    pptThumbs: vi.fn(async () => ({ slides: [] })),
    pptAnnotate: vi.fn(async () => ({ message: "ok" })),
    pptEdit: vi.fn(),
    pptExport: vi.fn(),
  },
}));

const mocks = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

// jsdom 没有 ResizeObserver：PptEditor 的画布自适应依赖它
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal("ResizeObserver", ResizeObserverStub);

beforeEach(() => {
  vi.clearAllMocks();
  mocks.pptOutline.mockResolvedValue({ project: "p", total: 1, slides: [] });
});

afterEach(() => {
  document.body.innerHTML = "";
});

describe("PptEditor · 「交 AI 应用」不再是点了没反应", () => {
  it("有标注时把清单交给上层（上层负责切对话页签）", async () => {
    mocks.pptAnnotations.mockResolvedValue({
      count: 1,
      items: [{ slide: 1, element_id: "shape-2", note: "精简成三个要点", text: ["很长的段落"] }],
    });
    const onRequestApply = vi.fn();
    const user = userEvent.setup();
    render(<PptEditor project="p" source="a.pptx" onRequestApply={onRequestApply} />);

    await user.click(await screen.findByTitle(/把「标注」页写下的修改意图/));

    await waitFor(() => expect(onRequestApply).toHaveBeenCalledTimes(1));
    expect(onRequestApply.mock.calls[0][0]).toMatchObject({
      project: "p", source: "a.pptx",
      annotations: [{ element_id: "shape-2", note: "精简成三个要点" }],
    });
  });

  it("没有标注时按钮仍可点，并提示怎么标（不再静默）", async () => {
    mocks.pptAnnotations.mockResolvedValue({ count: 0, items: [] });
    const onRequestApply = vi.fn();
    const user = userEvent.setup();
    render(<PptEditor project="p" source="a.pptx" onRequestApply={onRequestApply} />);

    const btn = await screen.findByTitle(/把「标注」页写下的修改意图/);
    expect(btn).not.toBeDisabled();
    await user.click(btn);

    expect(await screen.findByText(/还没有标注/)).toBeInTheDocument();
    expect(onRequestApply).not.toHaveBeenCalled();
  });
});
