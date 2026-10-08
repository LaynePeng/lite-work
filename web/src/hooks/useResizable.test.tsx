// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// useResizable：比例默认（随窗口实时重算）+ 后端持久化（persist/hydrated）。
// 回归背景：右栏默认曾是「挂载瞬间 innerWidth 的一次性快照」，窗口若在挂载时
// 尚未最大化就永久偏窄；且尺寸曾存 localStorage，而桌面端 Core 端口随机导致
// origin 每次都变 → 读不回、重开复原。现配置一律走后端 config，不再用 localStorage。

import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useResizable } from "./useResizable";

function setWindowWidth(w: number) {
  Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: w });
}

interface FakeTarget {
  listeners: Map<string, (ev: unknown) => void>;
  classList: { add: () => void; remove: () => void };
  setPointerCapture: () => void;
  releasePointerCapture: () => void;
  addEventListener: (type: string, fn: (ev: unknown) => void) => void;
  removeEventListener: (type: string) => void;
}

function makePointerTarget(): FakeTarget {
  const listeners = new Map<string, (ev: unknown) => void>();
  return {
    listeners,
    classList: { add: () => {}, remove: () => {} },
    setPointerCapture: () => {},
    releasePointerCapture: () => {},
    addEventListener: (type, fn) => { listeners.set(type, fn); },
    removeEventListener: (type) => { listeners.delete(type); },
  };
}

/** 模拟一次完整拖拽：down → move → up，返回挂上的 target（便于断言落盘） */
function drag(
  startDrag: (e: React.PointerEvent) => void,
  from: number,
  to: number,
) {
  const target = makePointerTarget();
  const startEvent = {
    button: 0,
    preventDefault: () => {},
    pointerId: 7,
    clientX: from,
    clientY: from,
    currentTarget: target,
  } as unknown as React.PointerEvent;
  act(() => { startDrag(startEvent); });
  act(() => { target.listeners.get("pointermove")?.({ clientX: to, clientY: to }); });
  act(() => { target.listeners.get("pointerup")?.({ pointerId: 7 }); });
  return target;
}

beforeEach(() => {
  setWindowWidth(2000);
});

describe("useResizable", () => {
  it("initial 传函数时按当前窗口宽度取比例", () => {
    setWindowWidth(2000);
    const { result } = renderHook(() => useResizable({
      axis: "col", min: 100, max: 5000,
      initial: () => Math.round(window.innerWidth * 0.28),
    }));
    expect(result.current.size).toBe(560);
  });

  it("自动模式下窗口 resize 实时重算（修复「一次性快照」）", () => {
    setWindowWidth(1280); // 挂载时窗口还没最大化
    const { result } = renderHook(() => useResizable({
      axis: "col", min: 100, max: 5000,
      initial: () => Math.round(window.innerWidth * 0.28),
    }));
    expect(result.current.size).toBe(358); // 1280 * 0.28
    act(() => {
      setWindowWidth(2560); // 窗口随后最大化
      window.dispatchEvent(new Event("resize"));
    });
    expect(result.current.size).toBe(717); // 2560 * 0.28 —— 自动跟随
  });

  it("拖动后进入手动模式：落盘 persist，且 resize 不再覆盖用户选择", () => {
    setWindowWidth(2560);
    const save = vi.fn();
    const { result } = renderHook(() => useResizable({
      axis: "col", min: 100, max: 5000, invert: true, initial: 300,
      persist: { load: () => null, save },
    }));
    drag(result.current.startDrag, 1000, 900); // invert：向左拖 100px = +100
    expect(result.current.size).toBe(400);
    expect(save).toHaveBeenCalledWith(400);
    act(() => {
      setWindowWidth(1200);
      window.dispatchEvent(new Event("resize"));
    });
    expect(result.current.size).toBe(400); // 手动值不被 resize 覆盖
  });

  it("双击 reset：回到比例默认并清除持久值（save(null)）", () => {
    setWindowWidth(2560);
    const save = vi.fn();
    const { result } = renderHook(() => useResizable({
      axis: "col", min: 100, max: 5000,
      initial: () => Math.round(window.innerWidth * 0.28),
      persist: { load: () => 600, save },
    }));
    expect(result.current.size).toBe(600); // 有持久值 → 手动
    act(() => { result.current.reset(); });
    expect(result.current.size).toBe(717); // 回默认
    expect(save).toHaveBeenCalledWith(null);
  });

  it("后端持久值异步就绪：有值则采用，无值则按已稳定的窗口重算默认", () => {
    setWindowWidth(1280);
    const { result, rerender } = renderHook(
      ({ ready, value }: { ready: boolean; value: number | null }) => useResizable({
        axis: "col", min: 100, max: 5000,
        initial: () => Math.round(window.innerWidth * 0.28),
        hydrated: { ready, value },
      }),
      { initialProps: { ready: false, value: null as number | null } },
    );
    expect(result.current.size).toBe(358);
    act(() => { setWindowWidth(2560); }); // 后端就绪前窗口已最大化
    rerender({ ready: true, value: null });
    expect(result.current.size).toBe(717); // 就绪时纠正默认
    rerender({ ready: true, value: 500 });
    expect(result.current.size).toBe(717); // 只应用一次，不再二次改写
  });

  it("hydrated 携带后端已存值：直接采用该像素值（仅一次）", () => {
    setWindowWidth(2560);
    const { result, rerender } = renderHook(
      ({ value }: { value: number | null }) => useResizable({
        axis: "col", min: 100, max: 5000, initial: () => 717,
        hydrated: { ready: true, value },
      }),
      { initialProps: { value: 500 as number | null } },
    );
    expect(result.current.size).toBe(500);
    rerender({ value: 650 });
    expect(result.current.size).toBe(500);
  });
});
