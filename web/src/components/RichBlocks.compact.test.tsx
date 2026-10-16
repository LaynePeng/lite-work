// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 富块渲染层响应式（compact 主题）回归测试。
// 设计稿：docs/rich-blocks-compact-design.md
// 验收线：desktop 参数集与改造前逐值一致（P3 原则）；compact 下 W=300、
// 网格 3 线、标签截断 5、隔标签、字号走 FONTS。

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ChartBlock, CompactProvider } from "./RichBlocks";
import type { ChartSpec } from "./RichBlocks";

const BAR_SPEC: ChartSpec = {
  type: "bar",
  title: "各方案月成本",
  unit: "元",
  labels: ["A方案", "B方案", "C方案"],
  series: [{ name: "成本", data: [120, 90, 150] }],
};

function svgsOf(container: HTMLElement): SVGSVGElement[] {
  return Array.from(container.querySelectorAll("svg"));
}

describe("ChartBlock · desktop（防回归：与改造前逐值一致）", () => {
  it("柱状图：viewBox 640 宽、网格 5 线（0..4）", () => {
    const { container } = render(<ChartBlock spec={BAR_SPEC} />);
    const svg = svgsOf(container)[0];
    expect(svg.getAttribute("viewBox")).toBe("0 0 640 290");
    // 5 条 y 网格线（4 段）
    const gridLines = svg.querySelectorAll("line");
    expect(gridLines.length).toBe(5);
  });

  it("饼图：viewBox 260×260", () => {
    const { container } = render(
      <ChartBlock spec={{ ...BAR_SPEC, type: "pie" }} />,
    );
    const svg = svgsOf(container)[0];
    expect(svg.getAttribute("viewBox")).toBe("0 0 260 260");
  });

  it("横向柱状图：viewBox 640 宽、padL 128", () => {
    const { container } = render(
      <ChartBlock spec={{ ...BAR_SPEC, type: "hbar" }} />,
    );
    const svg = svgsOf(container)[0];
    const vb = svg.getAttribute("viewBox") ?? "";
    expect(vb.startsWith("0 0 640 ")).toBe(true);
    // 类目标签 x = padL - 8 = 120
    const label = svg.querySelector("text");
    expect(label?.getAttribute("x")).toBe("120");
  });

  it("类目数 ≤10 单系列：显示数值标注", () => {
    const { container } = render(<ChartBlock spec={BAR_SPEC} />);
    const texts = Array.from(svgsOf(container)[0].querySelectorAll("text")).map(
      (t) => t.textContent,
    );
    expect(texts).toContain("120");
    expect(texts).toContain("150");
  });
});

