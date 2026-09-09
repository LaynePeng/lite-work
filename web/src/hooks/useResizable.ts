// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import { useCallback, useEffect, useRef, useState } from "react";

interface ResizableOptions {
  /** 拖拽轴：col = 左右边界（宽度），row = 上下边界（高度） */
  axis: "col" | "row";
  /** 初始尺寸（px），也是双击重置的默认值 */
  initial: number;
  min: number;
  /** 上限；可用函数在拖拽时动态计算（依赖视口/容器大小） */
  max: number | (() => number);
  /** 拖拽方向反转：分隔条在面板左侧时（如右侧工具面板），向左拖 = 增大 */
  invert?: boolean;
  /** localStorage 持久化键；空字符串则不持久化 */
  storageKey?: string;
}

interface ResizableResult {
  size: number;
  /** 绑定到分隔条 onPointerDown */
  startDrag: (e: React.PointerEvent) => void;
  /** 绑定到分隔条 onDoubleClick，恢复默认尺寸 */
  reset: () => void;
}

function clamp(v: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, v));
}

/**
 * 布局边界拖拽 hook：pointer 事件驱动，min/max 越界收敛，
 * 尺寸持久化到 localStorage，双击可重置。
 */
export function useResizable({ axis, initial, min, max, invert = false, storageKey = "" }: ResizableOptions): ResizableResult {
  const [size, setSize] = useState<number>(() => {
    if (!storageKey) return initial;
    try {
      const raw = localStorage.getItem(storageKey);
      if (raw !== null) {
        const n = Number(raw);
        if (Number.isFinite(n)) return clamp(n, min, typeof max === "number" ? max : max());
      }
    } catch { /* localStorage 不可用时静默降级 */ }
    return initial;
  });

  const sizeRef = useRef(size);
  useEffect(() => { sizeRef.current = size; }, [size]);

  const startDrag = useCallback((e: React.PointerEvent) => {
    if (e.button !== 0) return;
    e.preventDefault();
    const target = e.currentTarget as HTMLElement;
    target.setPointerCapture(e.pointerId);
    target.classList.add("dragging");
    document.body.classList.add(axis === "col" ? "resizing-col" : "resizing-row");

    const startPos = axis === "col" ? e.clientX : e.clientY;
    const startSize = sizeRef.current;

    const onMove = (ev: PointerEvent) => {
      const pos = axis === "col" ? ev.clientX : ev.clientY;
      const delta = (pos - startPos) * (invert ? -1 : 1);
      const upper = typeof max === "number" ? max : max();
      const next = clamp(startSize + delta, min, upper);
      setSize(next);
      sizeRef.current = next;
    };

    const finish = (ev: PointerEvent) => {
      target.classList.remove("dragging");
      document.body.classList.remove("resizing-col", "resizing-row");
      target.removeEventListener("pointermove", onMove as EventListener);
      target.removeEventListener("pointerup", finish as EventListener);
      target.removeEventListener("pointercancel", finish as EventListener);
      try { target.releasePointerCapture(ev.pointerId); } catch { /* 已释放则忽略 */ }
      if (storageKey) {
        try { localStorage.setItem(storageKey, String(sizeRef.current)); } catch { /* 忽略写入失败 */ }
      }
    };

    target.addEventListener("pointermove", onMove as EventListener);
    target.addEventListener("pointerup", finish as EventListener);
    target.addEventListener("pointercancel", finish as EventListener);
  }, [axis, invert, max, min, storageKey]);

  const reset = useCallback(() => {
    const upper = typeof max === "number" ? max : max();
    const next = clamp(initial, min, upper);
    setSize(next);
    sizeRef.current = next;
    if (storageKey) {
      try { localStorage.setItem(storageKey, String(next)); } catch { /* 忽略写入失败 */ }
    }
  }, [initial, max, min, storageKey]);

  return { size, startDrag, reset };
}
