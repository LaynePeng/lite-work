// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 产出物 Tab：右键菜单（重命名 / 删除）与行内重命名
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Sidebar from "./Sidebar";
import { api } from "../api";

vi.mock("../api", () => ({
  api: {
    outputs: vi.fn(),
    deleteFile: vi.fn(),
    renameFile: vi.fn(),
    filePreview: vi.fn(),
    clearOutputs: vi.fn(),
    workspaceTree: vi.fn(),
    fileDownloadUrl: (p: string) => `/api/files/download?path=${encodeURIComponent(p)}`,
    fileRawUrl: (p: string) => `/api/files/raw?path=${encodeURIComponent(p)}`,
    outputsZipUrl: (includeUploads = false) =>
      `/api/outputs/zip${includeUploads ? "?include_uploads=true" : ""}`,
  },
}));

const ITEM = {
  name: "报表.xlsx",
  path: "素材/报表.xlsx",
  source: "uploads",
  size: 1024,
  mtime: "2026-01-01 10:00",
};

const baseProps = {
  sessions: [],
  activeSessionId: null,
  workspace: "/tmp/ws",
  tab: "outputs" as const,
  treeRevision: 0,
  outputRevision: 0,
  version: "1.8.0",
  recentProjects: [],
  projectsView: "list" as const,
  projectKind: "project" as const,
  onTabChange: () => {},
  onSelectSession: () => {},
  onOpenSessionWithProject: () => {},
  onNewSession: () => {},
  onDeleteSession: () => {},
  onOpenProject: () => {},
  onOpenCode: () => {},
  onOpenRecent: () => {},
  onRemoveRecent: () => {},
  onTogglePin: () => {},
  onBackToProjects: () => {},
  onOpenSettings: () => {},
  onOpenAbout: () => {},
};

beforeEach(() => {
  vi.clearAllMocks();
  (api.outputs as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ items: [ITEM] });
  (api.deleteFile as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, path: ITEM.path });
  (api.renameFile as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
    ok: true, path: "素材/新报表.xlsx", name: "新报表.xlsx",
  });
});

describe("Sidebar · 产出物右键菜单", () => {
  it("右键文件项弹出「重命名 / 删除」菜单", async () => {
    render(<Sidebar {...baseProps} />);
    const row = await screen.findByTitle("素材/报表.xlsx");
    fireEvent.contextMenu(row);
    expect(screen.getByText(/重命名/)).toBeInTheDocument();
    expect(screen.getByText(/删除/)).toBeInTheDocument();
  });

  it("点击「重命名」→ 行内输入 → Enter 调用 api.renameFile", async () => {
    const user = userEvent.setup();
    render(<Sidebar {...baseProps} />);
    const row = await screen.findByTitle("素材/报表.xlsx");
    fireEvent.contextMenu(row);
    await user.click(screen.getByText(/重命名/));
    const input = screen.getByDisplayValue("报表.xlsx") as HTMLInputElement;
    await user.clear(input);
    await user.type(input, "新报表.xlsx");
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() =>
      expect(api.renameFile).toHaveBeenCalledWith("素材/报表.xlsx", "新报表.xlsx")
    );
  });

  it("点击「删除」→ 确认后调用 api.deleteFile", async () => {
    const user = userEvent.setup();
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<Sidebar {...baseProps} />);
    const row = await screen.findByTitle("素材/报表.xlsx");
    fireEvent.contextMenu(row);
    await user.click(screen.getByText(/删除/));
    await waitFor(() => expect(api.deleteFile).toHaveBeenCalledWith("素材/报表.xlsx"));
    confirmSpy.mockRestore();
  });
});

// ---------------------------------------------------------------- 文件页签右键菜单

const fileProps = { ...baseProps, tab: "files" as const };

const NESTED_FILE = { name: "main.py", path: "src/main.py", type: "file" as const, status: "M" };
const SRC_DIR = { name: "src", path: "src", type: "dir" as const };

describe("Sidebar · 文件页签右键菜单", () => {
  beforeEach(() => {
    (api.workspaceTree as unknown as ReturnType<typeof vi.fn>).mockImplementation(
      async (path?: string) => ({
        workspace: "/tmp/ws",
        path: path ?? "",
        git: { branch: "main", has_repo: true },
        entries: path === "src" ? [NESTED_FILE] : [SRC_DIR],
      })
    );
  });

  /** 展开 src/ 目录，让嵌套文件行出现。 */
  async function openNestedFile() {
    const user = userEvent.setup();
    render(<Sidebar {...fileProps} />);
    await user.click(await screen.findByText("src"));
    return screen.findByTitle("src/main.py");
  }

  it("右键文件行弹出「重命名 / 删除」菜单（与产出物面板一致）", async () => {
    const row = await openNestedFile();
    fireEvent.contextMenu(row);
    expect(screen.getByText(/重命名/)).toBeInTheDocument();
    expect(screen.getByText(/删除/)).toBeInTheDocument();
  });

  it("点击「重命名」→ 行内输入 → Enter 调用 api.renameFile（支持嵌套路径）", async () => {
    const user = userEvent.setup();
    const row = await openNestedFile();
    fireEvent.contextMenu(row);
    await user.click(screen.getByText(/重命名/));
    const input = screen.getByDisplayValue("main.py") as HTMLInputElement;
    await user.clear(input);
    await user.type(input, "app.py");
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(api.renameFile).toHaveBeenCalledWith("src/main.py", "app.py"));
  });

  it("点击「删除」→ 确认后调用 api.deleteFile", async () => {
    const user = userEvent.setup();
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const row = await openNestedFile();
    fireEvent.contextMenu(row);
    await user.click(screen.getByText(/删除/));
    await waitFor(() => expect(api.deleteFile).toHaveBeenCalledWith("src/main.py"));
    confirmSpy.mockRestore();
  });
});