describe("ChartBlock · compact（CompactProvider 强制）", () => {
  const wrap = (spec: ChartSpec) =>
    render(
      <CompactProvider value={true}>
        <ChartBlock spec={spec} />
      </CompactProvider>,
    );

  it("柱状图：viewBox 300 宽、网格 4 线（3 段）", () => {
    const { container } = wrap(BAR_SPEC);
    const svg = svgsOf(container)[0];
    expect(svg.getAttribute("viewBox")).toBe("0 0 300 218");
    const gridLines = svg.querySelectorAll("line");
    expect(gridLines.length).toBe(4);
  });

  it("柱状图：轴刻度字号 10.5（FONTS.m.axis）", () => {
    const { container } = wrap(BAR_SPEC);
    const svg = svgsOf(container)[0];
    // y 轴刻度是 svg 内第一批 text（类目标签在底部）
    const axisText = Array.from(svg.querySelectorAll("text"))[0];
    expect(axisText.getAttribute("font-size")).toBe("10.5");
  });

  it("类目超过 6 个时隔一个显示一个", () => {
    const spec: ChartSpec = {
      type: "bar",
      labels: ["1月", "2月", "3月", "4月", "5月", "6月", "7月", "8月"],
      series: [{ name: "销量", data: [5, 8, 6, 9, 7, 10, 8, 11] }],
    };
    const { container } = wrap(spec);
    const svg = svgsOf(container)[0];
    // 底部类目标签：compact 下隔标签 → 8 个类目只出 4 个
    const catLabels = Array.from(svg.querySelectorAll("text"))
      .filter((t) => t.getAttribute("y") === "206") // y0+16，compact y0=184+... 由 viewBox 推
      .map((t) => t.textContent);
    // 不做精确 y 断言（易碎），改为计数：类目文本出现次数 < 8
    const all = Array.from(svg.querySelectorAll("text")).map((t) => t.textContent);
    const catCount = ["1月", "2月", "3月", "4月", "5月", "6月", "7月", "8月"]
      .filter((m) => all.includes(m)).length;
    expect(catCount).toBe(4);
    expect(catLabels.length).toBeGreaterThanOrEqual(0);
  });

  it("数值标注阈值收紧：compact 下 7 类目不标数值", () => {
    const spec: ChartSpec = {
      type: "bar",
      labels: ["a", "b", "c", "d", "e", "f", "g"],
      series: [{ name: "v", data: [1, 2, 3, 4, 5, 6, 7] }],
    };
    const { container } = wrap(spec);
    const all = Array.from(svgsOf(container)[0].querySelectorAll("text")).map(
      (t) => t.textContent,
    );
    expect(all).not.toContain("7");
  });

  it("饼图：viewBox 220×220", () => {
    const { container } = wrap({ ...BAR_SPEC, type: "pie" });
    const svg = svgsOf(container)[0];
    expect(svg.getAttribute("viewBox")).toBe("0 0 220 220");
  });

  it("折线图：数据点半径 3.4（触控命中）", () => {
    const { container } = wrap({ ...BAR_SPEC, type: "line" });
    const dots = svgsOf(container)[0].querySelectorAll("circle");
    expect(dots.length).toBeGreaterThan(0);
    expect(dots[0].getAttribute("r")).toBe("3.4");
  });

  it("横向柱状图：viewBox 300 宽、padL 80（标签 x=72）", () => {
    const { container } = wrap({ ...BAR_SPEC, type: "hbar" });
    const svg = svgsOf(container)[0];
    expect(svg.getAttribute("viewBox")?.startsWith("0 0 300 ")).toBe(true);
    const label = svg.querySelector("text");
    expect(label?.getAttribute("x")).toBe("72");
  });

  it("长类目截断到 5 字（compact LABEL_TRUNC）", () => {
    const spec: ChartSpec = {
      type: "bar",
      labels: ["这是一个很长的类目标签", "B", "C"],
      series: [{ name: "v", data: [1, 2, 3] }],
    };
    const { container } = wrap(spec);
    const all = Array.from(svgsOf(container)[0].querySelectorAll("text")).map(
      (t) => t.textContent,
    );
    // trunc(s, 5) = 前 4 字 + "…"
    expect(all).toContain("这是一个…");
  });
});

describe("ChartBlock · 双轴组合图", () => {
  const COMBO_SPEC: ChartSpec = {
    type: "combo",
    labels: ["1月", "2月", "3月", "4月"],
    series: [
      { name: "销量", data: [100, 120, 90, 140] },
      { name: "增长率", data: [0.1, 0.2, -0.05, 0.3], axis: "right" },
    ],
  };

  it("desktop：viewBox 640 宽", () => {
    const { container } = render(<ChartBlock spec={COMBO_SPEC} />);
    expect(svgsOf(container)[0].getAttribute("viewBox")?.startsWith("0 0 640 ")).toBe(true);
  });

  it("compact：viewBox 300 宽", () => {
    const { container } = render(
      <CompactProvider value={true}>
        <ChartBlock spec={COMBO_SPEC} />
      </CompactProvider>,
    );
    expect(svgsOf(container)[0].getAttribute("viewBox")?.startsWith("0 0 300 ")).toBe(true);
  });
});

