// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api } from "../api";
import AppIcon from "./AppIcon";
import TerminalPanel from "./TerminalPanel";
import type { RecentProject, SessionInfo, TreeEntry, WorktreeStatus } from "../types";
import { baseName } from "../lib/path";
import { isTextLikePath, resolveOpenTarget } from "../lib/fileOpen";

export type SidebarTab = "sessions" | "tasks" | "files" | "terminal";

export interface OpenFileOptions {
  /** 强制用内置查看器（右键「用内置查看」），绕过 markdown 的系统优先规则 */
  forceBuiltin?: boolean;
}

/** 短 SHA（画分支树用）：取前 7 位，空值显示 "-"。 */
function shortSha(sha?: string): string {
  return sha ? sha.slice(0, 7) : "-";
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
  // 右键菜单 / 行内重命名（文件页签：删除·重命名工作区文件与目录）
  const [menu, setMenu] = useState<{ x: number; y: number; path: string; name: string; isDir: boolean } | null>(null);
  const [renamingPath, setRenamingPath] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const menuRef = useRef<HTMLDivElement | null>(null);
  // 批量选择删除（文件页签）：selectMode 开关 + 已选路径集合
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());

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
    const paths = [...openRef.current];
    // 逐目录独立结算：某个已展开目录被删除/改名后，其请求会 4xx，
    // 若用 Promise.all 则整体 reject → 面板进入"无法读取工作区"死状态，
    // 且 openRef 里的失效路径会让之后每次刷新都失败（只能切 tab 重挂载）。
    // 因此这里 allSettled + 剔除失效目录（含其子孙路径）。
    const [results, wt] = await Promise.all([
      Promise.allSettled(paths.map((p) => loadDir(p))),
      // 工作树总览：与目录树一起刷新（多会话隔离状态随时可能变化）
      api.worktreeList().catch(() => null),
    ]);
    const stale = paths.filter((p, i) => p !== "" && results[i].status === "rejected");
    if (stale.length > 0) {
      stale.forEach((p) => openRef.current.delete(p));
      setDirs((prev) => {
        const next = new Map(prev);
        for (const key of [...next.keys()]) {
          if (stale.some((p) => key === p || key.startsWith(`${p}/`))) next.delete(key);
        }
        return next;
      });
      setOpen(new Set(openRef.current));
    }
    // 只有根目录不可读才算"无法读取工作区"；子目录失效按过期目录处理
    setError(paths.some((p, i) => p === "" && results[i].status === "rejected"));
    if (wt) {
      setWorktrees(wt.worktrees);
      setMainBranch(wt.main_branch ?? "");
      setMainHead(wt.main_head ?? "");
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
          // 目录可能刚被删除/改名：不能把整棵树置为错误态（那会让面板卡在
          // "无法读取工作区"，只能切 tab 恢复），改为剔除该路径并刷新一次，
          // 让这一行从树上自然消失
          openRef.current.delete(path);
          refreshRef.current();
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
    // preventDefault：声明本 Esc 已被右键菜单占用，避免冒泡到全局「Esc 停止任务」
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { e.preventDefault(); setMenu(null); } };
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
      .catch((err) => {
        window.alert(`删除失败：${err instanceof Error ? err.message : err}`);
        void refresh(); // 失败也刷新：目录已被外部删除时（400），树要与磁盘对齐
      });
  };

  /** 删除目录（右键菜单）：连同内容递归删除，二次确认后调用后端。 */
  const removeDir = (path: string, name: string) => {
    setMenu(null);
    if (!window.confirm(`删除目录「${name}」及其全部内容？此操作不可恢复`)) return;
    void api.deleteFile(path, { recursive: true })
      .then(() => void refresh())
      .catch((err) => {
        window.alert(`删除目录失败：${err instanceof Error ? err.message : err}`);
        void refresh(); // 失败也刷新：目录已被外部删除时（400），树要与磁盘对齐
      });
  };
  /** 在指定目录下新建空文件/空目录（右键菜单）：成功后刷新并展开该目录。 */
  const createInDir = async (dirPath: string, kind: "file" | "dir") => {
    setMenu(null);
    const label = kind === "dir" ? "文件夹" : "文件";
    const raw = window.prompt(`在「${dirPath || "工作区根目录"}」下新建${label}，请输入名称：`);
    const name = (raw ?? "").trim();
    if (!name) return;
    try {
      await api.createEntry(dirPath, name, kind);
      await refresh();
      // 新建后展开该目录，让用户直接看到结果（根目录本就展开）
      if (dirPath && !openRef.current.has(dirPath)) await toggleDir(dirPath);
    } catch (err) {
      window.alert(`新建${label}失败：${err instanceof Error ? err.message : err}`);
    }
  };

  /** 复制工作区相对路径到剪贴板（浏览器可能无 clipboard 权限 → 提示路径）。 */
  const copyPath = (path: string) => {
    setMenu(null);
    const clip = navigator.clipboard;
    if (!clip?.writeText) {
      window.alert(`路径：${path}`);
      return;
    }
    void clip.writeText(path).catch(() => window.alert(`路径：${path}`));
  };

  /** 进入行内重命名（预填原名；扩展名不可改，由后端校验兜底）。 */
  const startRename = (path: string, name: string) => {
    setMenu(null);
    setRenamingPath(path);
    setRenameValue(name);
  };

  /** 批量模式：切换某文件选中态。 */
  const toggleSelected = (path: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  /** 退出批量选择并清空已选。 */
  const exitSelectMode = () => {
    setSelectMode(false);
    setSelected(new Set());
  };

  /** 批量删除所选文件：确认 → 后端批量删除 → 部分失败提示（截断前 5 条）→ 刷新并退出。 */
  const deleteSelected = async () => {
    if (selected.size === 0) return;
    if (!window.confirm(`删除所选 ${selected.size} 个文件？此操作不可恢复`)) return;
    try {
      const r = await api.deleteFilesBatch([...selected]);
      if (r.failed.length > 0) {
        const shown = r.failed.slice(0, 5);
        window.alert(
          `已删除 ${r.deleted} 个，${r.failed.length} 个失败：\n${shown.map((f) => `${f.path}：${f.error}`).join("\n")}` +
            (r.failed.length > shown.length ? "\n…" : "")
        );
      }
      await refresh();
    } catch (err) {
      window.alert(`删除失败：${err instanceof Error ? err.message : err}`);
    }
    exitSelectMode();
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
            title={selectMode ? `${n.path}（批量删除不支持目录）` : `${n.path}（双击在系统文件管理器中打开，右键可新建/重命名/删除）`}
            onClick={renamingPath === n.path ? undefined : () => void toggleDir(n.path)}
            onDoubleClick={() => {
              // 双击目录：系统文件管理器打开；双击产生的两次单击会把
              // 展开状态抵消（展开→折叠），这里恢复展开
              if (!open.has(n.path)) void toggleDir(n.path);
              onDirOpen?.(n.path);
            }}
            onContextMenu={(e) => {
              e.preventDefault();
              setMenu({ x: e.clientX, y: e.clientY, path: n.path, name: n.name, isDir: true });
            }}
          >
            <span className="tree-caret">{open.has(n.path) ? "▾" : "▸"}</span>
            <span className="tree-icon">📁</span>
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
            {n.has_changes && <span className="tree-dot" title="包含改动" />}
          </div>
          {open.has(n.path) && renderNodes(n.path, depth + 1)}
        </div>
      ) : (
        <div
          key={n.path}
          className={`tree-row file ${n.status ? `st-${n.status}` : ""} ${selectMode && selected.has(n.path) ? "selected" : ""}`}
          style={{ paddingLeft: depth * 14 + 8 }}
          title={n.path}
          onClick={selectMode ? () => toggleSelected(n.path) : undefined}
          onDoubleClick={selectMode ? undefined : () => onFileOpen?.(n.path)}
          onContextMenu={(e) => {
            e.preventDefault();
            setMenu({ x: e.clientX, y: e.clientY, path: n.path, name: n.name, isDir: false });
          }}
        >
          {selectMode ? (
            <input
              type="checkbox"
              className="batch-checkbox"
              checked={selected.has(n.path)}
              onChange={() => toggleSelected(n.path)}
              onClick={(e) => e.stopPropagation()}
            />
          ) : (
            <span className="tree-caret-placeholder" />
          )}
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
          <button
            className={`btn-batch ${selectMode ? "active" : ""}`}
            onClick={() => { setSelectMode((v) => !v); setSelected(new Set()); }}
            title={selectMode ? "退出批量选择" : "批量选择删除文件"}
          >
            ☑ 批量
          </button>
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
      {selectMode && (
        <div className="batch-bar">
          <span className="batch-count">已选 {selected.size}</span>
          <button className="batch-delete" disabled={selected.size === 0} onClick={() => void deleteSelected()}>
            🗑 删除所选
          </button>
          <button className="batch-cancel" onClick={exitSelectMode}>
            ✕ 取消
          </button>
        </div>
      )}
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

      {/* 右键菜单：目录（新建/重命名/系统打开/复制路径/删除）与文件（重命名/打开方式/删除） */}
      {menu && (
        <div className="context-menu" ref={menuRef} style={{ left: menu.x, top: menu.y }}>
          {menu.isDir ? (
            <>
              <button className="context-menu-item" onClick={() => void createInDir(menu.path, "file")}>
                📄 新建文件
              </button>
              <button className="context-menu-item" onClick={() => void createInDir(menu.path, "dir")}>
                📁 新建文件夹
              </button>
              <button className="context-menu-item" onClick={() => startRename(menu.path, menu.name)}>
                ✏ 重命名
              </button>
              <button className="context-menu-item" onClick={() => { setMenu(null); onDirOpen?.(menu.path); }}>
                🖥 在文件管理器中打开
              </button>
              <button className="context-menu-item" onClick={() => copyPath(menu.path)}>
                📋 复制路径
              </button>
              <button className="context-menu-item danger" onClick={() => removeDir(menu.path, menu.name)}>
                🗑 删除目录
              </button>
            </>
          ) : (
            <>
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
            </>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- 任务 Tab（Goals 视图：Muse/dots 的「任务与对话解耦」）

/** 相对时间：刚刚 / N 分钟前 / N 小时前 / 昨天 / N 天前 / 日期。 */
function relTime(ts: number): string {
  if (!ts) return "";
  const diff = Date.now() - ts;
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  if (diff < 172_800_000) return "昨天";
  if (diff < 7 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`;
  return new Date(ts).toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
}

/** 轨迹抽屉（W7 透出）：列出会话轨迹任务 + 最新一条的事件与 findings。 */
function TrajectoryDrawer({ sessionId, onClose }: { sessionId: string; onClose: () => void }) {
  const [state, setState] = useState<{
    loading: boolean;
    enabled: boolean;
    trajectories: { task_id: string; started_at?: number; events?: number }[];
    detail: { task_id: string; events: { type: string; [k: string]: unknown }[]; findings: { kind: string; message?: string; [k: string]: unknown }[] } | null;
    error: string;
  }>({ loading: true, enabled: false, trajectories: [], detail: null, error: "" });

  const load = useCallback(async () => {
    setState((s) => ({ ...s, loading: true, error: "" }));
    try {
      const r = await api.trajectoryList(sessionId);
      let detail = state.detail;
      if (r.trajectories.length > 0) {
        const latest = r.trajectories[r.trajectories.length - 1];
        const ev = await api.trajectoryEvents(sessionId, latest.task_id, 0, 60);
        detail = { task_id: latest.task_id, events: ev.events, findings: ev.findings };
      }
      setState({ loading: false, enabled: r.enabled, trajectories: r.trajectories, detail, error: "" });
    } catch (err) {
      setState((s) => ({ ...s, loading: false, error: err instanceof Error ? err.message : String(err) }));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  useEffect(() => { void load(); }, [load]);

  const EVENT_ICONS: Record<string, string> = {
    header: "🏁", step: "→", model_call: "🤖", tool_call: "🔧",
    skill_exec: "📦", state_change: "🔄", outcome: "✅",
  };

  return (
    <div className="traj-overlay" onClick={onClose}>
      <div className="traj-drawer" onClick={(e) => e.stopPropagation()}>
        <div className="traj-head">
          <span>📈 执行轨迹（W7）</span>
          <button className="traj-close" onClick={onClose}>✕</button>
        </div>
        {state.loading && <div className="traj-empty">加载中…</div>}
        {!state.loading && state.error && <div className="traj-empty traj-err">✗ {state.error}</div>}
        {!state.loading && !state.error && !state.enabled && (
          <div className="traj-empty">
            轨迹未开启
            <div className="traj-empty-sub">设置 → 效率与诊断机制 → Agent 轨迹，开启后新任务开始记录</div>
          </div>
        )}
        {!state.loading && !state.error && state.enabled && state.trajectories.length === 0 && (
          <div className="traj-empty">开启后尚无已记录的任务</div>
        )}
        {!state.loading && !state.error && state.detail && (
          <>
            <div className="traj-meta">
              {state.trajectories.length} 条轨迹 · 当前：{state.detail.task_id.slice(0, 18)}…
            </div>
            {state.detail.findings.length > 0 && (
              <div className="traj-findings">
                {state.detail.findings.map((f, i) => (
                  <div key={i} className="traj-finding">⚠ {f.kind}{f.message ? `：${f.message}` : ""}</div>
                ))}
              </div>
            )}
            <div className="traj-events">
              {state.detail.events.map((ev, i) => (
                <div key={i} className="traj-event">
                  <span className="traj-ev-icon">{EVENT_ICONS[ev.type] ?? "·"}</span>
                  <span className="traj-ev-type">{ev.type}</span>
                  <span className="traj-ev-detail">
                    {String(ev.tool ?? ev.model ?? ev.summary ?? ev.result ?? "")}
                  </span>
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/** 任务卡（Goals 视图主体）：进度/当前步骤/最近活动/元信息/快捷操作。
 *  宽度纪律：280px 侧栏（--sidebar-w）内设计；标题/活动行 nowrap+ellipsis；
 *  步骤最多 3 行；完成态自动收敛。 */
function TaskCard({ task, active, onSelect, onContinue, onDerive, onDelete, onRename, onTogglePin }: {
  task: SessionInfo;
  active: boolean;
  onSelect: () => void;
  onContinue?: () => void;
  onDerive?: () => void;
  onDelete: () => void;
  /** 重命名（传空串 = 清除自定义名） */
  onRename?: (name: string) => void;
  /** 置顶开关（运行中之后的第二优先级） */
  onTogglePin?: (pinned: boolean) => void;
}) {
  const [trajOpen, setTrajOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState("");
  const p = task.todo_progress;
  const done = p ? p.done >= p.total : false;
  const pct = p && p.total > 0 ? Math.round((p.done / p.total) * 100) : 0;
  const la = task.last_activity;

  return (
    <div className={`task-card ${active ? "active" : ""} ${done ? "done-card" : ""}`} onClick={onSelect}>
      <div className="task-head">
        {task.running ? (
          <span className="running-dot" title="任务运行中" />
        ) : (
          <span className="task-icon">{done ? "✅" : p ? "📊" : "💬"}</span>
        )}
        {renaming ? (
          <input
            className="task-rename-input"
            autoFocus
            value={draft}
            onClick={(e) => e.stopPropagation()}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              e.stopPropagation();
              if (e.key === "Enter") { setRenaming(false); onRename?.(draft.trim()); }
              if (e.key === "Escape") setRenaming(false);
            }}
            onBlur={() => setRenaming(false)}
            placeholder={task.title}
          />
        ) : (
          <span className="task-title" title={task.title}>
            {task.pinned && <span className="task-pin-mark" title="已置顶">📌</span>}
            {task.title}
          </span>
        )}
        {p && <span className={`task-pct ${done ? "done" : ""}`}>{pct}% {p.done}/{p.total}</span>}
      </div>

      {p && !done && (
        <>
          <div className="task-bar"><div className="task-bar-fill" style={{ width: `${pct}%` }} /></div>
          <div className="task-steps">
            {p.current && <div className="task-step cur"><span className="mark">▶</span>{p.current}</div>}
            {p.next.slice(0, p.current ? 2 : 3).map((n, i) => (
              <div key={i} className="task-step"><span className="mark">☐</span>{n}</div>
            ))}
          </div>
        </>
      )}

      {p && done && <div className="task-bar"><div className="task-bar-fill done" style={{ width: "100%" }} /></div>}

      {la && !done && (
        <div className="task-activity" title={la.summary}>
          <span className={la.ok === false ? "err" : "ok"}>{la.ok === false ? "✗" : "✓"}</span>
          <span className="trunc">{la.summary}</span>
          <span className="when">{relTime(task.updated_at)}</span>
        </div>
      )}

      {!done && (
        <div className="task-meta">
          {typeof task.cost_usd === "number" && task.cost_usd > 0 && (
            <span className="cost">${task.cost_usd < 0.01 ? "<0.01" : task.cost_usd}</span>
          )}
          {!!task.subagent_count && <span title="历史子 Agent 数">🤖 {task.subagent_count}</span>}
          <span title={`${task.message_count} 条消息`}>💬 {task.message_count}</span>
          <div className="task-actions">
            <button title={task.pinned ? "取消置顶" : "置顶（排在运行中任务之后）"}
              onClick={(e) => { e.stopPropagation(); onTogglePin?.(!task.pinned); }}>{task.pinned ? "📌" : "📍"}</button>
            {onRename && <button title="重命名"
              onClick={(e) => { e.stopPropagation(); setDraft(""); setRenaming(true); }}>✎</button>}
            <button title="执行轨迹（W7）" onClick={(e) => { e.stopPropagation(); setTrajOpen(true); }}>📈</button>
            {!task.running && onContinue && (
              <button className="btn-continue-card" title="续任务：TODO 有未完成项，继续推进（等同聊天区「继续执行未完成的任务」）"
                onClick={(e) => { e.stopPropagation(); onContinue(); }}>⏭ 续</button>
            )}
            <button className="run" title="进入会话" onClick={(e) => { e.stopPropagation(); onSelect(); }}>进入 ›</button>
            <button title="删除任务" onClick={(e) => { e.stopPropagation(); if (window.confirm(`删除任务「${task.title}」？`)) onDelete(); }}>✕</button>
          </div>
        </div>
      )}

      {done && (
        <div className="task-deliv" title={`完成于 ${relTime(task.updated_at)}`}>
          <span>✓ 全部完成</span>
          {onDerive && (
            <button className="btn-derive-card" title="派生：克隆目标与 TODO 结构到新会话（不复制对话历史）"
              onClick={(e) => { e.stopPropagation(); onDerive(); }}>⤴ 派生</button>
          )}
          <span className="task-done-actions">
            <button title={task.pinned ? "取消置顶" : "置顶"}
              onClick={(e) => { e.stopPropagation(); onTogglePin?.(!task.pinned); }}>{task.pinned ? "📌" : "📍"}</button>
            {onRename && <button title="重命名"
              onClick={(e) => { e.stopPropagation(); setDraft(""); setRenaming(true); }}>✎</button>}
          </span>
          <span style={{ marginLeft: "auto" }}>{relTime(task.updated_at)}</span>
        </div>
      )}

      {trajOpen && <TrajectoryDrawer sessionId={task.session_id} onClose={() => setTrajOpen(false)} />}
    </div>
  );
}

/** 任务 Tab：当前项目的会话按 Goals 视图呈现（运行中在前，其余按更新时间倒序）。 */
function TaskList({ sessions, activeSessionId, workspace, projectKind, projectName, onBackToProjects, onSelectSession, onContinueSession, onDeriveSession, onDeleteSession, onNewSession, onNewTask, onRenameSession, onTogglePinSession }: {
  sessions: SessionInfo[];
  activeSessionId: string | null;
  workspace: string;
  projectKind: "code" | "project";
  projectName: string;
  onBackToProjects: () => void;
  onSelectSession: (id: string) => void;
  onContinueSession?: (id: string, title?: string) => void;
  onDeriveSession?: (id: string, title?: string) => void;
  onDeleteSession: (id: string) => void;
  onNewSession: () => void;
  /** 任务 Tab 专用：打开新建任务向导（优先于 onNewSession） */
  onNewTask?: () => void;
  /** 重命名会话（空串 = 清除自定义名） */
  onRenameSession?: (id: string, name: string) => void;
  /** 置顶开关（metadata.pinned） */
  onTogglePinSession?: (id: string, pinned: boolean) => void;
}) {
  const sorted = [...sessions].sort((a, b) => {
    // 运行中 → 置顶（Goals 视图：正在替我干活的优先可见；用户钉住的其次）
    const run = (Number(b.running ?? false) - Number(a.running ?? false));
    if (run !== 0) return run;
    const pin = (Number(b.pinned ?? false) - Number(a.pinned ?? false));
    if (pin !== 0) return pin;
    return (b.updated_at ?? 0) - (a.updated_at ?? 0);
  });

  return (
    <div className="tasks-list">
      <div className="project-context">
        <button className="btn-back-projects" onClick={onBackToProjects} title="返回项目列表">←</button>
        <div className="project-context-name" title={workspace}>
          {projectKind === "code" ? "💻" : "📁"} {projectName}
        </div>
      </div>
      <button className="btn-new-session" onClick={onNewTask || onNewSession}>＋ 新建任务</button>
      {sorted.length === 0 && (
        <div className="sidebar-empty">
          还没有任务
          <div className="sidebar-empty-sub">新建任务后，Agent 建立了 TODO 看板就会在这里显示进度</div>
        </div>
      )}
      {sorted.map((s) => (
        <TaskCard
          key={s.session_id}
          task={s}
          active={s.session_id === activeSessionId}
          onSelect={() => onSelectSession(s.session_id)}
          onContinue={onContinueSession ? () => onContinueSession(s.session_id, s.title) : undefined}
          onDerive={onDeriveSession ? () => onDeriveSession(s.session_id, s.title) : undefined}
          onDelete={() => onDeleteSession(s.session_id)}
          onRename={onRenameSession ? (name) => onRenameSession(s.session_id, name) : undefined}
          onTogglePin={onTogglePinSession ? (pinned) => onTogglePinSession(s.session_id, pinned) : undefined}
        />
      ))}
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
  version,
  recentProjects,
  projectsView,
  projectKind,
  collapsed,
  onToggleCollapsed,
  onTabChange,
  onSelectSession,
  onContinueSession,
  onDeriveSession,
  onRenameSession,
  onTogglePinSession,
  onOpenSessionWithProject,
  onNewSession,
  onNewTask,
  onDeleteSession,
  onDeleteSessions,
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
  version: string;
  recentProjects: RecentProject[];
  projectsView: "list" | "sessions";
  projectKind: "code" | "project";
  collapsed?: boolean;
  onToggleCollapsed?: () => void;
  onTabChange: (tab: SidebarTab) => void;
  onSelectSession: (id: string) => void;
  /** 任务卡「续」：进入会话并自动续推（App 层复用 AUTO_CONTINUE 发送管线） */
  onContinueSession?: (id: string, title?: string) => void;
  /** 任务卡「派生」：克隆会话骨架到新会话（App 层调 derive API） */
  onDeriveSession?: (id: string, title?: string) => void;
  /** 任务卡重命名（PATCH /api/sessions/{id} name；空串 = 清除自定义名） */
  onRenameSession?: (id: string, name: string) => void;
  /** 任务卡置顶开关（PATCH /api/sessions/{id} pinned；与项目置顶 onTogglePin 区分） */
  onTogglePinSession?: (id: string, pinned: boolean) => void;
  onOpenSessionWithProject: (id: string) => void;
  onNewSession: () => void;
  onDeleteSession: (id: string) => void;
  /** 任务 Tab「＋ 新建任务」：打开任务向导弹窗（区别于项目 Tab 的新建会话） */
  onNewTask?: () => void;
  /** 批量删除会话（App 层调 api.deleteSessionsBatch 并同步页签/会话列表） */
  onDeleteSessions?: (ids: string[]) => void;
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

  // 会话批量选择删除：selectMode 开关 + 已选会话 id 集合（删除交给 App 层批量接口）
  const [sessionSelectMode, setSessionSelectMode] = useState(false);
  const [selectedSessions, setSelectedSessions] = useState<Set<string>>(new Set());
  const toggleSessionSelected = (id: string) => {
    setSelectedSessions((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };
  const exitSessionSelectMode = () => {
    setSessionSelectMode(false);
    setSelectedSessions(new Set());
  };
  /** 批量删除所选会话：确认后交给 App 层（api.deleteSessionsBatch + 页签同步），随后退出选择模式。 */
  const deleteSelectedSessions = () => {
    if (selectedSessions.size === 0) return;
    if (!window.confirm(`删除所选 ${selectedSessions.size} 个会话？此操作不可恢复`)) return;
    onDeleteSessions?.([...selectedSessions]);
    exitSessionSelectMode();
  };

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
        <button className={tab === "tasks" ? "active" : ""} onClick={() => onTabChange("tasks")}>
          任务
        </button>
        <button className={tab === "files" ? "active" : ""} onClick={() => onTabChange("files")}>
          文件
        </button>
        <button className={tab === "terminal" ? "active" : ""} onClick={() => onTabChange("terminal")}>
          终端
        </button>
      </div>

      {/* 终端 Tab 时 body 隐藏，让 .sidebar-terminal 独占 tabs 与 footer 之间的空间 */}
      <div className={`sidebar-body ${tab === "terminal" ? "hidden" : ""} ${tab === "files" || tab === "tasks" ? "sidebar-body--panel" : ""}`}>
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
              <button
                className={`btn-session-batch ${sessionSelectMode ? "active" : ""}`}
                onClick={() => { setSessionSelectMode((v) => !v); setSelectedSessions(new Set()); }}
                title={sessionSelectMode ? "退出批量选择" : "批量选择删除会话"}
              >
                ☑ 批量
              </button>
              {sessionSelectMode && (
                <div className="batch-bar">
                  <span className="batch-count">已选 {selectedSessions.size}</span>
                  <button className="batch-delete" disabled={selectedSessions.size === 0} onClick={deleteSelectedSessions}>
                    🗑 删除所选
                  </button>
                  <button className="batch-cancel" onClick={exitSessionSelectMode}>
                    ✕ 取消
                  </button>
                </div>
              )}
              {sessions.length === 0 && <div className="sidebar-empty">还没有会话</div>}
              {sessions.map((s) => (
                <div
                  key={s.session_id}
                  className={`session-item ${s.session_id === activeSessionId ? "active" : ""} ${sessionSelectMode && selectedSessions.has(s.session_id) ? "selected" : ""}`}
                  onClick={sessionSelectMode ? () => toggleSessionSelected(s.session_id) : () => onSelectSession(s.session_id)}
                  onDoubleClick={sessionSelectMode ? undefined : () => onOpenSessionWithProject(s.session_id)}
                >
                  {sessionSelectMode && (
                    <input
                      type="checkbox"
                      className="batch-checkbox"
                      checked={selectedSessions.has(s.session_id)}
                      onChange={() => toggleSessionSelected(s.session_id)}
                      onClick={(e) => e.stopPropagation()}
                    />
                  )}
                  <div className="session-title">{s.title}</div>
                  <div className="session-meta">
                    {s.message_count} 条消息
                    {!sessionSelectMode && (
                      <button
                        className="session-delete"
                        onClick={(e) => {
                          e.stopPropagation();
                          if (window.confirm(`删除会话「${s.title}」？`)) onDeleteSession(s.session_id);
                        }}
                      >
                        ✕
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )
        )}

        {/* 任务 Tab 的「←」语义 = 回到第一个 Tab（项目）列表：返回项目列表视图的同时切 Tab。
            只调 onBackToProjects 时 projectsView 虽变为 list，但任务卡只在 tab==="tasks"
            分支渲染，点击无可见效果（此前是「点击无效」的 bug）。 */}
        {tab === "tasks" && (
          <TaskList
            sessions={sessions}
            activeSessionId={activeSessionId}
            workspace={workspace}
            projectKind={projectKind ?? "project"}
            projectName={projectName}
            onBackToProjects={() => { onBackToProjects(); onTabChange("sessions"); }}
            onSelectSession={onSelectSession}
            onContinueSession={onContinueSession}
            onDeriveSession={onDeriveSession}
            onDeleteSession={onDeleteSession}
            onNewSession={onNewSession}
            onNewTask={onNewTask}
            onRenameSession={onRenameSession}
            onTogglePinSession={onTogglePinSession}
          />
        )}

        {tab === "files" && <FileTree workspace={workspace} revision={treeRevision} onFileOpen={onFileOpen} onDirOpen={onDirOpen} onOpenWorktreeSession={onOpenWorktreeSession} />}

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
