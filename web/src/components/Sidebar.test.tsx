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
    createEntry: vi.fn(),
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
    deleteFilesBatch: vi.fn(),
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

// ---------------------------------------------------------------- 批量选择删除

describe("Sidebar · 产出物批量选择删除", () => {
  const A = { name: "报告A.docx", path: "产出物/报告A.docx", source: "outputs", size: 1024, mtime: "2026-01-01 10:00" };
  const B = { name: "报告B.xlsx", path: "产出物/报告B.xlsx", source: "outputs", size: 2048, mtime: "2026-01-01 10:01" };

  beforeEach(() => {
    (api.outputs as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      groups: [{ name: "产出物", source: "outputs", items: [A, B] }],
      total: 2,
    });
    (api.deleteFilesBatch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true, deleted: 2, failed: [],
    });
  });

  it("批量模式勾选多项 → 删除所选 → api.deleteFilesBatch 收到全部路径", async () => {
    const user = userEvent.setup();
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<Sidebar {...baseProps} />);

    await screen.findByTitle("产出物/报告A.docx");
    // 进入批量模式（产出物面板头部「☑ 批量」；filter 只留 header 里那个）
    const batchBtn = screen.getAllByTitle("批量选择删除文件")[0];
    await user.click(batchBtn);
    // 单击切换选中（不再触发预览）
    await user.click(screen.getByTitle("产出物/报告A.docx"));
    await user.click(screen.getByTitle("产出物/报告B.xlsx"));
    expect(screen.getByText(/已选 2/)).toBeInTheDocument();
    // 删除所选
    await user.click(screen.getByText(/删除所选/));
    await waitFor(() =>
      expect(api.deleteFilesBatch).toHaveBeenCalledWith(["产出物/报告A.docx", "产出物/报告B.xlsx"])
    );
    confirmSpy.mockRestore();
  });

  it("普通模式单击仍是打开预览，不进批量删除", async () => {
    (api.filePreview as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      name: "x", kind: "text", text: "hi", truncated: false,
    });
    const user = userEvent.setup();
    render(<Sidebar {...baseProps} />);
    await user.click(await screen.findByTitle("产出物/报告A.docx"));
    await waitFor(() => expect(api.filePreview).toHaveBeenCalledWith("产出物/报告A.docx"));
    expect(api.deleteFilesBatch).not.toHaveBeenCalled();
  });
});

describe("Sidebar · 会话列表批量选择删除", () => {
  const sessions = [
    { session_id: "s1", title: "会话一", message_count: 3 },
    { session_id: "s2", title: "会话二", message_count: 5 },
  ];
  const sessionProps = {
    ...baseProps,
    tab: "sessions" as const,
    projectsView: "sessions" as const,
    sessions: sessions as never,
    onDeleteSessions: vi.fn(),
  };

  it("批量模式勾选会话 → 删除所选 → onDeleteSessions 收到全部 id", async () => {
    const user = userEvent.setup();
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<Sidebar {...sessionProps} />);

    await screen.findByText("会话一");
    await user.click(screen.getByTitle("批量选择删除会话"));
    await user.click(screen.getByText("会话一"));
    await user.click(screen.getByText("会话二"));
    expect(screen.getByText(/已选 2/)).toBeInTheDocument();
    await user.click(screen.getByText(/删除所选/));
    expect(sessionProps.onDeleteSessions).toHaveBeenCalledWith(["s1", "s2"]);
    confirmSpy.mockRestore();
  });
});

// ---------------------------------------------------------------- 文件页签：目录右键

