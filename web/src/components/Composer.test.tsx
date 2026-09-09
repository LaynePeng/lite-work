// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import Composer from "./Composer";
import type { CollabMode } from "../types";

const baseProps = {
  running: false,
  agents: [{ id: "build", mode: "primary", description: "开发" } as never],
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

const MODES: CollabMode[] = [
  { name: "default", display_name: "默认（自动路由）", description: "内置路由", source: "builtin", version: "", icon_url: null },
  { name: "review", display_name: "审查门", description: "审查策略", source: "builtin", version: "", icon_url: null },
  { name: "meeting", display_name: "会议（群聊共识）", description: "轮流发言收敛共识", source: "builtin", version: "1.0.0", icon_url: null },
  { name: "orchestrate", display_name: "编排-工人（并行派发）", description: "并行派发子任务", source: "builtin", version: "1.0.0", icon_url: null },
  { name: "debate", display_name: "辩论（对抗收敛）", description: "多轮对抗审查", source: "plugin", version: "1.1.0", icon_url: null },
];

describe("Composer · SSE 连接状态指示器（P1-5）", () => {
  it("非运行态不渲染指示器", () => {
    render(<Composer {...baseProps} running={false} sseState="connected" />);
    expect(screen.queryByText("已连接")).not.toBeInTheDocument();
  });

  it.each([
    ["connected", "已连接"],
    ["reconnecting", "重连中…"],
    ["lost", "连接丢失（任务仍在后端运行）"],
  ] as const)("运行中显示状态文案（%s → %s）", (state, label) => {
    const { unmount } = render(<Composer {...baseProps} running sseState={state} />);
    expect(screen.getByText(label)).toBeInTheDocument();
    const dot = document.querySelector(".sse-dot");
    expect(dot?.className).toContain(`sse-${state}`);
    unmount();
  });

  it("运行中显示绿点（connected）", () => {
    render(<Composer {...baseProps} running sseState="connected" />);
    expect(document.querySelector(".sse-dot.sse-connected")).toBeInTheDocument();
  });
});

describe("Composer · 协作模式选择器（方案 B 全插件化，会话级生效）", () => {
  it("协作模式在选择器中分组展示（内置 + 社区覆盖版，含说明）", async () => {
    const user = userEvent.setup();
    render(
      <Composer
        {...baseProps}
        collabModes={MODES}
        sessionCollabMode={null}
        onSessionCollabMode={() => {}}
      />
    );
    await user.click(screen.getByTitle(/多 Agent 协作模式/));
    // 分组标题 + 模式条目（内置与本地覆盖版并列；描述进 title 属性）
    expect(screen.getByText("协作模式（本会话生效）")).toBeInTheDocument();
    expect(screen.getByText("会议（群聊共识）")).toBeInTheDocument();
    expect(screen.getByText("辩论（对抗收敛）")).toBeInTheDocument();
    // 方案 B：不再有「技能触发」一次性分组
    expect(screen.queryByText(/技能触发/)).not.toBeInTheDocument();
    // 本地覆盖版带「社区版」标记
    expect(screen.getByText("社区版")).toBeInTheDocument();
  });

  it("选中模式 → 会话级回调触发（写入会话 metadata，全任务生效）", async () => {
    const onSessionCollabMode = vi.fn();
    const user = userEvent.setup();
    render(
      <Composer
        {...baseProps}
        collabModes={MODES}
        sessionCollabMode={null}
        onSessionCollabMode={onSessionCollabMode}
      />
    );
    await user.click(screen.getByTitle(/多 Agent 协作模式/));
    await user.click(screen.getByText("会议（群聊共识）"));
    expect(onSessionCollabMode).toHaveBeenCalledWith("meeting");
  });

  it("会话已选模式时按钮显示该模式名并高亮", async () => {
    const user = userEvent.setup();
    render(
      <Composer
        {...baseProps}
        collabModes={MODES}
        sessionCollabMode="debate"
        onSessionCollabMode={() => {}}
      />
    );
    const btn = screen.getByTitle(/多 Agent 协作模式/);
    expect(btn).toHaveTextContent("辩论（对抗收敛）");
    expect(btn.className).toContain("active");
    // 选「自动」→ 清除会话覆盖
    await user.click(btn);
    await user.click(screen.getByText("🤝 自动"));
  });

  it("「自动」清除会话覆盖（回调 null）", async () => {
    const onSessionCollabMode = vi.fn();
    const user = userEvent.setup();
    render(
      <Composer
        {...baseProps}
        collabModes={MODES}
        sessionCollabMode="meeting"
        onSessionCollabMode={onSessionCollabMode}
      />
    );
    await user.click(screen.getByTitle(/多 Agent 协作模式/));
    await user.click(screen.getByText("🤝 自动"));
    expect(onSessionCollabMode).toHaveBeenCalledWith(null);
  });

  it("打开选择器时请求刷新模式列表（安装新插件后即时可见）", async () => {
    const onCollabModesRefresh = vi.fn();
    const user = userEvent.setup();
    render(
      <Composer
        {...baseProps}
        collabModes={MODES}
        onCollabModesRefresh={onCollabModesRefresh}
      />
    );
    await user.click(screen.getByTitle(/多 Agent 协作模式/));
    expect(onCollabModesRefresh).toHaveBeenCalledTimes(1);
  });
});
