import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api } from "../api";
import AppIcon from "./AppIcon";
import TerminalPanel from "./TerminalPanel";
import type { OutputItem, SessionInfo, TreeEntry } from "../types";

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

// ---------------------------------------------------------------- 产出物面板（GAI 通用入口：预览/下载 Agent 生成的办公文件）

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
        <span className="files-workspace" title="Agent 产出物与上传素材">
          🗂️ 产出物与素材
        </span>
        <div className="files-header-actions">
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
            </div>
          ))}
        </div>
      )}

      {(previewLoading || previewError || preview) && (
        <div className="preview-overlay" onClick={closePreview}>
          <div className="preview-modal" onClick={(e) => e.stopPropagation()}>
            <div className="preview-modal-header">
              <span className="preview-title">{fileIcon(previewPath)} {previewPath.split("/").pop()}</span>
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
  onTabChange,
  onSelectSession,
  onOpenSessionWithProject,
  onNewSession,
  onDeleteSession,
  onOpenProject,
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
  onTabChange: (tab: SidebarTab) => void;
  onSelectSession: (id: string) => void;
  onOpenSessionWithProject: (id: string) => void;
  onNewSession: () => void;
  onDeleteSession: (id: string) => void;
  onOpenProject: () => void;
  onOpenProjectNewWindow?: () => void;
  onOpenSettings: () => void;
  onOpenAbout: () => void;
  onFileOpen?: (path: string) => void;
}) {
  return (
    <aside className="sidebar">
      <div className="sidebar-brand">
        <span className="brand-logo"><AppIcon size={30} /></span>
        <span className="brand-name">lite-work</span>
      </div>

      <div className="sidebar-tabs">
        <button className={tab === "sessions" ? "active" : ""} onClick={() => onTabChange("sessions")}>
          会话
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
          <div className="sessions-list">            <button className="btn-new-session" onClick={onNewSession}>
              ＋ 新建会话
            </button>
            <button className="btn-open-session" onClick={onOpenProject} title="选择其他项目目录，切换工作区">
              📂 打开项目…
            </button>
            {onOpenProjectNewWindow && (
              <button className="btn-open-session" onClick={onOpenProjectNewWindow} title="在新窗口打开另一个项目">
                ▣ 新窗口打开项目…
              </button>
            )}
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
