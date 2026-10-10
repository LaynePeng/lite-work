// 内联 HTML 回复：```html 完整文档 → 沙箱 iframe；短片段/普通代码块保持既有行为。
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Markdown } from "./ChatView";

describe("Markdown 内联 HTML 渲染（展示型回复）", () => {
  it("完整 html 文档围栏 → 渲染为沙箱 iframe，并保留「查看源码」入口", () => {
    const { container } = render(
      <Markdown text={
        "```html\n" +
        "<!DOCTYPE html><html><body><h1>Hello 预览</h1></body></html>\n" +
        "```"
      } />
    );
    const frame = container.querySelector("iframe.md-html-iframe");
    expect(frame).not.toBeNull();
    expect(frame!.getAttribute("srcdoc")).toContain("<h1>Hello 预览</h1>");
    expect(frame!.getAttribute("sandbox")).toBe("");
    expect(screen.getByRole("button", { name: "查看源码" })).toBeInTheDocument();
  });

  it("html 围栏前/后的说明文字仍然以 Markdown 渲染", () => {
    render(
      <Markdown text={
        "这是预览效果：\n\n```html\n" +
        "<!DOCTYPE html><html><body><p>卡片</p></body></html>\n" +
        "```\n\n如需说明找我要。"
      } />
    );
    expect(document.querySelector("iframe.md-html-iframe")).not.toBeNull();
    expect(screen.getByText(/这是预览效果/)).toBeInTheDocument();
    expect(screen.getByText(/如需说明找我要/)).toBeInTheDocument();
  });

  it("html 围栏内的自足片段（无 DOCTYPE/body）也渲染为 iframe", () => {
    const { container } = render(
      <Markdown text={"```html\n<div style=\"padding:8px\"><b>hi</b></div>\n```"} />
    );
    const frame = container.querySelector("iframe.md-html-iframe");
    expect(frame).not.toBeNull();
    expect(frame!.getAttribute("srcdoc")).toContain("<b>hi</b>");
  });

  it("围栏内没有任何 HTML 标签（纯文本）→ 保留为代码块", () => {
    const { container } = render(<Markdown text={"```html\njust some text\n```"} />);
    expect(container.querySelector("iframe.md-html-iframe")).toBeNull();
    expect(container.querySelector("code")).not.toBeNull();
  });

  it("无围栏：整条消息是完整 HTML 文档 → 整条渲染为 iframe", () => {
    const { container } = render(
      <Markdown
        text={"<!DOCTYPE html><html><body><h1>直接回复</h1></body></html>"}
      />
    );
    const frame = container.querySelector("iframe.md-html-iframe");
    expect(frame).not.toBeNull();
    expect(frame!.getAttribute("srcdoc")).toContain("<h1>直接回复</h1>");
  });

  it("无围栏：以 <html 开头但没有 </html> 结尾（截断/混合文本）→ 不当 HTML 渲染", () => {
    const { container } = render(
      <Markdown text={"<html>这是普通文本里提到 html 标签，没有闭合"} />
    );
    expect(container.querySelector("iframe.md-html-iframe")).toBeNull();
  });

  it("普通代码块（非 html）不受影响", () => {
    const { container } = render(<Markdown text={"```js\nconst a = 1;\n```"} />);
    expect(container.querySelector("iframe")).toBeNull();
    expect(container.querySelector("code")).not.toBeNull();
  });

  it("无围栏的纯文本不渲染 iframe", () => {
    const { container } = render(<Markdown text="普通文字回复" />);
    expect(container.querySelector("iframe")).toBeNull();
  });
});
