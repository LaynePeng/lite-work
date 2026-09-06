import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api } from "../api";
import AppIcon from "./AppIcon";
import TerminalPanel from "./TerminalPanel";
import type { OutputItem, RecentProject, SessionInfo, TreeEntry } from "../types";
import { baseName } from "../lib/path";

export type SidebarTab = "sessions" | "files" | "terminal" | "outputs";

// ---------------------------------------------------------------- 目录树

function FileTree({ workspace, revision, onFileOpen }: { workspace: string; revision: number; onFileOpen?: (path: string) => void }) {
  const [dirs, setDirs] = useState<Map<string, TreeEntry[]>>(new Map());
  const [open, setOpen] = useState<Set<string>>(new Set([""]));
  const [branch, setBranch] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const openRef = useRef<Set<string>>(new Set([""]));
  const refreshingRef = useRef(false);

  const loadDir = useCallback(async (path: string) => {
    const r = await api.workspaceTree(path);
    setBranch(r.git.branch);
    setDirs((prev) => new Map(prev).set(path, r.entries));
    return r;
  }, []);

  const refresh = useCallback(async () => {
    if (refreshingRef.current) return;
    refreshingRef.current = true;
    setLoading(true);
    try {
      await Promise.all([...openRef.current].map((p) => loadDir(p)));
      setError(false);
    } catch {
      setError(true);
    } finally {
      setLoading(false);
      refreshingRef.current = false;
    }
  }, [loadDir]);

  // 首次加载根目录
  useEffect(() => {
    void refresh();
  }, [refresh]);

  // 动态刷新：写工具执行 / 任务结束 / 子 Agent 完成后由 App 递增 revision
  useEffect(() => {
    if (revision > 0) void refresh();
  }, [revision, refresh]);

  const toggleDir = useCallback(
    async (path: string) => {
      const cur = openRef.current;
      if (cur.has(path)) {
        cur.delete(path);
        setOpen(new Set(cur));
        return;
      }
      if (!dirs.has(path)) {
        try {
          await loadDir(path);
        } catch {
          setError(true);
          return;
        }
      }
      cur.add(path);
      setOpen(new Set(cur));
    },
    [dirs, loadDir]
  );

  const renderNodes = (path: string, depth: number): ReactNode[] => {
    const nodes = dirs.get(path) ?? [];
    return nodes.map((n) =>
      n.type === "dir" ? (
        <div key={n.path}>
          <div
            className={`tree-row dir ${open.has(n.path) ? "open" : ""}`}
            style={{ paddingLeft: depth * 14 + 8 }}
            title={n.path}
            onClick={() => void toggleDir(n.path)}
          >
            <span className="tree-caret">{open.has(n.path) ? "▾" : "▸"}</span>
            <span className="tree-icon">📁</span>
            <span className="tree-name">{n.name}</span>
            {n.has_changes && <span className="tree-dot" title="包含改动" />}
          </div>
          {open.has(n.path) && renderNodes(n.path, depth + 1)}
        </div>
      ) : (
        <div
          key={n.path}
          className={`tree-row file ${n.status ? `st-${n.status}` : ""}`}
          style={{ paddingLeft: depth * 14 + 8 }}
          title={n.path}
          onDoubleClick={() => onFileOpen?.(n.path)}
        >
          <span className="tree-caret-placeholder" />
          <span className="tree-icon">{n.status === "D" ? "✕" : "📄"}</span>
          <span className="tree-name">{n.name}</span>
          {n.status && <span className="tree-status">{n.status}</span>}
        </div>
      )
    );
  };

  return (
    <div className="files-panel">
      <div className="files-header">
        <span className="files-workspace" title={workspace}>
          📁 {workspace}
        </span>
        <div className="files-header-actions">
          {branch && (
            <span className="git-branch" title="当前分支">
              ⎇ {branch}
            </span>
          )}
          <button
            className={`btn-refresh-tree ${loading ? "spinning" : ""}`}
            onClick={() => void refresh()}
            title="刷新目录树"
          >
            ↻
          </button>
        </div>
      </div>
      {loading && dirs.size === 0 ? (
        <div className="sidebar-empty">加载中…</div>
      ) : error ? (
        <div className="sidebar-empty">（无法读取工作区）</div>
      ) : (
        <div className="file-tree">{renderNodes("", 0)}</div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- 产出物面板（AGI 通用入口：预览/下载 Agent 生成的办公文件）

const OUTPUT_ICONS: Record<string, string> = {
  ".docx": "📄", ".xlsx": "📊", ".pptx": "🎞️", ".pdf": "📕",
  ".png": "🖼️", ".jpg": "🖼️", ".jpeg": "🖼️", ".svg": "🖼️",
};

function fileIcon(name: string) {
  const ext = name.slice(name.lastIndexOf(".")).toLowerCase();
  return OUTPUT_ICONS[ext] ?? "📦";
}

function fmtSize(size: number) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function OutputPreview({ revision }: { revision: number }) {
  const [items, setItems] = useState<OutputItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [preview, setPreview] = useState<import("../types").FilePreviewResponse | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [previewPath, setPreviewPath] = useState("");
  const refreshingRef = useRef(false);

  const refresh = useCallback(async () => {
    if (refreshingRef.current) return;
    refreshingRef.current = true;
    setLoading(true);
    try {
      const r = await api.outputs();
      setItems(r.items);
      setError(false);
    } catch {
      setError(true);
    } finally {
      setLoading(false);
      refreshingRef.current = false;
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // 动态刷新：产出工具执行 / 任务结束后由 App 递增 revision
  useEffect(() => {
    if (revision > 0) void refresh();
  }, [revision, refresh]);

  const openPreview = useCallback(async (path: string) => {
    setPreviewPath(path);
    setPreviewLoading(true);
    setPreviewError("");
    setPreview(null);
    try {
      const r = await api.filePreview(path);
      setPreview(r);
    } catch (err) {
      setPreviewError(err instanceof Error ? err.message : String(err));
    } finally {
      setPreviewLoading(false);
    }
  }, []);

  const closePreview = () => {
    setPreview(null);
    setPreviewPath("");
    setPreviewError("");
  };

  return (
    <div className="files-panel outputs-panel">
      <div className="files-header">
        <div className="files-header-actions">
          <button
            className="btn-ghost-sm"
            title="在系统文件管理器中打开产出物目录（.outputs）"
            onClick={() => {
              const bridge = window.liteWork;
              if (bridge?.openFile) {
                void bridge.openFile(".outputs").then((r) => {
                  if (!r.ok) window.alert(`无法打开目录：${r.error ?? ""}`);
                });
              } else {
                window.alert("在文件管理器中打开目录仅支持桌面应用。");
              }
            }}
          >
            📂 目录
          </button>
          <a
            className="btn-ghost-sm"
            href={api.outputsZipUrl(true)}
            title="打包下载全部产出物与素材（ZIP）"
          >
            ⬇ ZIP
          </a>
          <button
            className="btn-ghost-sm btn-danger-sm"
            onClick={() => {
              if (items.length === 0) return;
              if (!window.confirm(`确认清空全部产出物与素材（${items.length} 个文件）？此操作不可恢复`)) return;
              void api.clearOutputs("all")
                .then(() => void refresh())
                .catch((err) => window.alert(`清空失败：${err instanceof Error ? err.message : err}`));
            }}
            title="清空 .outputs 与 .uploads（不影响代码）"
          >
            🧹 清空
          </button>
          <button
            className={`btn-refresh-tree ${loading ? "spinning" : ""}`}
            onClick={() => void refresh()}
            title="刷新列表"
          >
            ↻
          </button>
        </div>
      </div>
      {loading && items.length === 0 ? (
        <div className="sidebar-empty">加载中…</div>
      ) : error ? (
        <div className="sidebar-empty">（无法读取，请确认已打开项目）</div>
      ) : items.length === 0 ? (
        <div className="sidebar-empty">
          还没有产出物
          <div className="sidebar-empty-sub">
            切换到「办公」或「调研」Agent，让 Agent 生成文档/表格/图表后，文件会出现在这里
          </div>
        </div>
      ) : (
        <div className="file-tree outputs-list">
          {items.map((it) => (
            <div key={it.path} className="tree-row file output-item" title={it.path} onClick={() => void openPreview(it.path)}>
              <span className="tree-caret-placeholder" />
              <span className="tree-icon">{fileIcon(it.name)}</span>
              <span className="tree-name">{it.name}</span>
              <span className="output-meta">
                {it.source === "uploads" ? "素材" : "产出"} · {fmtSize(it.size)} · {it.mtime}
              </span>
              <a
                className="output-download"
                href={api.fileDownloadUrl(it.path)}
                onClick={(e) => e.stopPropagation()}
                title="下载"
              >
                ⬇
              </a>
              <button
                className="output-download output-locate"
                title="在系统文件管理器中定位该文件"
                onClick={(e) => {
                  e.stopPropagation();
                  const bridge = window.liteWork;
                  if (bridge?.showInFolder) {
                    void bridge.showInFolder(it.path).then((r) => {
                      if (!r.ok) window.alert(`无法定位文件：${r.error ?? ""}`);
                    });
                  } else {
                    window.alert("在文件管理器中定位文件仅支持桌面应用。");
                  }
                }}
              >
                📍
              </button>
              <button
                className="output-download output-delete"
                onClick={(e) => {
                  e.stopPropagation();
                  if (!window.confirm(`删除「${it.name}」？`)) return;
                  void api.deleteFile(it.path)
                    .then(() => void refresh())
                    .catch((err) => window.alert(`删除失败：${err instanceof Error ? err.message : err}`));
                }}
                title="删除"
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      )}

      {(previewLoading || previewError || preview) && (
        <div className="preview-overlay" onClick={closePreview}>
          <div className="preview-modal" onClick={(e) => e.stopPropagation()}>
            <div className="preview-modal-header">
              <span className="preview-title">{fileIcon(previewPath)} {baseName(previewPath)}</span>
              <div className="preview-actions">
                <a className="output-download" href={api.fileDownloadUrl(previewPath)} title="下载文件">⬇ 下载</a>
                <button className="preview-close" onClick={closePreview}>✕</button>
              </div>
            </div>
            <div className="preview-modal-body">
              {previewLoading && <div className="sidebar-empty">解析中…</div>}
              {previewError && <div className="sidebar-empty">⚠ {previewError}</div>}
              {preview?.kind === "media" && (
                preview.media_type === "application/pdf" ? (
                  <iframe src={api.fileRawUrl(previewPath)} title={preview.name} className="preview-frame" />
                ) : (
                  <img src={api.fileRawUrl(previewPath)} alt={preview.name} className="preview-image" />
                )
              )}
              {preview?.kind === "table" && (
                <div className="preview-scroll">
                  {preview.rows.length === 0 ? (
                    <div className="sidebar-empty">（空表格）</div>
                  ) : (
                    <table className="preview-table">
                      <thead>
                        <tr>{preview.rows[0].map((c, i) => <th key={i}>{c}</th>)}</tr>
                      </thead>
                      <tbody>
                        {preview.rows.slice(1).map((row, ri) => (
                          <tr key={ri}>{row.map((c, ci) => <td key={ci}>{c}</td>)}</tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                  {preview.truncated && <div className="preview-truncated">仅显示前 100 行</div>}
                </div>
              )}
              {preview?.kind === "text" && (
                <pre className="preview-text">{preview.text}</pre>
              )}
              {preview?.kind === "slides" && (
                <div className="preview-scroll">
                  {preview.slides.map((s, i) => (
                    <div key={i} className="preview-slide">
                      <div className="preview-slide-title">{i + 1}. {s.title || "（无标题）"}</div>
                      {s.bullets.length > 0 && (
                        <ul className="preview-slide-bullets">
                          {s.bullets.map((b, bi) => <li key={bi}>{b}</li>)}
                        </ul>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- 侧边栏

export default function Sidebar({
  sessions,
  activeSessionId,
  workspace,
  tab,
  treeRevision,
  outputRevision,
  version,
  recentProjects,
  projectsView,
  projectKind,
  collapsed,
  onToggleCollapsed,
  onTabChange,
  onSelectSession,
  onOpenSessionWithProject,
  onNewSession,
  onDeleteSession,
  onOpenProject,
  onOpenCode,
  onNewProject,
  onOpenRecent,
  onRemoveRecent,
  onTogglePin,
  onBackToProjects,
  onOpenProjectNewWindow,
  onOpenSettings,
  onOpenAbout,
  onFileOpen,
}: {
  sessions: SessionInfo[];
  activeSessionId: string | null;
  workspace: string;
  tab: SidebarTab;
  treeRevision: number;
  outputRevision: number;
  version: string;
  recentProjects: RecentProject[];
  projectsView: "list" | "sessions";
  projectKind: "code" | "project";
  collapsed?: boolean;
  onToggleCollapsed?: () => void;
  onTabChange: (tab: SidebarTab) => void;
  onSelectSession: (id: string) => void;
  onOpenSessionWithProject: (id: string) => void;
  onNewSession: () => void;
  onDeleteSession: (id: string) => void;
  onOpenProject: () => void;
  onOpenCode: () => void;
  onNewProject?: (git: boolean) => void;
  onOpenRecent: (path: string) => void;
  onRemoveRecent: (path: string) => void;
  onTogglePin: (path: string) => void;
  onBackToProjects: () => void;
  onOpenProjectNewWindow?: () => void;
  onOpenSettings: () => void;
  onOpenAbout: () => void;
  onFileOpen?: (path: string) => void;
}) {
  // 当前项目显示名：路径末段（兼容 / 与 \）；未打开项目时由 App 层保证不进入 sessions 视图
  const projectName = workspace && workspace !== "未打开项目"
    ? baseName(workspace) || workspace
    : "";

  // 最近项目搜索（纯前端过滤：名称/路径子串，大小写不敏感）
  const [projectSearch, setProjectSearch] = useState("");
  const filteredProjects = projectSearch.trim()
    ? recentProjects.filter((p) =>
        p.name.toLowerCase().includes(projectSearch.trim().toLowerCase()) ||
        p.path.toLowerCase().includes(projectSearch.trim().toLowerCase()))
    : recentProjects;

  return (
    <aside className="sidebar">
      <div className="sidebar-brand">
        <span className="brand-logo"><AppIcon size={30} /></span>
        <span className="brand-name">lite-work</span>
        <button className="sidebar-collapse-btn" onClick={onToggleCollapsed} title={collapsed ? "展开侧边栏" : "收起侧边栏"}>
          {collapsed ? "▶" : "◀"}
        </button>
      </div>

      <div className="sidebar-tabs">
        <button className={tab === "sessions" ? "active" : ""} onClick={() => onTabChange("sessions")}>
          项目
        </button>
        <button className={tab === "files" ? "active" : ""} onClick={() => onTabChange("files")}>
          文件
        </button>
        <button className={tab === "outputs" ? "active" : ""} onClick={() => onTabChange("outputs")}>
          产出物
        </button>
        <button className={tab === "terminal" ? "active" : ""} onClick={() => onTabChange("terminal")}>
          终端
        </button>
      </div>

      {/* 终端 Tab 时 body 隐藏，让 .sidebar-terminal 独占 tabs 与 footer 之间的空间 */}
      <div className={`sidebar-body ${tab === "terminal" ? "hidden" : ""}`}>
        {tab === "sessions" && (
          projectsView === "list" ? (
            <div className="projects-list">
              <div className="projects-entries">
                <button className="btn-project-entry" onClick={onOpenProject} title="打开任意目录作为项目（文档/办公/任意工作目录）">
                  <span className="entry-icon">📂</span>
                  <span className="entry-text">
                    <span className="entry-title">打开项目</span>
                    <span className="entry-desc">任意工作目录</span>
                  </span>
                </button>
                <button className="btn-project-entry" onClick={onOpenCode} title="打开代码仓库（git 仓库，用代码 Agent 开发）">
                  <span className="entry-icon">💻</span>
                  <span className="entry-text">
                    <span className="entry-title">打开代码</span>
                    <span className="entry-desc">git 代码仓库</span>
                  </span>
                </button>
                <button className="btn-project-entry" onClick={() => onNewProject?.(false)} title="新建项目目录（不初始化 git）">
                  <span className="entry-icon">📁</span>
                  <span className="entry-text">
                    <span className="entry-title">新建项目</span>
                    <span className="entry-desc">通用项目目录</span>
                  </span>
                </button>
                <button className="btn-project-entry" onClick={() => onNewProject?.(true)} title="新建代码仓库（自动 git init）">
                  <span className="entry-icon">✚</span>
                  <span className="entry-text">
                    <span className="entry-title">新建代码</span>
                    <span className="entry-desc">默认 git 仓库</span>
                  </span>
                </button>
              </div>
              {onOpenProjectNewWindow && (
                <button className="btn-open-session" onClick={onOpenProjectNewWindow} title="在新窗口打开另一个项目">
                  ▣ 新窗口打开项目…
                </button>
              )}
              <div className="projects-recent-title">最近打开</div>
              {recentProjects.length > 3 && (
                <input
                  className="form-input projects-search"
                  placeholder="搜索项目（名称/路径）…"
                  value={projectSearch}
                  onChange={(e) => setProjectSearch(e.target.value)}
                />
              )}
              {recentProjects.length === 0 && (
                <div className="sidebar-empty">
                  还没有打开过项目
                  <div className="sidebar-empty-sub">从上方入口打开或新建你的第一个项目</div>
                </div>
              )}
              {projectSearch.trim() && filteredProjects.length === 0 && (
                <div className="sidebar-empty">没有匹配「{projectSearch.trim()}」的项目</div>
              )}
              {filteredProjects.map((p) => (
                <div
                  key={p.path}
                  className={`recent-project-item ${p.path === workspace ? "active" : ""} ${p.pinned ? "pinned" : ""}`}
                  title={p.path}
                  onClick={() => onOpenRecent(p.path)}
                >
                  <span className="recent-project-icon">
                    {p.pinned ? "📌" : p.kind === "code" ? "💻" : "📁"}
                  </span>
                  <span className="recent-project-name">{p.name}</span>
                  <span className={`recent-project-kind ${p.kind}`}>{p.kind === "code" ? "代码" : "项目"}</span>
                  <button
                    className={`recent-project-pin ${p.pinned ? "pinned" : ""}`}
                    title={p.pinned ? "取消置顶" : "置顶固定"}
                    onClick={(e) => {
                      e.stopPropagation();
                      onTogglePin(p.path);
                    }}
                  >
                    📌
                  </button>
                  <button
                    className="recent-project-remove"
                    title="从列表移除"
                    onClick={(e) => {
                      e.stopPropagation();
                      onRemoveRecent(p.path);
                    }}
                  >
                    ✕
                  </button>
                </div>
              ))}
            </div>
          ) : (
            <div className="sessions-list">
              {/* 二级视图：项目内会话历史（打开项目后进入） */}
              <div className="project-context">
                <button
                  className="btn-back-projects"
                  onClick={onBackToProjects}
                  title="返回项目列表"
                >
                  ← 项目列表
                </button>
                <div className="project-context-name" title={workspace}>
                  {projectKind === "code" ? "💻" : "📁"} {projectName}
                </div>
              </div>
              <button className="btn-new-session" onClick={onNewSession}>
                ＋ 新建会话
              </button>
              {sessions.length === 0 && <div className="sidebar-empty">还没有会话</div>}
              {sessions.map((s) => (
                <div
                  key={s.session_id}
                  className={`session-item ${s.session_id === activeSessionId ? "active" : ""}`}
                  onClick={() => onSelectSession(s.session_id)}
                  onDoubleClick={() => onOpenSessionWithProject(s.session_id)}
                >
                  <div className="session-title">{s.title}</div>
                  <div className="session-meta">
                    {s.message_count} 条消息
                    <button
                      className="session-delete"
                      onClick={(e) => {
                        e.stopPropagation();
                        if (confirm(`删除会话「${s.title}」？`)) onDeleteSession(s.session_id);
                      }}
                    >
                      ✕
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )
        )}

        {tab === "files" && <FileTree workspace={workspace} revision={treeRevision} onFileOpen={onFileOpen} />}

        {tab === "outputs" && <OutputPreview revision={outputRevision} />}
      </div>

      {tab === "terminal" && (
        <div className="sidebar-terminal">
          <TerminalPanel workspace={workspace} />
        </div>
      )}

      <div className="sidebar-footer">
        <button className="btn-open-settings" onClick={onOpenSettings}>
          ⚙️ 设置
        </button>
        <button className="footer-version footer-version-btn" onClick={onOpenAbout} title="关于 lite-work">
          lite-work v{version} · 手写 Agent Harness
        </button>
      </div>
    </aside>
  );
}
