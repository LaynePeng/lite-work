// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 产出物 Tab：右键菜单（重命名 / 删除）与行内重命名
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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
    // 文件面板底部「隔离工作树」总览数据源（无工作树 → 空列表）
    worktreeList: vi.fn(async () => ({ worktrees: [], main_branch: "main", main_head: "" })),
    worktreeClean: vi.fn(),
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
  (api.outputs as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ groups: [{ name: "产出物", source: "outputs", items: [ITEM] }], total: 1 });
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

// ---------------------------------------------------------------- 产出物单击打开

describe("Sidebar · 产出物单击打开（与文件树同规则）", () => {
  const DOCX = {
    name: "交底书.docx",
    path: "产出物/交底书.docx",
    source: "outputs",
    size: 2048,
    mtime: "2026-01-01 10:00",
  };

  const mockOutputs = (items: unknown[]) =>
    (api.outputs as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      groups: [{ name: "产出物", source: "outputs", items }],
      total: items.length,
    });

  const mockPreview = () =>
    (api.filePreview as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      name: "x", kind: "text", text: "a,b", truncated: false,
    });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("非文本类（.docx）单击 → 调系统默认程序，不走内置预览", async () => {
    mockOutputs([DOCX]);
    mockPreview();
    const openFile = vi.fn(async () => ({ ok: true }));
    vi.stubGlobal("liteWork", { openFile });

    const user = userEvent.setup();
    render(<Sidebar {...baseProps} />);
    await user.click(await screen.findByTitle("产出物/交底书.docx"));

    await waitFor(() => expect(openFile).toHaveBeenCalledWith("产出物/交底书.docx"));
    expect(api.filePreview).not.toHaveBeenCalled();
  });

  it("文本类（.csv）单击 → 走内置预览，不调系统程序", async () => {
    const csv = { ...DOCX, name: "数据.csv", path: "产出物/数据.csv" };
    mockOutputs([csv]);
    mockPreview();
    const openFile = vi.fn(async () => ({ ok: true }));
    vi.stubGlobal("liteWork", { openFile });

    const user = userEvent.setup();
    render(<Sidebar {...baseProps} />);
    await user.click(await screen.findByTitle("产出物/数据.csv"));

    await waitFor(() => expect(api.filePreview).toHaveBeenCalledWith("产出物/数据.csv"));
    expect(openFile).not.toHaveBeenCalled();
  });

  it("浏览器模式（无 bridge）→ 回落内置预览", async () => {
    mockOutputs([DOCX]);
    mockPreview();

    const user = userEvent.setup();
    render(<Sidebar {...baseProps} />);
    await user.click(await screen.findByTitle("产出物/交底书.docx"));

    await waitFor(() => expect(api.filePreview).toHaveBeenCalledWith("产出物/交底书.docx"));
  });

  it("系统打开失败（非文本类）→ 提示，且不再回落内置预览", async () => {
    mockOutputs([DOCX]);
    mockPreview();
    const openFile = vi.fn(async () => ({ ok: false, error: "没有关联程序" }));
    vi.stubGlobal("liteWork", { openFile });
    const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});

    const user = userEvent.setup();
    render(<Sidebar {...baseProps} />);
    await user.click(await screen.findByTitle("产出物/交底书.docx"));

    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith(expect.stringContaining("没有关联程序")));
    expect(api.filePreview).not.toHaveBeenCalled();
    alertSpy.mockRestore();
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
