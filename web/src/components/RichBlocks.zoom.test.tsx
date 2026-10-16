// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// SVG 放大灯箱（mermaid / diagram / layout）回归测试。
// 需求：架构图在气泡里偏小 → 点击浮到最上层放大、可拖动平移、点击返回。

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DiagramBlock } from "./RichBlocks";
import type { DiagramSpec } from "./RichBlocks";

const SPEC: DiagramSpec = {
  title: "部署架构",
  parts: [
    {
      name: "网关",
      desc: "鉴权与路由",
      shapes: [
        { t: "rect", x: 180, y: 20, w: 100, h: 40, rx: 8 },
        { t: "label", x: 230, y: 40, text: "网关" },
      ],
    },
  ],
};

function openZoom() {
  const btn = screen.getByRole("button", { name: /放大查看/ });
  fireEvent.click(btn);
}

describe("ZoomableSvg · 放大灯箱", () => {
  it("默认不渲染灯箱，仅显示放大按钮", () => {
    render(<DiagramBlock spec={SPEC} />);
    expect(document.querySelector(".md-zoom-overlay")).toBeNull();
    expect(screen.getByRole("button", { name: /放大查看/ })).toBeTruthy();
  });

  it("点击放大按钮 → 灯箱打开（挂在 body 上，脱离气泡祖先）", async () => {
    render(<DiagramBlock spec={SPEC} />);
    openZoom();
    await waitFor(() => {
      expect(document.querySelector(".md-zoom-overlay")).toBeTruthy();
    });
    // portal 到 body 的直属子节点（保证浮在所有 UI 之上，不受祖先 transform 影响）
    const overlay = document.querySelector(".md-zoom-overlay")!;
    expect(overlay.parentElement).toBe(document.body);
    expect(overlay.getAttribute("role")).toBe("dialog");
    // 图内容被复制进灯箱
    expect(overlay.querySelector("svg")).toBeTruthy();
  });

  it("灯箱内 id 加前缀（避免与列表副本冲突，mermaid 箭头 marker 串色）", async () => {
    render(<DiagramBlock spec={SPEC} />);
    openZoom();
    await waitFor(() => expect(document.querySelector(".md-zoom-overlay")).toBeTruthy());
    // 原图 svg 在列表里保留（未被搬走）
    const listSvg = document.querySelector(".md-diagram-svg-wrap svg");
    expect(listSvg).toBeTruthy();
  });

  it("点击遮罩关闭（点回对话）", async () => {
    render(<DiagramBlock spec={SPEC} />);
    openZoom();
    await waitFor(() => expect(document.querySelector(".md-zoom-overlay")).toBeTruthy());
    fireEvent.click(document.querySelector(".md-zoom-overlay")!);
    await waitFor(() => expect(document.querySelector(".md-zoom-overlay")).toBeNull());
  });

  it("Esc 关闭", async () => {
    render(<DiagramBlock spec={SPEC} />);
    openZoom();
    await waitFor(() => expect(document.querySelector(".md-zoom-overlay")).toBeTruthy());
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(document.querySelector(".md-zoom-overlay")).toBeNull());
  });

  it("✕ 按钮关闭", async () => {
    render(<DiagramBlock spec={SPEC} />);
    openZoom();
    await waitFor(() => expect(document.querySelector(".md-zoom-overlay")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /关闭/ }));
    await waitFor(() => expect(document.querySelector(".md-zoom-overlay")).toBeNull());
  });

  it("＋/－ 缩放改变百分比显示", async () => {
    render(<DiagramBlock spec={SPEC} />);
    openZoom();
    await waitFor(() => expect(document.querySelector(".md-zoom-overlay")).toBeTruthy());
    expect(screen.getByText("100%")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "放大" }));
    await waitFor(() => expect(screen.getByText("125%")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "缩小" }));
    await waitFor(() => expect(screen.getByText("100%")).toBeTruthy());
  });

  it("重置回 100% 并清平移", async () => {
    render(<DiagramBlock spec={SPEC} />);
    openZoom();
    await waitFor(() => expect(document.querySelector(".md-zoom-overlay")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "放大" }));
    await waitFor(() => expect(screen.getByText("125%")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "重置" }));
    await waitFor(() => expect(screen.getByText("100%")).toBeTruthy());
  });

  it("打开时锁背景滚动（body overflow hidden），关闭后恢复", async () => {
    render(<DiagramBlock spec={SPEC} />);
    const before = document.body.style.overflow;
    openZoom();
    await waitFor(() => expect(document.body.style.overflow).toBe("hidden"));
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(document.body.style.overflow).toBe(before));
  });
});