describe("ChartBlock · P2 图表（六类）compact", () => {
  const wrap = (spec: ChartSpec) =>
    render(
      <CompactProvider value={true}>
        <ChartBlock spec={spec} />
      </CompactProvider>,
    );

  it("散点图：viewBox 300 宽、点半径 4.2", () => {
    const spec: ChartSpec = {
      type: "scatter",
      labels: [],
      series: [{ name: "A", data: [] }],
      points: [[{ x: 1, y: 2 }, { x: 3, y: 4 }, { x: 5, y: 1 }]],
    };
    const { container } = wrap(spec);
    const svg = svgsOf(container)[0];
    expect(svg.getAttribute("viewBox")?.startsWith("0 0 300 ")).toBe(true);
    const dot = svg.querySelector("circle");
    expect(dot?.getAttribute("r")).toBe("4.2");
  });

  it("箱线图：viewBox 300 宽", () => {
    const spec: ChartSpec = {
      type: "boxplot",
      labels: ["A", "B"],
      series: [{ name: "s", data: [] }],
      boxStats: [[{ label: "A", min: 1, q1: 2, median: 3, q3: 4, max: 5 }]],
    };
    const { container } = wrap(spec);
    expect(svgsOf(container)[0].getAttribute("viewBox")?.startsWith("0 0 300 ")).toBe(true);
  });

  it("直方图：viewBox 300 宽", () => {
    const spec: ChartSpec = {
      type: "histogram",
      labels: [],
      series: [{ name: "v", data: [1, 1.2, 2, 2.2, 3, 3.5, 4, 4.8, 5] }],
    };
    const { container } = wrap(spec);
    expect(svgsOf(container)[0].getAttribute("viewBox")?.startsWith("0 0 300 ")).toBe(true);
  });

  it("瀑布图：viewBox 300 宽", () => {
    const spec: ChartSpec = {
      type: "waterfall",
      labels: [],
      series: [{ name: "v", data: [] }],
      items: [
        { label: "期初", value: 100, total: true },
        { label: "新增", value: 30 },
        { label: "流失", value: -18 },
        { label: "期末", value: 112, total: true },
      ],
    };
    const { container } = wrap(spec);
    expect(svgsOf(container)[0].getAttribute("viewBox")?.startsWith("0 0 300 ")).toBe(true);
  });

  it("堆叠柱：viewBox 300 宽", () => {
    const spec: ChartSpec = {
      type: "stacked",
      labels: ["Q1", "Q2"],
      series: [
        { name: "a", data: [3, 4] },
        { name: "b", data: [1, 2] },
      ],
    };
    const { container } = wrap(spec);
    expect(svgsOf(container)[0].getAttribute("viewBox")?.startsWith("0 0 300 ")).toBe(true);
  });

  it("雷达图：viewBox 270×270、顶点半径 3.2", () => {
    const spec: ChartSpec = {
      type: "radar",
      labels: ["速度", "质量", "成本", "交付"],
      series: [{ name: "v", data: [3, 4, 2, 5] }],
    };
    const { container } = wrap(spec);
    const svg = svgsOf(container)[0];
    expect(svg.getAttribute("viewBox")).toBe("0 0 270 270");
    const vertex = svg.querySelector("circle");
    expect(vertex?.getAttribute("r")).toBe("3.2");
  });

  it("desktop 雷达图防回归：viewBox 300×300、顶点 2.6", () => {
    const spec: ChartSpec = {
      type: "radar",
      labels: ["速度", "质量", "成本", "交付"],
      series: [{ name: "v", data: [3, 4, 2, 5] }],
    };
    const { container } = render(<ChartBlock spec={spec} />);
    const svg = svgsOf(container)[0];
    expect(svg.getAttribute("viewBox")).toBe("0 0 300 300");
    expect(svg.querySelector("circle")?.getAttribute("r")).toBe("2.6");
  });

  it("desktop 散点图防回归：点半径 3", () => {
    const spec: ChartSpec = {
      type: "scatter",
      labels: [],
      series: [{ name: "A", data: [] }],
      points: [[{ x: 1, y: 2 }, { x: 3, y: 4 }]],
    };
    const { container } = render(<ChartBlock spec={spec} />);
    expect(svgsOf(container)[0].querySelector("circle")?.getAttribute("r")).toBe("3");
  });
});
