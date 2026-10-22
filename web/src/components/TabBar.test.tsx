// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
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

describe("TabBar · 布局（页签栏必须恒定一行高）", () => {
  /** 四种 kind 全覆盖：ppt-tpl / ppt 是这次出问题的两类。 */
  const LONG_TITLE = "重排：2026年第一季度经营复盘与全年战略规划汇报.pptx";
  const MIXED: TabItem[] = [
    ...TABS,
    { id: "tab_5", kind: "ppt", title: "季度复盘.pptx", pptProject: "p", pptSource: "季度复盘.pptx" },
    { id: "tab_6", kind: "ppt-tpl", title: LONG_TITLE },
  ];

  // 回归点：kind 曾被直接当作 class 写进 tab-item，于是 `ppt-tpl` 页签命中了
  // 模板选择器容器的全局 `.ppt-tpl { flex:1; flex-direction:column }` ——
  // 每个页签都被纵向拉伸，整条页签栏撑成两行高（实测 81px），
  // 该页签标题还被固定在 120px 而盒子被压到 21px，文字直接溢出到中间栏上。
  // jsdom 不算布局，因此这里锁住「class 命名空间」这个根因。
  it("kind 必须带命名空间前缀，不能是裸 kind class", () => {
    render(<TabBar tabs={MIXED} activeTabId="tab_1" onSelect={() => {}} onClose={() => {}} />);
    const items = document.querySelectorAll(".tab-item");
    expect(items.length).toBe(MIXED.length);
    for (const kind of ["chat", "file", "ppt", "ppt-tpl"]) {
      // 裸 class 会撞上同名的全局容器规则
      expect(document.querySelector(`.tab-item.${kind}`)).toBeNull();
    }
    expect(document.querySelector(".tab-item.tab-kind-ppt-tpl")).toBeTruthy();
    expect(document.querySelector(".tab-item.tab-kind-ppt")).toBeTruthy();
  });

  it("页签包在 .tab-list 里，动作按钮在列表之外（只有列表滚动）", () => {
    render(<TabBar tabs={MIXED} activeTabId="tab_1" onSelect={() => {}} onClose={() => {}}
      onOpenTemplate={() => {}} />);
    const list = document.querySelector(".tab-list")!;
    expect(list).toBeTruthy();
    // 页签全在滚动容器内
    expect(list.querySelectorAll(".tab-item").length).toBe(MIXED.length);
    // 动作按钮不在滚动容器内（否则页签一多就被滚走，看起来像没有这个入口）
    const actions = document.querySelector(".tab-bar-actions")!;
    expect(actions).toBeTruthy();
    expect(list.contains(actions)).toBe(false);
  });

  it("每个页签都有 title 提示（长标题被截断后仍能看全）", () => {
    render(<TabBar tabs={MIXED} activeTabId="tab_1" onSelect={() => {}} onClose={() => {}} />);
    const tabs = [...document.querySelectorAll<HTMLElement>(".tab-item")];
    expect(tabs.every((t) => (t.getAttribute("title") ?? "").length > 0)).toBe(true);
    expect(tabs.some((t) => t.getAttribute("title") === LONG_TITLE)).toBe(true);
  });
});

/** 样式契约：jsdom 不解析布局，但可以只读断言关键属性没被改回旧写法。 */
describe("TabBar · 样式契约（读 styles.css 源码文本）", () => {
  // vitest 里 import.meta.url 不是 file: 协议，用 process.cwd()（= web/）定位
  const css = readFileSync(resolve(process.cwd(), "src/styles.css"), "utf-8");
  /** 取某条规则的选择器块文本（同名的伪类规则不会被误取：只匹配 "{ sel {" ） */
  const block = (sel: string) => {
    const i = css.indexOf(`\n${sel} {`);
    expect(i, `找不到 ${sel}`).toBeGreaterThan(-1);
    return css.slice(i, css.indexOf("}", i));
  };

  it("页签栏固定一行高，滚动交给 .tab-list（页签再多也不变成两行）", () => {
    expect(block(".tab-bar")).toMatch(/height:\s*36px/);
    expect(block(".tab-bar")).toMatch(/overflow:\s*hidden/);
    expect(css).not.toMatch(/\.tab-bar \{[^}]*overflow-x:\s*auto/);
    expect(block(".tab-list")).toMatch(/overflow-x:\s*auto/);
  });

  it("页签不收缩、标题可省略号截断（否则会浮到中间栏上）", () => {
    expect(block(".tab-item")).toMatch(/flex:\s*none/);
    expect(block(".tab-title")).toMatch(/min-width:\s*0/);
    expect(block(".tab-title")).toMatch(/text-overflow:\s*ellipsis/);
    expect(block(".tab-title")).toMatch(/overflow:\s*hidden/);
  });
});

describe("TabBar · 「从模板做 PPT」常驻入口", () => {
  it("页签栏右侧常驻按钮，点击回调触发（此前只在空对话首屏，聊过就找不到）", async () => {
    const onOpenTemplate = vi.fn();
    const user = userEvent.setup();
    render(
      <TabBar tabs={TABS} activeTabId="tab_1" onSelect={() => {}} onClose={() => {}}
        sseStates={{}} onOpenTemplate={onOpenTemplate} />
    );
    // 有历史对话时按钮依然在（不是只有在空对话页才出现）
    const btn = screen.getByRole("button", { name: /从模板做 PPT/ });
    expect(btn).toBeInTheDocument();
    await user.click(btn);
    expect(onOpenTemplate).toHaveBeenCalledTimes(1);
  });

  it("未传回调时不渲染该入口（保持组件可选依赖）", () => {
    render(<TabBar tabs={TABS} activeTabId="tab_1" onSelect={() => {}} onClose={() => {}} sseStates={{}} />);
    expect(screen.queryByRole("button", { name: /从模板做 PPT/ })).not.toBeInTheDocument();
  });
});
