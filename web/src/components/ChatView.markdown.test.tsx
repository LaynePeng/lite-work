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

describe("Markdown 原生富内容块（chart / cards / 本地图片）", () => {
  it("```chart 围栏 → 原生 SVG 图表，围栏外文字照常渲染", () => {
    const { container } = render(
      <Markdown
        text={
          "趋势：\n\n```chart\n" +
          '{"type":"bar","title":"销量","labels":["一月","二月"],"series":[{"name":"销量","data":[3,5]}]}' +
          "\n```\n\n结论如上。"
        }
      />
    );
    expect(container.querySelector(".md-chart")).not.toBeNull();
    expect(container.querySelector(".md-chart svg rect")).not.toBeNull();
    expect(container.querySelector("iframe")).toBeNull();
    expect(screen.getByText("趋势：")).toBeInTheDocument();
    expect(screen.getByText("结论如上。")).toBeInTheDocument();
  });

  it("chart 单系列简写（data 字段）与 pie 类型可用", () => {
    const { container } = render(
      <Markdown text={'```chart\n{"type":"pie","labels":["A","B"],"data":[3,1]}\n```'} />
    );
    expect(container.querySelector(".md-chart svg path")).not.toBeNull();
  });

  it("chart JSON 无效 → 回退为普通代码块", () => {
    const { container } = render(<Markdown text={"```chart\nnot-json\n```"} />);
    expect(container.querySelector(".md-chart")).toBeNull();
    expect(container.querySelector("code")).not.toBeNull();
  });

  it("```cards 围栏 → 卡片网格，本地图片 rewrite 到 /api/files/raw", () => {
    const { container } = render(
      <Markdown
        text={
          '```cards\n{"title":"对比","items":[{"title":"A方案","image":"outputs/a.png","price":"¥299","tags":["快"]}]}\n```'
        }
      />
    );
    expect(container.querySelectorAll(".md-card").length).toBe(1);
    const img = container.querySelector(".md-card img") as HTMLImageElement;
    expect(img.getAttribute("src")).toBe("/api/files/raw?path=outputs%2Fa.png");
  });

  it("cards 里 https 图片保持原样", () => {
    const { container } = render(
      <Markdown text={'```cards\n{"items":[{"title":"X","image":"https://cdn.x.com/a.png"}]}\n```'} />
    );
    const img = container.querySelector(".md-card img") as HTMLImageElement;
    expect(img.getAttribute("src")).toBe("https://cdn.x.com/a.png");
  });

  it("```html 围栏内 <img> 相对路径 rewrite 到 /api/files/raw（修复断链）", () => {
    const { container } = render(
      <Markdown text={'```html\n<div><img src="outputs/pic.png"></div>\n```'} />
    );
    const frame = container.querySelector("iframe.md-html-iframe");
    expect(frame).not.toBeNull();
    expect(frame!.getAttribute("srcdoc")).toContain("/api/files/raw?path=outputs%2Fpic.png");
  });

  it("html 围栏内单引号 src 与 CSS url() 也 rewrite；https 图保持原样", () => {
    const { container } = render(
      <Markdown
        text={
          "```html\n" +
          '<div style="background:url(bg.png)"><img src=\'a.jpg\'><img src="https://x.com/b.png"></div>\n' +
          "```"
        }
      />
    );
    const srcdoc = container.querySelector("iframe.md-html-iframe")!.getAttribute("srcdoc") ?? "";
    expect(srcdoc).toContain("/api/files/raw?path=a.jpg");
    expect(srcdoc).toContain("/api/files/raw?path=bg.png");
    expect(srcdoc).toContain("https://x.com/b.png");
  });

  it("chart 与 cards 与文字混合分段渲染", () => {
    const { container } = render(
      <Markdown
        text={
          "看数据：\n\n```chart\n" +
          '{"type":"line","labels":["1","2"],"data":[1,2]}' +
          "\n```\n\n再看卡片：\n\n```cards\n{\"items\":[{\"title\":\"X\"}]}\n```"
        }
      />
    );
    expect(container.querySelector(".md-chart")).not.toBeNull();
    expect(container.querySelector(".md-card")).not.toBeNull();
    expect(screen.getByText("看数据：")).toBeInTheDocument();
    expect(screen.getByText("再看卡片：")).toBeInTheDocument();
  });
});
