// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api } from "../api";
import AppIcon from "./AppIcon";
import TerminalPanel from "./TerminalPanel";
import type { OutputItem, RecentProject, SessionInfo, TreeEntry, WorktreeStatus } from "../types";
import { baseName } from "../lib/path";
import { isTextLikePath, SYSTEM_FIRST_EXT } from "../lib/fileOpen";

export type SidebarTab = "sessions" | "files" | "terminal" | "outputs";

export interface OpenFileOptions {
  /** 强制用内置查看器（右键「用内置查看」），绕过 markdown 的系统优先规则 */
  forceBuiltin?: boolean;
}

// ---------------------------------------------------------------- 目录树

function FileTree({ workspace, revision, onFileOpen, onDirOpen, onOpenWorktreeSession }: { workspace: string; revision: number; onFileOpen?: (path: string, opts?: OpenFileOptions) => void; onDirOpen?: (path: string) => void; onOpenWorktreeSession?: (sessionId: string) => void }) {
  const [dirs, setDirs] = useState<Map<string, TreeEntry[]>>(new Map());
  const [open, setOpen] = useState<Set<string>>(new Set([""]));
  const [branch, setBranch] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  // 隔离工作树总览（多会话同项目：谁在隔离、谁落后于主分支）
  const [worktrees, setWorktrees] = useState<(WorktreeStatus & { name: string })[]>([]);
  const [mainBranch, setMainBranch] = useState<string>("");
  const [mainHead, setMainHead] = useState<string>("");
  const openRef = useRef<Set<string>>(new Set([""]));
  const refreshingRef = useRef(false);
  // 右键菜单 / 行内重命名（文件页签：删除·重命名工作区文件）
  const [menu, setMenu] = useState<{ x: number; y: number; path: string; name: string } | null>(null);
  const [renamingPath, setRenamingPath] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const menuRef = useRef<HTMLDivElement | null>(null);

  const loadDir = useCallback(async (path: string) => {
    const r = await api.workspaceTree(path);
    setBranch(r.git.branch);
    setDirs((prev) => new Map(prev).set(path, r.entries));
    return r;
  }, []);

  // 刷新进行中又来了新的刷新请求 → 记 pending，结束后补一轮。
  // 否则「in-flight 期间到达的最后一次变更」会被丢掉，面板停在旧状态。
  const pendingRef = useRef(false);
  const refreshRef = useRef<() => void>(() => {});

  const refresh = useCallback(async () => {
    if (refreshingRef.current) {
      pendingRef.current = true;
      return;
    }
    refreshingRef.current = true;
    setLoading(true);
    try {
      const [_, wt] = await Promise.all([
        Promise.all([...openRef.current].map((p) => loadDir(p))),
        // 工作树总览：与目录树一起刷新（多会话隔离状态随时可能变化）
        api.worktreeList().catch(() => null),
      ]);
      if (wt) {
        setWorktrees(wt.worktrees);
        setMainBranch(wt.main_branch ?? "");
        setMainHead(wt.main_head ?? "");
      }
      setError(false);
    } catch {
      setError(true);
    }
    setLoading(false);
    refreshingRef.current = false;
    if (pendingRef.current) {
      pendingRef.current = false;
      refreshRef.current();
    }
  }, [loadDir]);
  refreshRef.current = () => void refresh();

  const cleanWorktrees = useCallback(async () => {
    await api.worktreeClean().catch(() => null);
    await refresh();
  }, [refresh]);

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

  // 右键菜单：点击外部 / 滚动 / Esc 关闭
  useEffect(() => {
    if (!menu) return;
    const onDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenu(null);
    };
    const onScroll = () => setMenu(null);
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setMenu(null); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("scroll", onScroll, true);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("scroll", onScroll, true);
      document.removeEventListener("keydown", onKey);
    };
  }, [menu]);

  /** 删除文件（右键菜单）：确认后调用后端，成功刷新目录树。 */
  const removeFile = (path: string, name: string) => {
    setMenu(null);
    if (!window.confirm(`删除「${name}」？此操作不可恢复`)) return;
    void api.deleteFile(path)
      .then(() => void refresh())
      .catch((err) => window.alert(`删除失败：${err instanceof Error ? err.message : err}`));
  };

  /** 进入行内重命名（预填原名；扩展名不可改，由后端校验兜底）。 */
  const startRename = (path: string, name: string) => {
    setMenu(null);
    setRenamingPath(path);
    setRenameValue(name);
  };

  /** 右键「用系统默认程序打开」：桌面端 shell.openPath；浏览器提示不支持。 */
  const openWithSystem = (path: string, name: string) => {
    setMenu(null);
    const bridge = window.liteWork;
    if (bridge?.openFile) {
      void bridge.openFile(path).then((r) => {
        if (!r.ok) window.alert(`无法打开：${r.error ?? name}`);
      });
    } else {
      window.alert("用系统默认程序打开仅支持桌面应用。");
    }
  };

  /** 提交重命名：成功刷新目录树，失败提示（扩展名/重名等约束由后端返回）。 */
  const commitRename = async (path: string, name: string) => {
    const next = renameValue.trim();
    if (!next || next === name) {
      setRenamingPath(null);
      return;
    }
    try {
      await api.renameFile(path, next);
      setRenamingPath(null);
      void refresh();
    } catch (err) {
      window.alert(`重命名失败：${err instanceof Error ? err.message : err}`);
    }
  };

  const renderNodes = (path: string, depth: number): ReactNode[] => {
    const nodes = dirs.get(path) ?? [];
    return nodes.map((n) =>
      n.type === "dir" ? (
        <div key={n.path}>
          <div
            className={`tree-row dir ${open.has(n.path) ? "open" : ""}`}
            style={{ paddingLeft: depth * 14 + 8 }}
            title={`${n.path}（双击在系统文件管理器中打开）`}
            onClick={() => void toggleDir(n.path)}
            onDoubleClick={() => {
              // 双击目录：系统文件管理器打开；双击产生的两次单击会把
              // 展开状态抵消（展开→折叠），这里恢复展开
              if (!open.has(n.path)) void toggleDir(n.path);
              onDirOpen?.(n.path);
            }}
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
          onContextMenu={(e) => {
            e.preventDefault();
            setMenu({ x: e.clientX, y: e.clientY, path: n.path, name: n.name });
          }}
        >
          <span className="tree-caret-placeholder" />
          <span className="tree-icon">{n.status === "D" ? "✕" : "📄"}</span>
          {renamingPath === n.path ? (
            <input
              className="output-rename-input"
              value={renameValue}
              autoFocus
              onClick={(e) => e.stopPropagation()}
              onChange={(e) => setRenameValue(e.target.value)}
              onBlur={() => setRenamingPath(null)}
              onKeyDown={(e) => {
                if (e.key === "Enter") { e.preventDefault(); void commitRename(n.path, n.name); }
                else if (e.key === "Escape") { e.preventDefault(); setRenamingPath(null); }
              }}
            />
          ) : (
            <span className="tree-name">{n.name}</span>
          )}
          {n.status && <span className="tree-status">{n.status}</span>}
        </div>
      )
    );
  };

  return (
    <div className="files-panel">
      <div className="files-header">
        <span
          className="files-workspace"
          title={`${workspace}（双击在系统文件管理器中打开）`}
          onDoubleClick={() => onDirOpen?.("")}
        >
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

      {/* 分支与工作树：git 风格分支树（始终显示；无 worktree 时给提示） */}
      <div className="wt-overview">
        <div className="wt-overview-head">
          <span>🌿 分支与工作树</span>
          {worktrees.length > 0 && (
            <button
              className="wt-overview-clean"
              title="清理所有无改动的隔离工作树"
              onClick={() => void cleanWorktrees()}
            >
              清理空壳
            </button>
          )}
        </div>
        <div className="wt-graph">
          {/* 当前分支（合并目标） */}
          <div className="wt-graph-main" title="主工作区当前分支：worktree 的改动会合并到这里">
            <span className="wt-graph-branch">{mainBranch || branch || "（非 git 仓库）"}</span>
            {branch && <span className="wt-graph-dot" />}
            <span className="wt-graph-sha">{shortSha(mainHead)}</span>
            {branch && <span className="wt-graph-tag">合并目标</span>}
          </div>
          {worktrees.length > 0 && (
            <div className="wt-graph-list">
              {worktrees.map((w, i) => (
                <button
                  key={w.name}
                  className={`wt-graph-item ${(w.files?.length ?? 0) > 0 ? "dirty" : ""}`}
                  title={`${w.branch ?? w.name}\n基点 ${shortSha(w.base)} → tip ${shortSha(w.head)}\n点击打开该会话：评审卡可「查看 diff / 让 AI 合并 / 丢弃」`}
                  onClick={() => onOpenWorktreeSession?.(w.name)}
                >
                  <span className="wt-graph-connector">{i === worktrees.length - 1 ? "└─" : "├─"}</span>
                  <span className="wt-graph-dot" />
                  <span className="wt-graph-name">{w.name}</span>
                  <span className="wt-graph-meta">
                    {(w.files?.length ?? 0) > 0 ? `${w.files!.length} 变更` : "无改动"}
                    {w.commits ? ` · ${w.commits} 提交` : ""}
                  </span>
                  {w.behind && (
                    <b className="wt-overview-behind"
                      title="基于旧提交：主分支已被其他会话推进，合并时可能需要解决冲突（推荐让 AI 合并）">
                      ⚠
                    </b>
                  )}
                </button>
              ))}
            </div>
          )}
        </div>
        <div className="wt-overview-hint">
          {!branch
            ? "当前项目不是 git 仓库，隔离工作树不可用"
            : worktrees.length === 0
              ? "没有隔离工作树 · 用 /worktree on 开启（改动与主工作区隔离，可审查后合并）"
              : "点击某项打开会话 → 评审卡可「查看 diff / 让 AI 合并 / 丢弃」"}
        </div>
      </div>

      {/* 右键菜单：重命名 / 删除 / 打开方式（工作区文件简单管理） */}
      {menu && (
        <div className="context-menu" ref={menuRef} style={{ left: menu.x, top: menu.y }}>
          <button className="context-menu-item" onClick={() => startRename(menu.path, menu.name)}>
            ✏ 重命名
          </button>
          <button className="context-menu-item" onClick={() => openWithSystem(menu.path, menu.name)}>
            🖥 用系统默认程序打开
          </button>
          {isTextLikePath(menu.path) && (
            <button
              className="context-menu-item"
              onClick={() => {
                onFileOpen?.(menu.path, { forceBuiltin: true });
                setMenu(null);
              }}
            >
              👁 用内置查看
            </button>
          )}
          <button className="context-menu-item danger" onClick={() => removeFile(menu.path, menu.name)}>
            🗑 删除
          </button>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- 产出物面板（AGI 通用入口：预览/下载 Agent 生成的办公文件）

const OUTPUT_ICONS: Record<string, string> = {
  ".docx": "📄", ".doc": "📄", ".xlsx": "📊", ".xls": "📊", ".pptx": "🎞️", ".ppt": "🎞️",
  ".pdf": "📕", ".png": "🖼️", ".jpg": "🖼️", ".jpeg": "🖼️", ".svg": "🖼️", ".gif": "🖼️", ".webp": "🖼️",
  ".md": "📝", ".txt": "📄", ".csv": "📈", ".html": "🌐", ".zip": "🗜️",
  ".mmd": "🧩", ".puml": "🧩",
};

/** 短 SHA（画分支树用）：取前 7 位，空值显示 "-"。 */
function shortSha(sha?: string): string {
  return sha ? sha.slice(0, 7) : "-";
}

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
  const [groups, setGroups] = useState<import("../types").OutputGroup[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [query, setQuery] = useState("");
  const [preview, setPreview] = useState<import("../types").FilePreviewResponse | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [previewPath, setPreviewPath] = useState("");
  const refreshingRef = useRef(false);
  // 右键菜单 / 行内重命名（产出物·素材文件简单管理）
  const [menu, setMenu] = useState<{ x: number; y: number; item: OutputItem } | null>(null);
  const [renamingPath, setRenamingPath] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const menuRef = useRef<HTMLDivElement | null>(null);

  // 同 FileTree：in-flight 期间到达的刷新请求记 pending，结束后补一轮
  const pendingRef = useRef(false);
  const refreshRef = useRef<() => void>(() => {});

  const refresh = useCallback(async () => {
    if (refreshingRef.current) {
      pendingRef.current = true;
      return;
    }
    refreshingRef.current = true;
    setLoading(true);
    try {
      const r = await api.outputs();
      setGroups(r.groups);
      setError(false);
    } catch {
      setError(true);
    } finally {
      setLoading(false);
      refreshingRef.current = false;
      if (pendingRef.current) {
        pendingRef.current = false;
        refreshRef.current();
      }
    }
  }, []);
  refreshRef.current = () => void refresh();

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // 动态刷新：产出工具执行 / 任务结束后由 App 递增 revision
  useEffect(() => {
    if (revision > 0) void refresh();
  }, [revision, refresh]);

  const openPreview = useCallback(async (path: string, opts?: OpenFileOptions) => {
    // markdown：桌面端优先系统默认程序（与文件树同规则）；失败/浏览器静默回退内置预览
    if (!opts?.forceBuiltin) {
      const ext = path.slice(path.lastIndexOf(".")).toLowerCase();
      if (SYSTEM_FIRST_EXT.has(ext)) {
        const bridge = window.liteWork;
        if (bridge?.openFile) {
          const r = await bridge.openFile(path);
          if (r.ok) return;
        }
      }
    }
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

  // 右键菜单：点击外部 / 滚动 / Esc 关闭
  useEffect(() => {
    if (!menu) return;
    const onDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenu(null);
    };
    const onScroll = () => setMenu(null);
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setMenu(null); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("scroll", onScroll, true);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("scroll", onScroll, true);
      document.removeEventListener("keydown", onKey);
    };
  }, [menu]);

  /** 删除单个文件（右键菜单与行内 ✕ 共用）。 */
  const removeItem = (it: OutputItem) => {
    setMenu(null);
    if (!window.confirm(`删除「${it.name}」？`)) return;
    void api.deleteFile(it.path)
      .then(() => void refresh())
      .catch((err) => window.alert(`删除失败：${err instanceof Error ? err.message : err}`));
  };

  /** 进入行内重命名（预填原名；扩展名不可改，由后端校验兜底）。 */
  const startRename = (it: OutputItem) => {
    setMenu(null);
    setRenamingPath(it.path);
    setRenameValue(it.name);
  };

  /** 提交重命名：成功刷新列表，失败提示（扩展名/重名等约束由后端返回）。 */
  const commitRename = async (it: OutputItem) => {
    const next = renameValue.trim();
    if (!next || next === it.name) {
      setRenamingPath(null);
      return;
    }
    try {
      await api.renameFile(it.path, next);
      setRenamingPath(null);
      void refresh();
    } catch (err) {
      window.alert(`重命名失败：${err instanceof Error ? err.message : err}`);
    }
  };

  // 收件箱只展示 Agent 产出物（素材留给文件树/上传区，不混在这里）
  const q = query.trim().toLowerCase();
  const outputsGroups = groups.filter((g) => g.source === "outputs");
  const shownGroups = outputsGroups
    .map((g) => ({ ...g, items: q ? g.items.filter((it) => it.name.toLowerCase().includes(q)) : g.items }))
    .filter((g) => g.items.length > 0);
  const total = outputsGroups.reduce((n, g) => n + g.items.length, 0);
  const isNew = (mtime: string): boolean => {
    const t = new Date(mtime.replace(" ", "T")).getTime();
    return !Number.isNaN(t) && Date.now() - t < 5 * 60_000;
  };

  return (
    <div className="files-panel outputs-panel">
      <div className="files-header">
        <span className="outputs-count">产出物{total > 0 ? ` · ${total}` : ""}</span>
        <div className="files-header-actions">
          <button
            className="output-tool-icon"
            data-tip="在文件管理器中打开产出物目录"
            aria-label="在文件管理器中打开产出物目录"
            onClick={() => {
              const bridge = window.liteWork;
              if (bridge?.openFile) {
                void bridge.openFile("产出物").then((r) => {
                  if (!r.ok) window.alert(`无法打开目录：${r.error ?? ""}`);
                });
              } else {
                window.alert("在文件管理器中打开目录仅支持桌面应用。");
              }
            }}
          >
            📂
          </button>
          <a
            className="output-tool-icon"
            href={api.outputsZipUrl(false)}
            data-tip="打包下载全部产出物（ZIP）"
            aria-label="打包下载全部产出物"
          >
            ⬇
          </a>
          <button
            className="output-tool-icon output-tool-danger"
            data-tip="清空产出物（不影响代码与素材）"
            aria-label="清空产出物"
            onClick={() => {
              if (total === 0) return;
              if (!window.confirm(`确认清空全部产出物（${total} 个文件）？此操作不可恢复`)) return;
              void api.clearOutputs("outputs")
                .then(() => void refresh())
                .catch((err) => window.alert(`清空失败：${err instanceof Error ? err.message : err}`));
            }}
          >
            🧹
          </button>
          <button
            className="output-tool-icon"
            data-tip="刷新列表"
            aria-label="刷新列表"
            onClick={() => void refresh()}
          >
            ↻
          </button>
        </div>
      </div>
      {total > 0 && (
        <div className="outputs-search-row">
          <input
            className="outputs-search"
            placeholder="🔍 搜索文件名…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
      )}
      {loading && total === 0 ? (
        <div className="sidebar-empty">加载中…</div>
      ) : error ? (
        <div className="sidebar-empty">（无法读取，请确认已打开项目）</div>
      ) : total === 0 ? (
        <div className="sidebar-empty">
          还没有产出物
          <div className="sidebar-empty-sub">
            切换到「办公」或「调研」Agent，让 Agent 生成文档/表格/图表后，
            最新的交付物会按类型出现在这里（旧版本在 归档/ 里，不重复展示）
          </div>
        </div>
      ) : shownGroups.length === 0 ? (
        <div className="sidebar-empty">没有匹配「{query}」的文件</div>
      ) : (
        <div className="file-tree outputs-list">
          {shownGroups.map((g) => (
            <div key={`${g.source}-${g.name}`} className="output-group">
              <div className="output-group-head">
                <span>{g.source === "uploads" ? `📥 ${g.name}（素材）` : `📁 ${g.name}`}</span>
                <span className="output-group-count">{g.items.length}</span>
              </div>
              {g.items.map((it) => {
                const isRenaming = renamingPath === it.path;
                return (
                  <div
                    key={it.path}
                    className="tree-row file output-item"
                    title={`${it.path}${it.version ? `（v${it.version}）` : ""}`}
                    onClick={() => { if (!isRenaming) void openPreview(it.path); }}
                    onContextMenu={(e) => {
                      e.preventDefault();
                      setMenu({ x: e.clientX, y: e.clientY, item: it });
                    }}
                  >
                    <span className="tree-icon">{fileIcon(it.name)}</span>
                    {isRenaming ? (
                      <input
                        className="output-rename-input"
                        value={renameValue}
                        autoFocus
                        onClick={(e) => e.stopPropagation()}
                        onChange={(e) => setRenameValue(e.target.value)}
                        onBlur={() => setRenamingPath(null)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") { e.preventDefault(); void commitRename(it); }
                          else if (e.key === "Escape") { e.preventDefault(); setRenamingPath(null); }
                        }}
                      />
                    ) : (
                      <span className="tree-name">{it.name}</span>
                    )}
                    {isNew(it.mtime) && <span className="output-new">NEW</span>}
                    <span className="output-meta">{fmtSize(it.size)}</span>
                  </div>
                );
              })}
            </div>
          ))}
        </div>
      )}

      {/* 右键菜单：下载 / 重命名 / 打开 / 定位 / 删除（产出物文件管理） */}
      {menu && (
        <div className="context-menu" ref={menuRef} style={{ left: menu.x, top: menu.y }}>
          <a
            className="context-menu-item"
            href={api.fileDownloadUrl(menu.item.path)}
            download
            onClick={() => setMenu(null)}
          >
            ⬇ 下载
          </a>
          <button className="context-menu-item" onClick={() => startRename(menu.item)}>
            ✏ 重命名
          </button>
          <button
            className="context-menu-item"
            onClick={() => {
              const it = menu.item;
              setMenu(null);
              const bridge = window.liteWork;
              if (bridge?.openFile) {
                void bridge.openFile(it.path).then((r) => {
                  if (!r.ok) window.alert(`无法打开：${r.error ?? it.name}`);
                });
              } else {
                window.alert("用系统默认程序打开仅支持桌面应用。");
              }
            }}
          >
            🖥 用系统默认程序打开
          </button>
          <button
            className="context-menu-item"
            onClick={() => {
              const it = menu.item;
              setMenu(null);
              void openPreview(it.path, { forceBuiltin: true });
            }}
          >
            👁 用内置查看
          </button>
          <button
            className="context-menu-item"
            onClick={() => {
              const it = menu.item;
              setMenu(null);
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
            📍 在文件管理器中显示
          </button>
          <button className="context-menu-item danger" onClick={() => removeItem(menu.item)}>
            🗑 删除
          </button>
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
  onToggleKind,
  onOpenWorktree,
  onOpenWorktreeSession,
  onBackToProjects,
  onOpenProjectNewWindow,
  onOpenSettings,
  onOpenAbout,
  onFileOpen,
  onDirOpen,
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
  onToggleKind?: (path: string, kind: "code" | "project") => void;
  /** 在隔离工作树中打开项目（新会话 + worktree 模式） */
  onOpenWorktree?: (path: string) => void;
  /** 打开某个 worktree 对应的会话（名字即会话 ID；文件面板区块点击） */
  onOpenWorktreeSession?: (sessionId: string) => void;
  onBackToProjects: () => void;
  onOpenProjectNewWindow?: () => void;
  onOpenSettings: () => void;
  onOpenAbout: () => void;
  onFileOpen?: (path: string, opts?: OpenFileOptions) => void;
  onDirOpen?: (path: string) => void;
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
      <div className={`sidebar-body ${tab === "terminal" ? "hidden" : ""} ${tab === "files" || tab === "outputs" ? "sidebar-body--panel" : ""}`}>
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
                  {onOpenWorktree && (
                    <button
                      className="recent-project-worktree"
                      title="在隔离工作树中打开（新会话：任务在独立分支+目录执行，主工作区不受影响）"
                      onClick={(e) => {
                        e.stopPropagation();
                        onOpenWorktree(p.path);
                      }}
                    >
                      🛡️
                    </button>
                  )}
                  <button
                    className="recent-project-kind-toggle"
                    title={p.kind === "code"
                      ? "标记为普通项目（📁）"
                      : "标记为代码项目（💻）"}
                    onClick={(e) => {
                      e.stopPropagation();
                      onToggleKind?.(p.path, p.kind === "code" ? "project" : "code");
                    }}
                  >
                    ⇄
                  </button>
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

        {tab === "files" && <FileTree workspace={workspace} revision={treeRevision} onFileOpen={onFileOpen} onDirOpen={onDirOpen} onOpenWorktreeSession={onOpenWorktreeSession} />}

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