describe("Sidebar · 文件页签目录右键菜单", () => {
  const entries = [
    { name: "src", path: "src", type: "dir", has_changes: false },
    { name: "a.txt", path: "a.txt", type: "file", has_changes: false, status: null },
  ];
  const fileProps = { ...baseProps, tab: "files" as const, treeRevision: 1 };

  beforeEach(() => {
    // 只在根目录返回条目：展开子目录返回空，避免同一 path 被重复渲染成自引用树
    (api.workspaceTree as unknown as ReturnType<typeof vi.fn>).mockImplementation(
      async (path: string) => ({
        git: { branch: "main", has_repo: true },
        entries: path === "" ? entries : [],
      })
    );
    (api.createEntry as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true, path: "src/a.txt", name: "a.txt", kind: "file",
    });
  });

  it("目录可右键：菜单含新建文件/新建文件夹/重命名/删除目录", async () => {
    render(<Sidebar {...fileProps} />);
    const dirRow = await screen.findByText("src");

    fireEvent.contextMenu(dirRow.closest(".tree-row")!);

    expect(screen.getByText(/📄 新建文件/)).toBeInTheDocument();
    expect(screen.getByText(/📁 新建文件夹/)).toBeInTheDocument();
    expect(screen.getByText(/重命名/)).toBeInTheDocument();
    expect(screen.getByText(/删除目录/)).toBeInTheDocument();
    // 目录菜单不应出现文件专属项
    expect(screen.queryByText(/用系统默认程序打开/)).toBeNull();
  });

  it("右键目录 → 新建文件 → api.createEntry(parent, name, 'file')", async () => {
    const user = userEvent.setup();
    const promptSpy = vi.spyOn(window, "prompt").mockReturnValue("a.txt");
    render(<Sidebar {...fileProps} />);
    const dirRow = await screen.findByText("src");

    fireEvent.contextMenu(dirRow.closest(".tree-row")!);
    await user.click(screen.getByText(/📄 新建文件/));

    await waitFor(() => expect(api.createEntry).toHaveBeenCalledWith("src", "a.txt", "file"));
    promptSpy.mockRestore();
  });

  it("右键目录 → 新建文件夹 → kind='dir'，取消输入则不调用", async () => {
    const user = userEvent.setup();
    const promptSpy = vi.spyOn(window, "prompt").mockReturnValue("pkg");
    render(<Sidebar {...fileProps} />);
    const dirRow = await screen.findByText("src");

    fireEvent.contextMenu(dirRow.closest(".tree-row")!);
    await user.click(screen.getByText(/📁 新建文件夹/));
    await waitFor(() => expect(api.createEntry).toHaveBeenCalledWith("src", "pkg", "dir"));

    // 取消（null）不产生调用
    (api.createEntry as unknown as ReturnType<typeof vi.fn>).mockClear();
    promptSpy.mockReturnValue(null);
    fireEvent.contextMenu(dirRow.closest(".tree-row")!);
    await user.click(screen.getByText(/📁 新建文件夹/));
    expect(api.createEntry).not.toHaveBeenCalled();
    promptSpy.mockRestore();
  });

  it("右键目录 → 删除目录 → 确认后 deleteFile(path, {recursive:true})", async () => {
    const user = userEvent.setup();
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<Sidebar {...fileProps} />);
    const dirRow = await screen.findByText("src");

    fireEvent.contextMenu(dirRow.closest(".tree-row")!);
    await user.click(screen.getByText(/删除目录/));

    await waitFor(() => expect(api.deleteFile).toHaveBeenCalledWith("src", { recursive: true }));
    confirmSpy.mockRestore();
  });

  it("回归：文件右键仍是文件菜单（无新建项）", async () => {
    render(<Sidebar {...fileProps} />);
    const fileRow = await screen.findByText("a.txt");

    fireEvent.contextMenu(fileRow.closest(".tree-row")!);

    expect(screen.getByText(/重命名/)).toBeInTheDocument();
    expect(screen.queryByText(/📄 新建文件/)).toBeNull();
    expect(screen.queryByText(/删除目录/)).toBeNull();
  });
});

// ------------------------------------------------- 目录消失后文件树不再卡死（回归）

describe("Sidebar · 目录被删除/变化后文件树仍可显示", () => {
  const tree = {
    root: [
      { name: "src", path: "src", type: "dir", has_changes: false },
      { name: "a.txt", path: "a.txt", type: "file", has_changes: false, status: null },
    ] as unknown[],
    srcFails: false,
  };
  const baseFileProps = { ...baseProps, tab: "files" as const };

  beforeEach(() => {
    tree.root = [
      { name: "src", path: "src", type: "dir", has_changes: false },
      { name: "a.txt", path: "a.txt", type: "file", has_changes: false, status: null },
    ];
    tree.srcFails = false;
    (api.workspaceTree as unknown as ReturnType<typeof vi.fn>).mockImplementation(
      async (path: string) => {
        if (path === "") return { git: { branch: "main", has_repo: true }, entries: tree.root };
        if (path === "src") {
          if (tree.srcFails) throw new Error("400 目录不存在: src");
          return { git: { branch: "main", has_repo: true }, entries: [] };
        }
        return { git: { branch: "main", has_repo: true }, entries: [] };
      }
    );
  });

  it("已展开目录被删除：刷新不再进入错误态，且失效路径被剔除", async () => {
    const { rerender } = render(<Sidebar {...baseFileProps} treeRevision={1} />);
    const dirRow = await screen.findByText("src");
    fireEvent.click(dirRow);
    await waitFor(() => expect(api.workspaceTree).toHaveBeenCalledWith("src"));

    // 模拟目录被删除（4xx）+ 根目录不再列出它，然后触发一次刷新
    tree.srcFails = true;
    tree.root = [{ name: "a.txt", path: "a.txt", type: "file", has_changes: false, status: null }];
    rerender(<Sidebar {...baseFileProps} treeRevision={2} />);

    await waitFor(() => expect(screen.getByText("a.txt")).toBeInTheDocument());
    expect(screen.queryByText(/无法读取工作区/)).toBeNull();
    expect(screen.queryByText("src")).toBeNull(); // 行随根目录刷新消失

    // 失效路径已被剔除：后续刷新不再请求它（否则永远失败）
    (api.workspaceTree as unknown as ReturnType<typeof vi.fn>).mockClear();
    rerender(<Sidebar {...baseFileProps} treeRevision={3} />);
    await waitFor(() => expect(api.workspaceTree).toHaveBeenCalled());
    const requested = (api.workspaceTree as unknown as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[0]);
    expect(requested).not.toContain("src");
  });

  it("点击刚被删除的目录：不进入错误态，根目录仍正常显示", async () => {
    tree.srcFails = true; // 该目录的列表接口直接 4xx
    render(<Sidebar {...baseFileProps} treeRevision={1} />);
    const dirRow = await screen.findByText("src");

    fireEvent.click(dirRow);

    await waitFor(() => expect(api.workspaceTree).toHaveBeenCalledWith("src"));
    expect(screen.queryByText(/无法读取工作区/)).toBeNull();
    expect(screen.getByText("a.txt")).toBeInTheDocument();
  });
});
