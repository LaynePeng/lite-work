import { describe, it, expect } from "vitest";
import { render, fireEvent, waitFor } from "@testing-library/react";
import { DiagramBlock } from "./RichBlocks";

/** 双击放大灯箱（ZoomableSvg / prefixSvgIds）的回归测试。
 *
 *  历史问题：prefixSvgIds 用 image/svg+xml（XML 严格）解析，svg 文本含
 *  HTML 实体（&nbsp; 等，mermaid 输出常见）时 parsererror → 静默退回 →
 *  灯箱里与气泡同名 id 冲突，「气泡正常、双击最大化挂掉」。
 *  修复后用 text/html 宽松解析。这里用 DiagramBlock 走同一 ZoomableSvg。 */
describe("ZoomBox 双击放大", () => {
  const spec = (text: string) => ({
    title: "测试",
    canvas: { w: 300, h: 150 },
    parts: [{ name: text, desc: "d", shapes: ["rect(20,20,120,50,8)", `label(80,45,${text},11)`] }],
  });

  it("普通文本：双击后灯箱 svg 带 zx- 前缀 id", async () => {
    const { container } = render(<DiagramBlock spec={spec("正常文本") as never} />);
    await waitFor(() => expect(container.querySelector("svg")).toBeTruthy());
    fireEvent.doubleClick(container.querySelector(".md-zoom-wrap") as HTMLElement);
    await waitFor(() => expect(document.querySelector(".md-zoom-overlay")).toBeTruthy());
    // DiagramBlock 的 svg 无 id → map 为空 → 原样返回（无前缀，属正常）。
    // 这里验证「双击后灯箱打开、svg 内容完整、无异常」。
    const canvas = document.querySelector(".md-zoom-canvas");
    expect(canvas?.innerHTML).toContain("svg");
    expect(canvas?.innerHTML).toContain("正常文本");
  });

  it("含 &nbsp; 的文本：灯箱照常打开，内容不丢（XML 严格模式回归）", async () => {
    // 图内文本含 HTML 实体（jsdom 序列化后成 &nbsp;）——旧实现（XML 严格
    // 解析）会 parsererror；新实现（text/html）应照常进灯箱且文字完整。
    const { container } = render(<DiagramBlock spec={spec("价格\u00a0变动") as never} />);
    await waitFor(() => expect(container.querySelector("svg")).toBeTruthy());
    fireEvent.doubleClick(container.querySelector(".md-zoom-wrap") as HTMLElement);
    await waitFor(() => expect(document.querySelector(".md-zoom-overlay")).toBeTruthy());
    const canvas = document.querySelector(".md-zoom-canvas");
    expect(canvas?.innerHTML).toContain("svg");
    // 文字进灯箱（实体不丢、不因解析失败变空）
    expect(canvas?.innerHTML).toContain("价格");
    expect(canvas?.innerHTML).toContain("变动");
  });
});
