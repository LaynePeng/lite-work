// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import TabBar from "./TabBar";
import type { TabItem } from "../types";

const TABS: TabItem[] = [
  { id: "tab_1", kind: "chat", sessionId: "s1", title: "会话一" },
  { id: "tab_2", kind: "chat", sessionId: "s2", title: "会话二" },
  { id: "tab_3", kind: "chat", title: "新会话（无 session）" },
  { id: "tab_4", kind: "file", filePath: "a.py", title: "a.py" },
];

describe("TabBar · SSE 连接状态点（会话 tab 标题右侧，无文字）", () => {
  it("运行中的会话 tab 显示对应颜色的点", () => {
    render(
      <TabBar tabs={TABS} activeTabId="tab_1" onSelect={() => {}} onClose={() => {}}
        sseStates={{ s1: "connected", s2: "reconnecting" }} />
    );
    const dots = document.querySelectorAll(".tab-sse-dot");
    expect(dots.length).toBe(2);
    expect(dots[0].className).toContain("sse-connected");
    expect(dots[1].className).toContain("sse-reconnecting");
    // 无文字（指示器纯色点）
    expect(screen.queryByText(/已连接|重连中/)).not.toBeInTheDocument();
  });

  it("未运行 / 无 session 的 tab 不显示点", () => {
    render(
      <TabBar tabs={TABS} activeTabId="tab_1" onSelect={() => {}} onClose={() => {}}
        sseStates={{}} />
    );
    expect(document.querySelectorAll(".tab-sse-dot").length).toBe(0);
  });

  it("idle 状态不显示（等价未运行）", () => {
    render(
      <TabBar tabs={TABS} activeTabId="tab_1" onSelect={() => {}} onClose={() => {}}
        sseStates={{ s1: "idle" }} />
    );
    expect(document.querySelectorAll(".tab-sse-dot").length).toBe(0);
  });

  it("失联显示红点（lost）", () => {
    render(
      <TabBar tabs={TABS} activeTabId="tab_1" onSelect={() => {}} onClose={() => {}}
        sseStates={{ s1: "lost" }} />
    );
    expect(document.querySelector(".tab-sse-dot.sse-lost")).toBeInTheDocument();
  });
});
