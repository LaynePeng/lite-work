// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

interface ResizableOptions {
  /** 拖拽轴：col = 左右边界（宽度），row = 上下边界（高度） */
  axis: "col" | "row";
  /**
   * 初始/默认尺寸（px），或返回尺寸的函数（如按窗口宽度取比例）。
   * 传函数时：在**未被用户手动调整过**的前提下，窗口 resize 会按它实时重算
   * ——修掉「一次性 innerWidth 快照」在挂载当口窗口尺寸未定（如未最大化）
   * 时算出错误默认值、且此后永不跟随窗口的问题。
   */
  initial: number | (() => number);
  min: number;
  /** 上限；可用函数在拖拽时动态计算（依赖视口/容器大小） */
  max: number | (() => number);
  /** 拖拽方向反转：分隔条在面板左侧时（如右侧工具面板），向左拖 = 增大 */
  invert?: boolean;
  /**
   * 持久化（如后端 /api/config）。**配置一律存后端，不走 localStorage**：
   * Electron 每次启动本地 Core 端口随机（serve --port 0），渲染层 origin 随之
   * 变化，localStorage 按 origin 隔离会读不回（「改了、重开又变回去」）。
   * `load` 必须**同步**返回（读内存缓存即可）；`save(null)` 表示清除
   * （双击重置回默认）。不传则不持久化。
   */
  persist?: {
    load: () => number | null;
    save: (size: number | null) => void;
  };
  /**
   * 启动时从后端异步读回的已保存值。就绪后一次性应用，并用 useLayoutEffect
   * 在首帧绘制前生效，避免「先默认尺寸、再跳变」的闪烁。
   */
  hydrated?: { ready: boolean; value: number | null };
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
 * 尺寸持久化经 `persist`（后端 config），双击重置。
 *
 * 尺寸有两种来源：
 *   - 手动：用户拖拽/读到持久值 → 固定像素，不随窗口变化；
 *   - 自动：无持久值 → 按 initial()（可为比例）随窗口 resize 实时重算。
 * 双击分隔条回到自动模式（并清除持久值）。
 */
export function useResizable({
  axis,
  initial,
  min,
  max,
  invert = false,
  persist,
  hydrated,
}: ResizableOptions): ResizableResult {
  const upper = useCallback(() => (typeof max === "number" ? max : max()), [max]);
  const resolveDefault = useCallback(
    () => clamp(typeof initial === "number" ? initial : initial(), min, upper()),
    [initial, min, upper],
  );

  const readStored = useCallback((): number | null => {
    const stored = persist?.load();
    return stored != null && Number.isFinite(stored) ? stored : null;
  }, [persist]);

  // 挂载时只读一次持久值（决定初值 + 是否处于「手动」模式）
  const storedRef = useRef<number | null | undefined>(undefined);
  if (storedRef.current === undefined) storedRef.current = readStored();

  // 手动模式：用户拖拽过 / 读到过持久值。自动模式下窗口 resize 会重算默认尺寸。
  const manualRef = useRef<boolean>(storedRef.current != null);

  const [size, setSize] = useState<number>(() => {
    const stored = storedRef.current;
    return stored == null ? resolveDefault() : clamp(stored, min, upper());
  });

  const sizeRef = useRef(size);
  useEffect(() => { sizeRef.current = size; }, [size]);

  // 自动模式：跟随窗口尺寸实时重算（手动拖过则不再干预用户选择）
  useEffect(() => {
    const onResize = () => {
      if (manualRef.current) return;
      const next = resolveDefault();
      sizeRef.current = next;
      setSize(next);
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [resolveDefault]);

  // 后端持久值异步就绪：首帧绘制前应用（无闪烁）。
  // 无持久值则重算一次默认值——此时后端已就绪、窗口尺寸已稳定
  // （首个打开窗口未最大化的场景可在此纠正）。
  const hydratedAppliedRef = useRef(false);
  useLayoutEffect(() => {
    if (hydratedAppliedRef.current || !hydrated?.ready) return;
    hydratedAppliedRef.current = true;
    if (hydrated.value != null && Number.isFinite(hydrated.value)) {
      // 用户已自行调整过（拖拽 / 同 origin 缓存）则不回退到后端旧值
      if (!manualRef.current) {
        const next = clamp(hydrated.value, min, upper());
        manualRef.current = true;
        sizeRef.current = next;
        setSize(next);
      }
    } else if (!manualRef.current) {
      const next = resolveDefault();
      sizeRef.current = next;
      setSize(next);
    }
  }, [hydrated?.ready, hydrated?.value, min, resolveDefault, upper]);

  // 落盘一次用户尺寸（后端 config；不写 localStorage）
  const persistSize = useCallback((value: number) => {
    persist?.save(value);
  }, [persist]);

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
      const next = clamp(startSize + delta, min, upper());
      // 一旦拖动即为「手动」：此后窗口 resize 不再覆盖用户选择
      manualRef.current = true;
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
      if (manualRef.current) persistSize(sizeRef.current);
    };

    target.addEventListener("pointermove", onMove as EventListener);
    target.addEventListener("pointerup", finish as EventListener);
    target.addEventListener("pointercancel", finish as EventListener);
  }, [axis, invert, min, persistSize, upper]);

  const reset = useCallback(() => {
    const next = resolveDefault();
    manualRef.current = false;
    setSize(next);
    sizeRef.current = next;
    // 清除持久值：回到「自动」语义，下次启动按比例默认
    persist?.save(null);
  }, [persist, resolveDefault]);

  return { size, startDrag, reset };
}
