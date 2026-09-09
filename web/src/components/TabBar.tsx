// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import type { SseConnState, TabItem } from "../types";

/** 会话 tab 的 SSE 连接状态点：任务运行中才显示（绿=正常/黄=重连/红=失联）。 */
function SseDot({ sse }: { sse?: SseConnState }) {
  if (!sse || sse === "idle") return null;
  return <span className={`tab-sse-dot sse-${sse}`} />;
}

export default function TabBar({
  tabs,
  activeTabId,
  onSelect,
  onClose,
  sseStates,
}: {
  tabs: TabItem[];
  activeTabId: string;
  onSelect: (id: string) => void;
  onClose: (id: string) => void;
  /** sessionId → 该会话任务的 SSE 连接状态（仅运行中的会话显示） */
  sseStates?: Record<string, SseConnState>;
}) {
  if (tabs.length === 0) return null;

  return (
    <div className="tab-bar">
      {tabs.map((t) => (
        <div
          key={t.id}
          className={`tab-item ${t.id === activeTabId ? "active" : ""} ${t.kind}`}
          onClick={() => onSelect(t.id)}
        >
          <span className="tab-icon">{t.kind === "file" ? "📄" : "💬"}</span>
          <span className="tab-title">{t.title}</span>
          <SseDot sse={t.sessionId ? sseStates?.[t.sessionId] : undefined} />
          <button
            className="tab-close"
            onClick={(e) => { e.stopPropagation(); onClose(t.id); }}
          >
            ✕
          </button>
        </div>
      ))}
    </div>
  );
}