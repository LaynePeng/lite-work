import { useCallback, useEffect, useState } from "react";
import { api } from "../api";

interface FsEntry {
  path: string;
  parent: string | null;
  home: string;
  is_workspace: boolean;
  dirs: string[];
  files: string[];
  truncated: boolean;
}

export default function ProjectPicker({
  initialPath,
  initialCreate = false,
  onClose,
  onSelect,
}: {
  initialPath: string;
  /** true 时打开即展开「新建项目」表单（新建代码/新建项目入口） */
  initialCreate?: boolean;
  onClose: () => void;
  onSelect: (path: string) => void;
}) {
  const [current, setCurrent] = useState(initialPath);
  const [entry, setEntry] = useState<FsEntry | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  // 默认隐藏 . 开头的隐藏文件/目录（可切换）
  const [showHidden, setShowHidden] = useState(false);

  // 新建项目状态
  const [showCreate, setShowCreate] = useState(initialCreate);
  const [newName, setNewName] = useState("");
  const [newGit, setNewGit] = useState(true);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const load = useCallback(async (path: string, hidden: boolean) => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.fsList(path, hidden);
      setEntry(data);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(current, showHidden);
  }, [current, showHidden, load]);

  const enter = (dir: string) => {
    // Windows 盘符（如 "C:\"）：直接切换到该盘根目录
    if (/^[A-Za-z]:[\\/]?$/.test(dir)) {
      setCurrent(dir);
      return;
    }
    setCurrent(entry ? `${entry.path}/${dir}` : `${current}/${dir}`);
  };

  const goHome = () => setCurrent(entry?.home ?? "~");
  const goUp = () => {
    if (entry?.parent) setCurrent(entry.parent);
  };

  const breadcrumb = (current || "").split("/").filter(Boolean);
  const jumpTo = (idx: number) => {
    const path = "/" + breadcrumb.slice(0, idx + 1).join("/");
    setCurrent(path);
  };

  const submitCreate = async () => {
    const name = newName.trim();
    if (!name) {
      setCreateError("请输入项目名");
      return;
    }
    setCreating(true);
    setCreateError(null);
    try {
      const parent = entry?.path ?? current;
      const r = await api.createProject(parent, name, newGit);
      // 创建成功：进入新项目目录并收起表单
      setShowCreate(false);
      setNewName("");
      setCurrent(r.path);
    } catch (e) {
      setCreateError((e as Error).message);
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal project-picker" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>打开项目</h3>
          <button className="modal-close" onClick={onClose}>✕</button>
        </div>

        <div className="picker-pathbar">
          <button className="picker-nav" onClick={goHome} title="主目录">🏠</button>
          <button className="picker-nav" onClick={goUp} title="上级目录">⬆</button>
          <div className="picker-breadcrumb">
            {breadcrumb.map((seg, i) => (
              <span key={i}>
                <button className="crumb" onClick={() => jumpTo(i)}>{seg}</button>
                {i < breadcrumb.length - 1 && <span className="crumb-sep">/</span>}
              </span>
            ))}
          </div>
          <button
            className="btn-ghost-sm"
            onClick={() => { setShowCreate((v) => !v); setCreateError(null); }}
            title="在当前目录下新建项目"
          >
            ＋ 新建项目
          </button>
          <button
            className={`btn-ghost-sm ${showHidden ? "active" : ""}`}
            onClick={() => setShowHidden((v) => !v)}
            title={showHidden ? "当前：显示隐藏文件（. 开头）" : "当前：隐藏 . 开头的文件与目录，点击切换"}
          >
            {showHidden ? "👁 隐藏文件" : "🚫 隐藏文件"}
          </button>
        </div>

        {showCreate && (
          <div className="picker-create">
            <input
              className="form-input picker-create-name"
              placeholder={`项目名（在 ${entry?.path ?? current} 下创建）`}
              value={newName}
              autoFocus
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") void submitCreate(); }}
              disabled={creating}
            />
            <label className="picker-create-git" title="新项目默认初始化为 git 仓库，侧边栏可直接展示分支与文件状态">
              <input type="checkbox" checked={newGit} onChange={(e) => setNewGit(e.target.checked)} />
              初始化 git（推荐）
            </label>
            <button className="btn-primary" onClick={() => void submitCreate()} disabled={creating}>
              {creating ? "创建中…" : "创建"}
            </button>
          </div>
        )}
        {createError && <div className="picker-error">⚠ {createError}</div>}

        {error && <div className="picker-error">⚠ {error}</div>}

        <div className="picker-tree">
          {loading ? (
            <div className="picker-empty">加载中…</div>
          ) : entry ? (
            <>
              {entry.dirs.length === 0 && entry.files.length === 0 && (
                <div className="picker-empty">（空目录）</div>
              )}
              {entry.dirs.map((d) => (
                <button key={d} className="picker-row dir" onDoubleClick={() => enter(d)} onClick={() => enter(d)}>
                  <span className="picker-ico">📁</span>
                  <span className="picker-name">{d}</span>
                </button>
              ))}
              {entry.files.map((f) => (
                <div key={f} className="picker-row file" title={f}>
                  <span className="picker-ico">📄</span>
                  <span className="picker-name">{f}</span>
                </div>
              ))}
              {entry.truncated && <div className="picker-empty">…（条目过多，仅显示前 500 项）</div>}
            </>
          ) : null}
        </div>

        <div className="picker-footer">
          <span className="picker-path-label">{entry?.path ?? current}</span>
          <div className="picker-actions">
            <button className="btn-ghost" onClick={onClose}>取消</button>
            <button
              className="btn-primary"
              disabled={!entry}
              onClick={() => entry && onSelect(entry.path)}
            >
              选择此目录
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
