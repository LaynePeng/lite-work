// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 移动壳：两页导航（列表 ↔ 聊天）、会话操作 bottom sheet、推送开关、
// 弹层透传。ChatView/Composer 等重组件全部 mock——它们的移动适配属各组件
// 自身的测试范围。

import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import MobileApp from "./MobileApp";
import type { MobileAppProps } from "./MobileApp";
import type { ChatSessionState, SessionInfo } from "../types";

vi.mock("./ChatView", () => ({
  default: () => <div data-testid="chat-view" />,
  QuestionBar: () => <div data-testid="question-bar" />,
}));
vi.mock("./Composer", () => ({
  default: (props: { onSend: (p: string) => void }) => (
    <div data-testid="composer">
      <button onClick={() => props.onSend("hi")}>mock-send</button>
    </div>
  ),
}));
vi.mock("./NewTaskWizard", () => ({
  default: (props: { onSubmit: () => void }) => (
    <div data-testid="wizard">
      <button onClick={props.onSubmit}>wizard-submit</button>
    </div>
  ),
}));
vi.mock("./SettingsModal", () => ({
  default: () => <div data-testid="settings" />,
}));
vi.mock("./ProjectPicker", () => ({
  default: (props: { onSelect: (p: string) => void; onClose: () => void }) => (
    <div data-testid="project-picker">
      <button onClick={() => props.onSelect("/tmp/picked")}>picker-select</button>
      <button onClick={props.onClose}>picker-close</button>
    </div>
  ),
}));
vi.mock("./WatchToaster", () => ({
  WatchToaster: () => <div data-testid="toaster" />,
}));
vi.mock("../lib/pwa", () => ({
  getPushState: vi.fn(async () => "on"),
  subscribePush: vi.fn(async () => ({ ok: true })),
  unsubscribePush: vi.fn(async () => ({ ok: true })),
  isStandalone: () => false,
}));

const EMPTY_CHAT: ChatSessionState = {
  messages: [],
  streaming: null,
  running: false,
  turn: 0,
  stats: null,
  contextStats: null,
  contextHistory: [],
  error: null,
  pendingApprovals: [],
  subAgentRecords: [],
  agentBoard: [],
  todos: [],
  stalled: false,
  pendingQueue: [],
};

function session(partial: Partial<SessionInfo>): SessionInfo {
  return {
    session_id: "s1",
    created_at: 1,
    updated_at: Date.now() - 60_000,
    message_count: 3,
    title: "会话一",
    metadata: {},
    ...partial,
  };
}

function baseProps(over: Partial<MobileAppProps> = {}): MobileAppProps {
  return {
    workspaceName: "my-project",
    version: "2.0.0",
    workspaceIsGit: false,
    workspace: "/ws/my-project",
    recentProjects: [],
    onSwitchProject: vi.fn(),
    sessions: [session({ session_id: "s1", title: "会话一" })],
    activeSessionId: null,
    onSelectSession: vi.fn(),
    onNewChat: vi.fn(),
    onNewTask: vi.fn(),
    onContinueSession: vi.fn(),
    onRenameSession: vi.fn(),
    onDeleteSession: vi.fn(),
    onTogglePinSession: vi.fn(),
    onRefreshSessions: vi.fn(),
    sessionTitle: "",
    currentChat: EMPTY_CHAT,
    currentAgent: "build",
    onSend: vi.fn(),
    onStop: vi.fn(),
    onApprove: vi.fn(),
    onAnswerQuestion: vi.fn(),
    onContinueTask: vi.fn(),
    onStickChange: vi.fn(),
    onDismissError: vi.fn(),
    onWorktreeDiff: vi.fn(),
    onWorktreeMerge: vi.fn(),
    onWorktreeAiMerge: vi.fn(),
    onWorktreeAbortMerge: vi.fn(),
    onWorktreeDiscard: vi.fn(),
    onQueueRemove: vi.fn(),
    onQueueReorder: vi.fn(),
    onQueueSend: vi.fn(),
    agents: [],
    onSelectAgent: vi.fn(),
    collabModes: [],
    sessionCollabMode: null,
    onSessionCollabMode: vi.fn(),
    onCollabModesRefresh: vi.fn(),
    llmConfig: null,
    sessionModel: null,
    onSessionModelChange: vi.fn(),
    reasoningEffort: "",
    onReasoningEffortChange: vi.fn(),
    scrolledUp: false,
    stopArmed: false,
    success: null,
    onDismissSuccess: vi.fn(),
    newTaskOpen: false,
    onNewTaskCancel: vi.fn(),
    onNewTaskSubmit: vi.fn(),
    settingsOpen: false,
    onOpenSettings: vi.fn(),
    onCloseSettings: vi.fn(),
    onSettingsSaved: vi.fn(),
    watchToasts: [],
    onDismissWatchToast: vi.fn(),
    ...over,
  };
}

