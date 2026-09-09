// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import type { TabItem } from "../types";

export default function TabBar({
  tabs,
  activeTabId,
  onSelect,
  onClose,
}: {
  tabs: TabItem[];
  activeTabId: string;
  onSelect: (id: string) => void;
  onClose: (id: string) => void;
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