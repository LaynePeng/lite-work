// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// # 素材引用面板：办公/调研 Agent 下输入 # 列出 素材/ 文件，Tab/Enter 补全
import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Composer from "./Composer";
import { api } from "../api";

// 面板打开时懒加载命令/技能/素材（与服务端对齐：素材 source=uploads，产出入 source=outputs）
vi.mock("../api", () => ({
  api: {
    commands: vi.fn(),
    skills: vi.fn(),
    outputs: vi.fn(),
  },
}));

// 注意：setup.ts 每个用例后 vi.restoreAllMocks() 会清掉 mock 实现，
// 故 mock 返回值必须放在 beforeEach 里逐用例重设。
beforeEach(() => {
  (api.commands as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ commands: [] });
  (api.skills as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ skills: [] });
  (api.outputs as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
    items: [
      { name: "报表.xlsx", path: "素材/报表.xlsx", source: "uploads", size: 2048, mtime: "2026-01-01 10:00" },
      { name: "数据.csv", path: "素材/数据.csv", source: "uploads", size: 512, mtime: "2026-01-01 10:00" },
      { name: "报告.docx", path: "产出物/报告.docx", source: "outputs", size: 4096, mtime: "2026-01-01 10:00" },
    ],
  });
});

const baseProps = {
  running: false,
  agents: [
    { id: "build", mode: "primary", description: "开发" },
    { id: "office", mode: "primary", description: "办公" },
    { id: "research", mode: "primary", description: "调研" },
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

describe("Composer · # 素材引用面板", () => {
  it("办公 Agent 输入 # 列出 素材/ 文件（不含产出物）", async () => {
    const user = userEvent.setup();
    const { container } = render(<Composer {...baseProps} />);
    const textarea = container.querySelector("textarea")!;
    await user.type(textarea, "#");
    expect(container.querySelector(".command-palette")).not.toBeNull();
    expect(await screen.findByText("#素材/报表.xlsx")).toBeInTheDocument();
    expect(screen.getByText("#素材/数据.csv")).toBeInTheDocument();
    // 产出物不进入 # 候选
    expect(screen.queryByText("#产出物/报告.docx")).not.toBeInTheDocument();
  });

  it("输入 #报 过滤出匹配的素材", async () => {
    const user = userEvent.setup();
    const { container } = render(<Composer {...baseProps} />);
    const textarea = container.querySelector("textarea")!;
    await user.type(textarea, "#报");
    expect(await screen.findByText("#素材/报表.xlsx")).toBeInTheDocument();
    expect(screen.queryByText("#素材/数据.csv")).not.toBeInTheDocument();
  });

  it("Tab 补全为 #素材/报表.xlsx ", async () => {
    const user = userEvent.setup();
    const { container } = render(<Composer {...baseProps} />);
    const textarea = container.querySelector("textarea") as HTMLTextAreaElement;
    await user.type(textarea, "#报");
    await screen.findByText("#素材/报表.xlsx");
    fireEvent.keyDown(textarea, { key: "Tab" });
    expect(textarea.value).toBe("#素材/报表.xlsx ");
  });

  it("调研 Agent 同样支持 # 引用", async () => {
    const user = userEvent.setup();
    const { container } = render(<Composer {...baseProps} currentAgent="research" />);
    const textarea = container.querySelector("textarea")!;
    await user.type(textarea, "#");
    expect(await screen.findByText("#素材/报表.xlsx")).toBeInTheDocument();
  });

  it("build Agent 输入 # 不弹面板（普通文本）", async () => {
    const user = userEvent.setup();
    const { container } = render(<Composer {...baseProps} currentAgent="build" />);
    const textarea = container.querySelector("textarea")!;
    await user.type(textarea, "#");
    expect(container.querySelector(".command-palette")).toBeNull();
  });
});
