// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// @-mention 面板行为验证：输入 @ 弹出候选、过滤、Tab/Enter 补全
import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import Composer from "./Composer";
import { api } from "../api";

// 命令/技能懒加载 mock（面板打开时请求）
vi.mock("../api", () => ({
  api: {
    commands: vi.fn().mockResolvedValue({ commands: [{ name: "compact", description: "压缩上下文", argsHint: "" }] }),
    skills: vi.fn().mockResolvedValue({ skills: [] }),
    outputs: vi.fn().mockResolvedValue({ items: [] }),
  },
}));

const baseProps = {
  running: false,
  agents: [
    { id: "build", mode: "primary", description: "开发" },
    { id: "office", mode: "primary", description: "办公" },
    { id: "qa", mode: "subagent", description: "自定义测试员" },
  ] as never,
  currentAgent: "build",
  onSelectAgent: () => {},
  onSend: () => {},
  onStop: () => {},
  llmConfig: null,
  sessionModel: null,
  onSessionModelChange: () => {},
  reasoningEffort: "",
  onReasoningEffortChange: () => {},
};

describe("Composer · @-mention 面板", () => {
  it("输入 @ 弹出候选面板（内置 spawn 角色 + 自定义 subagent）", async () => {
    const user = userEvent.setup();
    const { container } = render(<Composer {...baseProps} />);
    const textarea = container.querySelector("textarea")!;
    await user.type(textarea, "@");
    const palette = container.querySelector(".command-palette");
    expect(palette).not.toBeNull();
    // 内置角色可见
    expect(screen.getByText(/@explorer/)).toBeInTheDocument();
    // 自定义 subagent 可见
    expect(screen.getByText(/@qa/)).toBeInTheDocument();
  });

  it("输入 @ex 过滤出 explorer 候选", async () => {
    const user = userEvent.setup();
    const { container } = render(<Composer {...baseProps} />);
    const textarea = container.querySelector("textarea")!;
    await user.type(textarea, "@ex");
    expect(container.querySelector(".command-palette")).not.toBeNull();
    expect(screen.getByText(/@explorer/)).toBeInTheDocument();
    expect(screen.queryByText(/@critic/)).not.toBeInTheDocument();
  });

  it("输入 @o 过滤出 office 主 Agent 候选", async () => {
    const user = userEvent.setup();
    const { container } = render(<Composer {...baseProps} />);
    const textarea = container.querySelector("textarea")!;
    await user.type(textarea, "@o");
    expect(container.querySelector(".command-palette")).not.toBeNull();
    expect(screen.getByText(/@office/)).toBeInTheDocument();
  });

  it("候选面板列出主 Agent（标注派生执行）与 spawn 角色两类", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <Composer {...baseProps} agents={[
        { id: "build", mode: "primary", description: "开发 Agent" },
        { id: "plan", mode: "primary", description: "规划 Agent" },
      ] as never} />
    );
    const textarea = container.querySelector("textarea")!;
    await user.type(textarea, "@");
    // 主 Agent 候选（带派生标注）
    expect(screen.getByText(/@build/)).toBeInTheDocument();
    expect(screen.getAllByText(/以该 Agent 身份派生执行/).length).toBe(2);
    // 内置 spawn 角色仍在
    expect(screen.getByText(/@explorer/)).toBeInTheDocument();
    expect(container.querySelector(".command-palette")!.children.length).toBeGreaterThanOrEqual(6);
  });

  it("Tab 选中候选 → 回填 @explorer ", async () => {
    const user = userEvent.setup();
    const { container } = render(<Composer {...baseProps} />);
    const textarea = container.querySelector("textarea")! as HTMLTextAreaElement;
    await user.type(textarea, "@ex");
    fireEvent.keyDown(textarea, { key: "Tab" });
    expect(textarea.value).toBe("@explorer ");
  });

  it("中途输入 @（非首字符）不触发面板（现状行为）", async () => {
    const user = userEvent.setup();
    const { container } = render(<Composer {...baseProps} />);
    const textarea = container.querySelector("textarea")!;
    await user.type(textarea, "hello @");
    expect(container.querySelector(".command-palette")).toBeNull();
  });

  it("任务运行中输入 @ 仍弹出候选面板（追加指令走队列）", async () => {
    const user = userEvent.setup();
    const { container } = render(<Composer {...baseProps} running />);
    const textarea = container.querySelector("textarea")!;
    await user.type(textarea, "@");
    expect(container.querySelector(".command-palette")).not.toBeNull();
    expect(screen.getByText(/@explorer/)).toBeInTheDocument();
  });
});
