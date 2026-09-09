// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import { useEffect } from "react";
import AppIcon from "./AppIcon";

const GITHUB_URL = "https://github.com/LaynePeng/lite-work";

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
            <a href={`${GITHUB_URL}/releases`} target="_blank" rel="noreferrer">📦 版本发布</a>
            <a href={`${GITHUB_URL}/issues`} target="_blank" rel="noreferrer">💬 问题反馈</a>
          </div>

          <div className="about-actions">
            <a className="about-btn-primary" href={GITHUB_URL} target="_blank" rel="noreferrer">打开 GitHub</a>
          </div>

          <div className="about-meta">
            <span>Electron · React 18 · FastAPI · Python 3.11+</span>
            <span>Apache-2.0 License · © 2026 LaynePeng</span>
          </div>
        </div>
      </div>
    </div>
  );
}
