// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import { useState, useCallback } from "react";

export default function PendingQueue({
  items,
  onRemove,
  onReorder,
  onSend,
}: {
  items: string[];
  onRemove: (index: number) => void;
  onReorder: (from: number, to: number) => void;
  onSend: (index: number) => void;
}) {
  const [dragIdx, setDragIdx] = useState<number | null>(null);
  const [dragOverIdx, setDragOverIdx] = useState<number | null>(null);

  const handleDragStart = useCallback((idx: number) => (e: React.DragEvent) => {
    setDragIdx(idx);
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", String(idx));
  }, []);

  const handleDragOver = useCallback((idx: number) => (e: React.DragEvent) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    setDragOverIdx(idx);
  }, []);

  const handleDragLeave = useCallback(() => {
    setDragOverIdx(null);
  }, []);

  const handleDrop = useCallback((idx: number) => (e: React.DragEvent) => {
    e.preventDefault();
    if (dragIdx !== null && dragIdx !== idx) {
      onReorder(dragIdx, idx);
    }
    setDragIdx(null);
    setDragOverIdx(null);
  }, [dragIdx, onReorder]);

  const handleDragEnd = useCallback(() => {
    setDragIdx(null);
    setDragOverIdx(null);
  }, []);

  if (items.length === 0) return null;

  return (
    <div className="pending-queue">
      <div className="pending-queue-header">
        <span className="pending-queue-title">待发送队列（{items.length}）</span>
      </div>
      <div className="pending-queue-list">
        {items.map((item, idx) => (
          <div
            key={idx}
            className={`pending-queue-item ${dragOverIdx === idx ? "drag-over" : ""} ${dragIdx === idx ? "dragging" : ""}`}
            draggable
            onDragStart={handleDragStart(idx)}
            onDragOver={handleDragOver(idx)}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop(idx)}
            onDragEnd={handleDragEnd}
          >
            <span className="pending-queue-drag">⠿</span>
            <span className="pending-queue-text" title={item}>{item}</span>
            <button className="pending-queue-send" onClick={() => onSend(idx)} title="立即发送">➤</button>
            <button className="pending-queue-remove" onClick={() => onRemove(idx)} title="移除">✕</button>
          </div>
        ))}
      </div>
    </div>
  );
}