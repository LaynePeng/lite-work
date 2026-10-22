// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import { useEffect, useRef } from "react";

import type { SseConnState, TabItem } from "../types";

/** tab 图标：ppt 用演示文稿符号，可与文件页签区分。 */
const TAB_ICON: Record<string, string> = { file: "📄", chat: "💬", ppt: "📊", "ppt-tpl": "✨" };

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
  onOpenTemplate,
}: {
  tabs: TabItem[];
  activeTabId: string;
  onSelect: (id: string) => void;
  onClose: (id: string) => void;
  /** sessionId → 该会话任务的 SSE 连接状态（仅运行中的会话显示） */
  sseStates?: Record<string, SseConnState>;
  /** 打开「从模板做 PPT」页签（此前只藏在空对话页首屏，有历史对话后就找不到） */
  onOpenTemplate?: () => void;
}) {
  const listRef = useRef<HTMLDivElement>(null);

  // 页签多到需要滚动时，切换页签要把它带进可视区——否则新开的页签在栏外，
  // 点「✨ 从模板做 PPT」看起来像没反应。
  useEffect(() => {
    const list = listRef.current;
    if (!list) return;
    const el = list.querySelector<HTMLElement>(`[data-tab-id="${activeTabId}"]`);
    if (!el) return;
    const left = el.offsetLeft;
    const right = left + el.offsetWidth;
    if (left < list.scrollLeft) list.scrollLeft = left;
    else if (right > list.scrollLeft + list.clientWidth) {
      list.scrollLeft = right - list.clientWidth;
    }
  }, [activeTabId, tabs.length]);

  if (tabs.length === 0) return null;

  return (
    <div className="tab-bar">
      <div className="tab-list" ref={listRef}>
        {tabs.map((t) => (
          <div
            key={t.id}
            data-tab-id={t.id}
            // kind 必须加命名空间：此前直接写成裸 kind，ppt-tpl 页签命中了模板
            // 选择器容器的全局 `.ppt-tpl { flex:1; flex-direction:column }`，
            // 结果所有页签被纵向拉伸、页签栏变成两行高（约 74px），
            // 那一条的标题还固守 120px 宽而盒子被 flex:1 压到 21px → 文字溢出。
            className={`tab-item tab-kind-${t.kind} ${t.id === activeTabId ? "active" : ""}`}
            title={t.title}
            onClick={() => onSelect(t.id)}
          >
            <span className="tab-icon">{TAB_ICON[t.kind] ?? "💬"}</span>
            <span className="tab-title">{t.title}</span>
            <SseDot sse={t.sessionId ? sseStates?.[t.sessionId] : undefined} />
            <button
              className="tab-close"
              title="关闭"
              onClick={(e) => { e.stopPropagation(); onClose(t.id); }}
            >
              ✕
            </button>
          </div>
        ))}
      </div>
      {onOpenTemplate && (
        <div className="tab-bar-actions">
          <button
            className="tab-action tab-action-wide"
            onClick={onOpenTemplate}
            title="从模板做 PPT：选版式 → 填内容 → 生成后直接微调"
          >
            <span className="tab-action-icon">✨</span>
            <span className="tab-action-label">从模板做 PPT</span>
          </button>
        </div>
      )}
    </div>
  );
}
