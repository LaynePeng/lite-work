// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import ChatView, { QuestionBar } from "./ChatView";
import type { Msg } from "../types";

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

// ---------------------------------------------------------------- 滚动行为

// jsdom 无真实布局（scrollHeight/clientHeight 恒为 0）：
// 在容器实例上劫持三个滚动属性，驱动贴底判定与 scrollTop 断言
function hijackScrollMetrics(el: HTMLElement, scrollHeight = 1000, clientHeight = 400) {
  let top = 0;
  Object.defineProperty(el, "scrollTop", {
    get: () => top,
    set: (v: number) => { top = v; },
    configurable: true,
  });
  Object.defineProperty(el, "scrollHeight", { get: () => scrollHeight, configurable: true });
  Object.defineProperty(el, "clientHeight", { get: () => clientHeight, configurable: true });
  return {
    get top() { return top; },
    set top(v: number) { top = v; },
  };
}

function getScrollEl() {
  return document.querySelector(".chat-scroll") as HTMLElement;
}

describe("ChatView 滚动跟随", () => {
  it("流式内容增长时持续贴底，用户上翻后暂停，滚回底部恢复", () => {
    const messages = [{ role: "user", content: "请持续输出" }] as Msg[];
    const chunk = (text: string) => ({
      items: [{ type: "text" as const, id: "stream", content: text }],
    });

    const { rerender } = render(
      <ChatView {...baseProps} messages={messages} streaming={chunk("第一段")} />
    );
    const el = getScrollEl();
    const scroll = hijackScrollMetrics(el);

    // 流式刷新 → 贴底（scrollTop = scrollHeight）
    rerender(
      <ChatView {...baseProps} messages={messages} streaming={chunk("第一段\n第二段")} />
    );
    expect(scroll.top).toBe(1000);

    // 用户上翻（距底部 300px > 阈值 48px）→ 暂停跟随
    scroll.top = 300;
    fireEvent.scroll(el);
    rerender(
      <ChatView {...baseProps} messages={messages} streaming={chunk("第一段\n第二段\n第三段")} />
    );
    expect(scroll.top).toBe(300);

    // 滚回底部（距底部 0px）→ 恢复跟随
    scroll.top = 600;
    fireEvent.scroll(el);
    rerender(
      <ChatView {...baseProps} messages={messages} streaming={chunk("第一段\n第二段\n第三段\n第四段")} />
    );
    expect(scroll.top).toBe(1000);
  });

  it("上翻浏览历史时发送新消息，强制回到底部", () => {
    const { rerender } = render(
      <ChatView {...baseProps} messages={[{ role: "user", content: "第一条" }] as Msg[]} streaming={null} />
    );
    const el = getScrollEl();
    const scroll = hijackScrollMetrics(el);

    // 用户上翻 → stick=false
    scroll.top = 200;
    fireEvent.scroll(el);

    // 发送新消息（messages 追加 user 消息）→ 强制贴底
    rerender(
      <ChatView
        {...baseProps}
        messages={[
          { role: "user", content: "第一条" },
          { role: "user", content: "第二条" },
        ] as Msg[]}
        streaming={null}
      />
    );
    expect(scroll.top).toBe(1000);
  });

  it("上翻后流式结束落定，不强制拉回底部", () => {
    const { rerender } = render(
      <ChatView
        {...baseProps}
        messages={[{ role: "user", content: "请输出" }] as Msg[]}
        streaming={{ items: [{ type: "text", id: "stream", content: "流式中" }] }}
      />
    );
    const el = getScrollEl();
    const scroll = hijackScrollMetrics(el);

    // 用户上翻
    scroll.top = 250;
    fireEvent.scroll(el);

    // 任务结束：streaming → null，finalMsg 落入 messages
    rerender(
      <ChatView
        {...baseProps}
        messages={[
          { role: "user", content: "请输出" },
          { role: "assistant", content: "最终回复，很长很长。" },
        ] as Msg[]}
        streaming={null}
      />
    );
    // 上翻浏览历史不被打扰
    expect(scroll.top).toBe(250);
  });
});

// 生成 n 轮（user+assistant 各一条）的长会话消息
function makeLongMessages(turns: number): Msg[] {
  const msgs: Msg[] = [];
  for (let i = 0; i < turns; i++) {
    msgs.push({ role: "user", content: `问题 ${i}` } as Msg);
    msgs.push({ role: "assistant", content: `回答 ${i}` } as Msg);
  }
  return msgs;
}

