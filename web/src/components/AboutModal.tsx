import { useEffect } from "react";
import AppIcon from "./AppIcon";

const GITHUB_URL = "https://github.com/LaynePeng/lite-work";

function GitHubIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor" aria-hidden="true">
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
    </svg>
  );
}

/** 应用图标由共享组件 AppIcon 提供（与 scripts/app-icon.svg 同源） */

export default function AboutModal({ onClose, serverVersion }: { onClose: () => void; serverVersion?: string | null }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const desktopVersion = window.liteWork?.version;
  // 展示版本运行时获取：Core 版本（/api/status）→ 桌面版本（app.getVersion），不再硬编码
  const shownVersion = serverVersion || desktopVersion || "?";

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal about-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>关于 lite-work</h2>
          <button className="modal-close" onClick={onClose} title="关闭 (Esc)">✕</button>
        </div>
        <div className="modal-body about-body">
          <div className="about-logo"><AppIcon size={72} /></div>
          <h3 className="about-name">lite-work</h3>
          <div className="about-version-pill">v{shownVersion}<span className="about-core">Core v{shownVersion}</span></div>
          <p className="about-desc">
            手写内核的 Code Agent 桌面应用——Python 内核 + React UI + Electron 外壳，
            从 LLM 流式解析、上下文压缩到沙箱审批全部纯手写，不依赖 LangChain 等高层框架。
          </p>

          <div className="about-links">
            <a href={GITHUB_URL} target="_blank" rel="noreferrer"><GitHubIcon />GitHub 仓库</a>
            <a href={`${GITHUB_URL}/releases`} target="_blank" rel="noreferrer">📦 版本发布</a>
            <a href={`${GITHUB_URL}/issues`} target="_blank" rel="noreferrer">💬 问题反馈</a>
          </div>

          <div className="about-actions">
            <a className="about-btn-primary" href={GITHUB_URL} target="_blank" rel="noreferrer">打开 GitHub</a>
          </div>

          <div className="about-meta">
            <span>Electron · React 18 · FastAPI · Python 3.11+</span>
            <span>MIT License · © 2026 LaynePeng</span>
          </div>
        </div>
      </div>
    </div>
  );
}