describe("MobileApp · 两页导航", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("首页是会话列表：渲染会话卡（标题/运行点/进度）", () => {
    const props = baseProps({
      sessions: [
        session({ session_id: "s1", title: "跑数任务", running: true, todo_progress: { total: 4, done: 2, current: "清洗数据", next: [] } }),
        session({ session_id: "s2", title: "周报", pinned: true }),
      ],
    });
    render(<MobileApp {...props} />);
    expect(screen.getByText("跑数任务")).toBeInTheDocument();
    expect(screen.getByText("周报")).toBeInTheDocument();
    expect(document.querySelector(".mobile-run-dot")).toBeTruthy();
    expect(screen.getByText(/2\/4/)).toBeInTheDocument();
    // 列表页不渲染聊天组件
    expect(screen.queryByTestId("chat-view")).not.toBeInTheDocument();
  });

  it("空列表显示空态引导", () => {
    render(<MobileApp {...baseProps({ sessions: [] })} />);
    expect(screen.getByText("还没有会话")).toBeInTheDocument();
  });

  it("点会话卡：onSelectSession + 进入聊天页；返回键回列表并刷新", () => {
    const props = baseProps({ activeSessionId: "s1", sessionTitle: "会话一" });
    render(<MobileApp {...props} />);

    fireEvent.click(screen.getByText("会话一"));
    expect(props.onSelectSession).toHaveBeenCalledWith("s1");
    expect(screen.getByTestId("chat-view")).toBeInTheDocument();
    expect(screen.getByTestId("composer")).toBeInTheDocument();

    fireEvent.click(screen.getByTitle("返回会话列表"));
    expect(props.onRefreshSessions).toHaveBeenCalled();
    expect(screen.queryByTestId("chat-view")).not.toBeInTheDocument();
  });

  it("聊天页发送：Composer onSend 透传", () => {
    const props = baseProps({ activeSessionId: "s1" });
    render(<MobileApp {...props} />);
    fireEvent.click(screen.getByText("会话一"));
    fireEvent.click(screen.getByText("mock-send"));
    expect(props.onSend).toHaveBeenCalledWith("hi");
  });

  it("「✏️ 对话」等 onNewChat 完成才翻页（无项目时 App 先切个人区）", async () => {
    // App 层在无项目时要先异步切个人区再建会话，翻页必须等它落地
    let resolveNewChat: () => void = () => {};
    const onNewChat = vi.fn(() => new Promise<void>((r) => { resolveNewChat = r; }));
    const props = baseProps({ onNewChat });
    render(<MobileApp {...props} />);

    fireEvent.click(screen.getByText("✏️ 对话"));
    expect(onNewChat).toHaveBeenCalled();
    // 尚未完成：仍停在列表页
    expect(screen.queryByTestId("chat-view")).not.toBeInTheDocument();

    await act(async () => { resolveNewChat(); });
    await screen.findByTestId("chat-view");
  });
});