describe("ChatView 超长会话折叠（P1）", () => {
  it("超过 500 turn：折叠更早历史 + 占位条", () => {
    // 320 对问答 = 640 个 RenderTurn（user 和纯 assistant 各成一个 turn）
    render(
      <ChatView {...baseProps} messages={makeLongMessages(320)} streaming={null} />
    );
    // 640 - 300 = 340 个 turn 被折叠
    expect(screen.getByRole("button", { name: /查看更早的 340 轮对话/ })).toBeInTheDocument();
    // 保留区从 turn 340 开始 = 「问题 170」（每对问答占 2 个 turn）
    expect(screen.getByText("问题 170")).toBeInTheDocument();
    expect(screen.queryByText("问题 169")).not.toBeInTheDocument();
    // 最近的轮次可见
    expect(screen.getByText("问题 319")).toBeInTheDocument();
  });

  it("点击占位条：展开 300 个 turn，剩余折叠数递减", async () => {
    const user = userEvent.setup();
    render(
      <ChatView {...baseProps} messages={makeLongMessages(320)} streaming={null} />
    );
    await user.click(screen.getByRole("button", { name: /查看更早的 340 轮对话/ }));
    // 340 - 300 = 40 仍折叠
    expect(screen.getByRole("button", { name: /查看更早的 40 轮对话/ })).toBeInTheDocument();
    // 展开区已包含更早内容（turn 40 对应「问题 20」）
    expect(screen.getByText("问题 20")).toBeInTheDocument();
    expect(screen.queryByText("问题 19")).not.toBeInTheDocument();
  });

  it("500 turn 以内：不折叠，无占位条", () => {
    // 250 对问答 = 500 turn，恰好不超过阈值
    render(
      <ChatView {...baseProps} messages={makeLongMessages(250)} streaming={null} />
    );
    expect(screen.queryByRole("button", { name: /查看更早的/ })).not.toBeInTheDocument();
    expect(screen.getByText("问题 0")).toBeInTheDocument();
    expect(screen.getByText("问题 249")).toBeInTheDocument();
  });

  it("1000 turn：首屏折叠 700 轮，逐级展开（300/轮）直到全部可见", async () => {
    // 500 对问答 = 1000 turn（对应 ~/.lite-work/sessions/session_demo_fold_1000turns.json）
    const user = userEvent.setup();
    render(
      <ChatView {...baseProps} messages={makeLongMessages(500)} streaming={null} />
    );
    // 首屏：折叠 1000 - 300 = 700 轮，仅保留最近 300 轮（turn 700 起 = 问题 350）
    expect(screen.getByRole("button", { name: /查看更早的 700 轮对话/ })).toBeInTheDocument();
    expect(screen.getByText("问题 350")).toBeInTheDocument();
    expect(screen.queryByText("问题 349")).not.toBeInTheDocument();
    expect(screen.getByText("问题 499")).toBeInTheDocument();

    // 第 1 次展开：+300 → 折叠 400 轮（turn 400 起 = 问题 200）
    await user.click(screen.getByRole("button", { name: /查看更早的 700 轮对话/ }));
    expect(screen.getByRole("button", { name: /查看更早的 400 轮对话/ })).toBeInTheDocument();
    expect(screen.getByText("问题 200")).toBeInTheDocument();
    expect(screen.queryByText("问题 199")).not.toBeInTheDocument();

    // 第 2 次展开：+300 → 折叠 100 轮（turn 100 起 = 问题 50）
    await user.click(screen.getByRole("button", { name: /查看更早的 400 轮对话/ }));
    expect(screen.getByRole("button", { name: /查看更早的 100 轮对话/ })).toBeInTheDocument();
    expect(screen.getByText("问题 50")).toBeInTheDocument();
    expect(screen.queryByText("问题 49")).not.toBeInTheDocument();

    // 第 3 次展开：+300 → 全部可见（问题 0 出现，占位条消失）
    await user.click(screen.getByRole("button", { name: /查看更早的 100 轮对话/ }));
    expect(screen.queryByRole("button", { name: /查看更早的/ })).not.toBeInTheDocument();
    expect(screen.getByText("问题 0")).toBeInTheDocument();
    expect(screen.getByText("问题 499")).toBeInTheDocument();
  });
});

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
