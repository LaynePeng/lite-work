// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 输入卡顿的回归防线。
//
// 症状：打字时明显卡顿。成因是两条渲染路径叠加——
// 1. App 的流式刷新每 80ms 调一次 patchChat，产生新 chatStates → 整个 App
//    树重渲染（含 Composer）。Composer 的 text 状态在自己内部，本不该跟着
//    父树一起重渲染。
// 2. Composer 之前没包 memo，且父组件传的回调是内联箭头（每次渲染换新引用），
//    所以连 memo 也会失效。
//
// 这里锁住：① Composer 必须是 memo 包装（父树重渲染时能跳过）；
// ② 放大态测高必须走 rAF（读 scrollHeight 会强制同步 reflow，
//    放在按键路径上就是卡顿源）。

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const composerSrc = readFileSync(
  resolve(process.cwd(), "src/components/Composer.tsx"), "utf-8");
const appSrc = readFileSync(
  resolve(process.cwd(), "src/App.tsx"), "utf-8");

describe("输入性能 · 渲染路径", () => {
  it("Composer 必须用 memo 包装（父树流式刷新时跳过重渲染）", () => {
    expect(composerSrc).toMatch(/export default memo\(Composer\)/);
    // memo 必须真的从 react 导入，否则是未定义标识符
    expect(composerSrc).toMatch(/import \{[^}]*\bmemo\b[^}]*\} from "react"/);
  });

  it("放大态测高走 rAF，不在按键路径上同步 reflow", () => {
    const i = composerSrc.indexOf("const cap = Math.round(window.innerHeight * 0.7)");
    expect(i, "找不到测高逻辑").toBeGreaterThan(-1);
    // 测高代码周围必须有 requestAnimationFrame
    const around = composerSrc.slice(Math.max(0, i - 900), i + 200);
    expect(around).toContain("requestAnimationFrame");
  });

  it("App 传给 Composer 的推理强度回调必须是稳定引用（useCallback）", () => {
    // 内联箭头会让 memo 失效——必须收口成 useCallback 后按名传
    expect(appSrc).toMatch(/onReasoningEffortChange=\{handleReasoningEffortChange\}/);
    expect(appSrc).toMatch(/const handleReasoningEffortChange = useCallback\(/);
  });

  it("App 传给 Composer 的模型切换回调必须是稳定引用", () => {
    expect(appSrc).toMatch(/onSessionModelChange=\{handleSessionModelChange\}/);
    expect(appSrc).toMatch(/const handleSessionModelChange = useCallback\(/);
  });
});