describe("MobileApp · 会话操作 sheet", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("⋯ 打开 sheet：删除（confirm 确认）→ onDeleteSession", () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const props = baseProps();
    render(<MobileApp {...props} />);

    fireEvent.click(screen.getByRole("button", { name: "会话操作" }));
    fireEvent.click(screen.getByText("🗑 删除"));
    expect(props.onDeleteSession).toHaveBeenCalledWith("s1");
    // sheet 关闭
    expect(screen.queryByText("🗑 删除")).not.toBeInTheDocument();
  });

  it("置顶切换透传 pinned 反值", () => {
    const props = baseProps({ sessions: [session({ pinned: true })] });
    render(<MobileApp {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "会话操作" }));
    fireEvent.click(screen.getByText("📌 取消置顶"));
    expect(props.onTogglePinSession).toHaveBeenCalledWith("s1", false);
  });

  it("交付物 chip：点击带令牌拉文件并走系统分享", async () => {
    const share = vi.fn(async (_data?: { files?: File[]; title?: string }) => {});
    Object.defineProperty(navigator, "canShare", { value: () => true, configurable: true });
    Object.defineProperty(navigator, "share", { value: share, configurable: true });
    const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});
    window.localStorage.setItem("litework.auth.token", "tok-share-123");
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) =>
      new Response(new Uint8Array([0x70, 0x64, 0x66]), {
        headers: { "Content-Type": "application/pdf" },
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const props = baseProps({
      sessions: [session({ deliverables: [{ name: "季度总结.pdf", mtime: 1 }] })],
    });
    render(<MobileApp {...props} />);
    fireEvent.click(screen.getByText(/季度总结\.pdf/));

    await waitFor(() => expect(share).toHaveBeenCalled());
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toContain("/api/files/download?path=");
    const headers = init?.headers as Record<string, string> | undefined;
    expect(headers?.Authorization).toBeDefined();
    expect(share.mock.calls[0]?.[0]?.files?.[0]?.name).toBe("季度总结.pdf");
    expect(alertSpy).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});

describe("MobileApp · 项目切换", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("顶栏 📁 打开项目面板：列出最近项目，点选即切换", () => {
    const props = baseProps({
      workspace: "/ws/my-project",
      recentProjects: [
        { path: "/ws/my-project", name: "my-project", kind: "project", is_git: false, pinned: false, opened_at: "" },
        { path: "/ws/other", name: "other", kind: "code", is_git: true, pinned: false, opened_at: "" },
      ],
    });
    render(<MobileApp {...props} />);

    fireEvent.click(screen.getByTitle("切换项目"));
    expect(screen.getAllByText(/my-project/).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByTitle("/ws/other"));
    expect(props.onSwitchProject).toHaveBeenCalledWith("/ws/other");
    // 点当前项目不触发切换
    fireEvent.click(screen.getByTitle("切换项目"));
    fireEvent.click(screen.getByTitle("/ws/my-project"));
    expect(props.onSwitchProject).toHaveBeenCalledTimes(1);
  });

  it("项目面板「浏览其它目录」：打开 ProjectPicker，选中后切换", () => {
    const props = baseProps();
    render(<MobileApp {...props} />);
    fireEvent.click(screen.getByTitle("切换项目"));
    fireEvent.click(screen.getByText(/浏览其它目录/));
    expect(screen.getByTestId("project-picker")).toBeInTheDocument();
    fireEvent.click(screen.getByText("picker-select"));
    expect(props.onSwitchProject).toHaveBeenCalledWith("/tmp/picked");
    expect(screen.queryByTestId("project-picker")).not.toBeInTheDocument();
  });
});

describe("MobileApp · 推送开关与弹层", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("订阅已开（getPushState=on）：点铃铛走退订", async () => {
    const { unsubscribePush } = await import("../lib/pwa");
    render(<MobileApp {...baseProps()} />);
    // getPushState 是异步初始化，等铃铛出现
    const bell = await screen.findByTitle("关闭推送通知");
    fireEvent.click(bell);
    await waitFor(() => expect(unsubscribePush).toHaveBeenCalled());
  });

  it("HTTP 局域网（insecure）：铃铛可点，提示需 HTTPS 才有推送", async () => {
    const { getPushState } = await import("../lib/pwa");
    vi.mocked(getPushState).mockResolvedValueOnce("insecure");
    const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});
    render(<MobileApp {...baseProps()} />);

    const bell = await screen.findByTitle("推送不可用（HTTP 连接）");
    fireEvent.click(bell);
    expect(alertSpy).toHaveBeenCalledWith(expect.stringContaining("HTTPS"));
    expect(alertSpy).toHaveBeenCalledWith(expect.stringContaining("HTTP"));
  });

  it("newTaskOpen：向导渲染，提交后进入聊天页", () => {
    const props = baseProps({ newTaskOpen: true });
    render(<MobileApp {...props} />);
    fireEvent.click(screen.getByText("wizard-submit"));
    expect(props.onNewTaskSubmit).toHaveBeenCalled();
    expect(screen.getByTestId("chat-view")).toBeInTheDocument();
  });

  it("settingsOpen：设置弹层渲染", () => {
    render(<MobileApp {...baseProps({ settingsOpen: true })} />);
    expect(screen.getByTestId("settings")).toBeInTheDocument();
  });
});
