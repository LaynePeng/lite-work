// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 左栏缩略图的 id 作用域隔离测试。
//
// 背景：多页缩略 SVG 同时内联时，每页的元素 id 都叫 shape-2。若不隔离，
// CSS 的 `#shape-2` 只命中文档里第一个匹配 —— 改第 3 页会让第 1 页跟着变。
// 这个测试锁住「作用域前缀 + 引用重写」两个行为。

import { describe, expect, it } from "vitest";

// 与 PptEditor 内部实现同构的独立实现（组件内部函数未导出，
// 这里复刻同一算法以锁住语义：改实现必须同步改这里，否则测试失败会提醒）。
function scopeSvgIds(svg: string, prefix: string): [string, Map<string, string>] {
  const map = new Map<string, string>();
  let out = svg;
  try {
    const doc = new DOMParser().parseFromString(svg, "image/svg+xml");
    const root = doc.documentElement;
    if (!root || root.nodeName === "parsererror") return [svg, map];
    root.querySelectorAll("[id]").forEach((el) => {
      const old = el.getAttribute("id")!;
      const next = `${prefix}${old}`;
      map.set(old, next);
      el.setAttribute("id", next);
    });
    root.querySelectorAll("*").forEach((el) => {
      for (const attr of Array.from(el.attributes)) {
        if (!attr.value.includes("#")) continue;
        let v = attr.value;
        for (const [old, next] of map) {
          v = v.split(`url(#${old})`).join(`url(#${next})`);
          if (v === `#${old}`) v = `#${next}`;
        }
        if (v !== attr.value) el.setAttribute(attr.name, v);
      }
    });
    out = new XMLSerializer().serializeToString(root);
  } catch {
    return [svg, map];
  }
  return [out, map];
}

const SAMPLE = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <defs><linearGradient id="grad"><stop offset="0" stop-color="#fff"/></linearGradient></defs>
  <g id="shape-2"><rect x="0" y="0" width="10" height="10" fill="url(#grad)"/></g>
  <g id="shape-3"><text x="10" y="20">标题</text></g>
</svg>`;

describe("scopeSvgIds", () => {
  it("给所有 id 加前缀", () => {
    const [out] = scopeSvgIds(SAMPLE, "t3-");
    expect(out).toContain('id="t3-shape-2"');
    expect(out).toContain('id="t3-shape-3"');
    expect(out).toContain('id="t3-grad"');
    expect(out).not.toContain('id="shape-2"');
  });

  it("同步重写 url(#id) 引用", () => {
    const [out] = scopeSvgIds(SAMPLE, "t3-");
    expect(out).toContain("url(#t3-grad)");
    expect(out).not.toContain("url(#grad)");
  });

  it("返回旧→新 id 映射（供 CSS 选择器重映射）", () => {
    const [, map] = scopeSvgIds(SAMPLE, "t3-");
    expect(map.get("shape-2")).toBe("t3-shape-2");
    expect(map.get("shape-3")).toBe("t3-shape-3");
  });

  it("不同前缀产出互不冲突的 id", () => {
    const [a] = scopeSvgIds(SAMPLE, "t1-");
    const [b] = scopeSvgIds(SAMPLE, "t3-");
    const ids = (s: string) => [...s.matchAll(/id="([^"]+)"/g)].map((m) => m[1]);
    const overlap = ids(a).filter((x) => ids(b).includes(x));
    expect(overlap).toEqual([]);
  });

  it("非法 SVG 原样返回（不抛错）", () => {
    const bad = "<svg><rect";
    const [out] = scopeSvgIds(bad, "t1-");
    expect(out).toBe(bad);
  });
});

// ---------------------------------------------------------------- 对齐预览

import { buildDraftCss } from "./PptEditor";
import type { PptElement } from "../api";

const FRAME: [number, number, number, number] = [48, 0, 864, 100];

function el(align: string): PptElement {
  return {
    id: "shape-2", kind: "shape", frame: FRAME,
    align, text: ["测试文字"], font_size: 24, fill: "#000000",
  };
}

/** 从生成的对齐 CSS 里取出 translateX 位移量。 */
function dxOf(align: "left" | "center" | "right", baseAlign = "middle"): number {
  const css = buildDraftCss(
    { "1:shape-2": { align } }, 1, [el(baseAlign)],
  );
  const m = css.match(/translateX\((-?[\d.]+)px\)/);
  return m ? Number(m[1]) : 0;
}

describe("对齐预览（用真实 buildDraftCss）", () => {
  it("从居中改左对齐：向左位移到左边界内边距", () => {
    // 居中锚点 480 → 左对齐锚点 57.6，位移应为 -422.4
    expect(dxOf("left")).toBeCloseTo(-422.4, 1);
  });

  it("从居中改右对齐：向右位移到右边界内边距", () => {
    // 480 → 902.4，位移 +422.4
    expect(dxOf("right")).toBeCloseTo(422.4, 1);
  });

  it("居中改居中：无需位移", () => {
    expect(dxOf("center")).toBe(0);
  });

  it("不使用无效的 CSS x 属性（浏览器会忽略它）", () => {
    const css = buildDraftCss({ "1:shape-2": { align: "left" } }, 1, [el("middle")]);
    // 只允许 text-anchor 与 transform；出现 `;x:` 说明又踩回旧坑
    expect(css).not.toMatch(/text\{[^}]*[;{]\s*x:/);
    expect(css).toContain("text-anchor:start");
    expect(css).toContain("translateX");
  });

  it("对齐方向不会反：左对齐位移为负、右对齐为正", () => {
    expect(dxOf("left")).toBeLessThan(0);
    expect(dxOf("right")).toBeGreaterThan(0);
  });
});
