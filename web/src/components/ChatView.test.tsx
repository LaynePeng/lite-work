// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import ChatView, { QuestionBar } from "./ChatView";
import type { Msg } from "../types";

// jsdom 无真实布局，Virtuoso 无法测量视口——mock 为普通列表渲染。
// （虚拟化本身在真实浏览器验证；组件测试验证分组/渲染逻辑）
vi.mock("react-virtuoso", () => ({
  Virtuoso: ({ data, itemContent, components, className }: {
    data: unknown[]; itemContent: (index: number, item: never) => React.ReactNode;
    components?: { Header?: React.ComponentType; Footer?: React.ComponentType };
    className?: string;
  }) => (
    <div className={className}>
      {components?.Header ? <components.Header /> : null}
      {data.map((item, i) => itemContent(i, item as never))}
      {components?.Footer ? <components.Footer /> : null}
    </div>
  ),
}));

const baseProps = {
  sessionId: "session_test",
  sessionTitle: "测试会话",
  running: false,
  turn: 0,
  goal: null,
  loop: null,
  pendingApprovals: [],
  subAgentRecords: [],
  onSend: () => {},
  onStop: () => {},
  onApprove: () => {},
  currentAgent: "build",
};

function makeMsgs(): Msg[] {
  return [
    { role: "system", content: "系统提示（不渲染为气泡）" },
    { role: "user", content: "帮我读取配置文件" },
    {
      role: "assistant",
      content: null,
      tool_calls: [{
        id: "call_1",
        type: "function",
        // execute_command 属"重要工具"→ 独立 ToolCard（可展开参数/结果）
        function: { name: "execute_command", arguments: '{"command": "cat config.toml"}' },
      }],
    },
    { role: "tool", name: "execute_command", tool_call_id: "call_1", content: "[Command OK] 20 行输出" },
    { role: "assistant", content: "读取完成，共 20 行。" },
  ] as unknown as Msg[];
}

describe("ChatView", () => {
  it("空会话渲染欢迎态（无消息气泡）", () => {
    render(
      <ChatView {...baseProps} messages={[]} streaming={null} />
    );
    expect(screen.getByText("lite-work", { exact: false })).toBeInTheDocument();
  });

  it("渲染用户/助手气泡与工具卡片（历史消息分组）", () => {
    render(
      <ChatView {...baseProps} messages={makeMsgs()} streaming={null} />
    );
    expect(screen.getByText("帮我读取配置文件")).toBeInTheDocument();
    expect(screen.getByText(/读取完成，共 20 行。/)).toBeInTheDocument();
    // execute_command 为重要工具 → 独立 ToolCard，默认展开参数与结果
    const card = screen.getByText("execute_command", { exact: false }).closest(".tool-card") as HTMLElement;
    expect(card).toBeTruthy();
    expect(within(card).getByText("参数")).toBeInTheDocument();
    expect(within(card).getByText(/\[Command OK\]/)).toBeInTheDocument();
  });

  it("工具卡片点击头部可折叠/展开参数", async () => {
    const user = userEvent.setup();
    render(
      <ChatView {...baseProps} messages={makeMsgs()} streaming={null} />
    );
    const header = screen.getByText("execute_command", { exact: false }).closest(".tool-card-header") as HTMLElement;
    expect(within(header.parentElement as HTMLElement).getByText("参数")).toBeInTheDocument();
    await user.click(header);
    expect(within(header.parentElement as HTMLElement).queryByText("参数")).not.toBeInTheDocument();
  });

  it("goal 横幅：设置会话目标后展示，loop 开启时带循环进度徽标", () => {
    render(
      <ChatView
        {...baseProps}
        messages={makeMsgs()}
        streaming={null}
        goal="完成 v1.4 发布"
        loop={{ count: 3, max: 10 }}
      />
    );
    expect(screen.getByText("完成 v1.4 发布")).toBeInTheDocument();
    expect(screen.getByText(/第 3\/10 轮/)).toBeInTheDocument();
  });

  it("无 goal 时不渲染横幅", () => {
    render(
      <ChatView {...baseProps} messages={makeMsgs()} streaming={null} goal={null} loop={null} />
    );
    expect(screen.queryByText(/自动推进/)).not.toBeInTheDocument();
  });

  it("审批卡片：pendingApprovals 渲染确认弹层，按钮回调", async () => {
    const onApprove = vi.fn();
    const user = userEvent.setup();
    render(
      <ChatView
        {...baseProps}
        messages={[]}
        streaming={null}
        pendingApprovals={[{ id: "ap1", action: "execute_command: rm -rf build", reason: "高危命令需确认" }]}
        onApprove={onApprove}
      />
    );
    expect(screen.getByText(/execute_command/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "允许执行" }));
    expect(onApprove).toHaveBeenCalledWith("ap1", true);
  });
});

describe("QuestionBar（ask_user 非阻塞提问条）", () => {
  const questions = [{ id: "q1", question: "用哪个数据库？", options: ["PostgreSQL", "SQLite"] }];

  it("无问题时不渲染", () => {
    const { container } = render(
      <QuestionBar pendingQuestions={[]} onAnswerQuestion={() => {}} />
    );
    expect(container.firstChild).toBeNull();
  });

  it("渲染问题与选项，点击选项即回调", async () => {
    const onAnswer = vi.fn();
    const user = userEvent.setup();
    render(<QuestionBar pendingQuestions={questions} onAnswerQuestion={onAnswer} />);
    await user.click(screen.getByRole("button", { name: "PostgreSQL" }));
    expect(onAnswer).toHaveBeenCalledWith("q1", "PostgreSQL");
  });

  it("自定义回答：输入并提交", async () => {
    const onAnswer = vi.fn();
    const user = userEvent.setup();
    render(<QuestionBar pendingQuestions={questions} onAnswerQuestion={onAnswer} />);
    const input = screen.getByPlaceholderText(/自定义回答/);
    await user.type(input, "MySQL");
    await user.click(screen.getByRole("button", { name: "提交回答" }));
    expect(onAnswer).toHaveBeenCalledWith("q1", "MySQL");
  });
});
