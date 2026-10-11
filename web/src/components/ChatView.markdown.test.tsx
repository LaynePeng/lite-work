// 内联 HTML 回复：```html 完整文档 → 沙箱 iframe；短片段/普通代码块保持既有行为。
import { fireEvent, render, screen } from "@testing-library/react";
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

// ---------------------------------------------------------------- 第二批富块形态

describe("Markdown 富内容块（kpi / timeline / table / compare / board）", () => {
  it("```kpi 围栏 → 指标卡（数值 + 涨跌色），文字照常渲染", () => {
    const { container } = render(
      <Markdown text={
        "本月经营：\n\n```kpi\n" +
        '{"title":"核心指标","items":[{"label":"营收","value":"¥1.24M","delta":"12.4%","trend":"up"},' +
        '{"label":"流失率","value":"4.7%","delta":"0.8%","trend":"down","note":"环比"}]}\n```'
      } />
    );
    expect(container.querySelectorAll(".md-kpi").length).toBe(2);
    expect(container.querySelector(".md-kpi-value")?.textContent).toBe("¥1.24M");
    expect(container.querySelector(".md-kpi-delta.up")?.textContent).toContain("12.4%");
    expect(container.querySelector(".md-kpi-delta.down")).not.toBeNull();
    expect(container.querySelector(".md-kpi-note")?.textContent).toBe("环比");
    expect(screen.getByText("本月经营：")).toBeInTheDocument();
  });

  it("kpi 缺 label/value 的条目被丢弃；全空 → 回退代码块", () => {
    const { container: c1 } = render(
      <Markdown text={'```kpi\n{"items":[{"label":"只有标签"},{"label":"A","value":"1"}]}\n```'} />
    );
    expect(c1.querySelectorAll(".md-kpi").length).toBe(1);

    const { container: c2 } = render(<Markdown text={'```kpi\n{"items":[{"label":"x"}]}\n```'} />);
    expect(c2.querySelector(".md-kpi")).toBeNull();
    expect(c2.querySelector("code")).not.toBeNull();
  });

  it("```timeline 围栏 → 时间线（时间、标题、说明、状态色）", () => {
    const { container } = render(
      <Markdown text={
        '```timeline\n{"items":[{"time":"09-22","title":"v1.10.7 发布","desc":"修复思考流","status":"done"},' +
        '{"title":"插件优先级","status":"active"},{"title":"待排期"}]}\n```'
      } />
    );
    expect(container.querySelectorAll(".md-tl-item").length).toBe(3);
    expect(container.querySelector(".md-tl-dot.done")).not.toBeNull();
    expect(container.querySelector(".md-tl-dot.active")).not.toBeNull();
    expect(container.querySelector(".md-tl-time")?.textContent).toBe("09-22");
    expect(container.querySelector(".md-tl-desc")?.textContent).toBe("修复思考流");
    // 默认状态 pending（未声明 status）
    expect(container.querySelectorAll(".md-tl-dot.pending").length).toBe(1);
  });

  it("```table 围栏 → 数据表；数值列右对齐（自动判定）", () => {
    const { container } = render(
      <Markdown text={
        '```table\n{"title":"方案对比","columns":["方案","月成本","风险"],' +
        '"rows":[["A 自建","¥12,400","中"],["B 托管","¥8,900","低"]]}\n```'
      } />
    );
    const ths = container.querySelectorAll(".md-table th");
    expect(ths.length).toBe(3);
    expect(ths[1].className).toContain("num");     // 月成本列自动判数值
    expect(ths[0].className).not.toContain("num"); // 方案列不是
    expect(container.querySelectorAll(".md-table tbody tr").length).toBe(2);
  });

  it("table 显式 numeric 覆盖自动判定；空 columns/rows → 回退代码块", () => {
    const { container } = render(
      <Markdown text={
        '```table\n{"columns":["A","B"],"rows":[["x","1"]],"numeric":[0]}\n```'
      } />
    );
    expect(container.querySelectorAll(".md-table th")[0].className).toContain("num");

    const { container: c2 } = render(<Markdown text={'```table\n{"columns":[],"rows":[]}\n```'} />);
    expect(c2.querySelector(".md-table")).toBeNull();
  });

  it("```compare 围栏 → 左右对比（两侧标题与条目、tone 描边）", () => {
    const { container } = render(
      <Markdown text={
        '```compare\n{"left":{"title":"现状","items":["旧版压新版","需手动更新"],"tone":"bad"},' +
        '"right":{"title":"改后","items":["版本感知","装完即用"],"tone":"good"}}\n```'
      } />
    );
    expect(container.querySelectorAll(".md-cmp-side").length).toBe(2);
    expect(container.querySelector(".md-cmp-side.bad .md-cmp-head")?.textContent).toBe("现状");
    expect(container.querySelector(".md-cmp-side.good .md-cmp-head")?.textContent).toBe("改后");
    expect(container.querySelectorAll(".md-cmp-list li").length).toBe(4);
    expect(container.querySelector(".md-cmp-vs")?.textContent).toBe("⇄");
  });

  it("compare 缺一侧 → 回退代码块", () => {
    const { container } = render(
      <Markdown text={'```compare\n{"left":{"title":"只有左","items":["a"]}}\n```'} />
    );
    expect(container.querySelector(".md-cmp")).toBeNull();
    expect(container.querySelector("code")).not.toBeNull();
  });

  it("```board 围栏 → 看板（分组列 + 卡片 + 计数 + 空列提示）", () => {
    const { container } = render(
      <Markdown text={
        '```board\n{"columns":[{"name":"待办","cards":[{"title":"写用例","tag":"P1"}]},' +
        '{"name":"完成","cards":[{"title":"树杀","tone":"good"},{"title":"OCR","tone":"good"}]},' +
        '{"name":"阻塞","cards":[]}]}\n```'
      } />
    );
    expect(container.querySelectorAll(".md-board-col").length).toBe(3);
    expect(container.querySelector(".md-board-count")?.textContent).toBe("1");
    expect(container.querySelector(".md-board-card.good")).not.toBeNull();
    expect(container.querySelector(".md-board-tag")?.textContent).toBe("P1");
    expect(container.querySelector(".md-board-empty")).not.toBeNull();
  });

  it("```progress 围栏 → 进度条（色阶 + 自定义 display + note）", () => {
    const { container } = render(
      <Markdown text={
        '```progress\n{"title":"质量目标","items":[{"label":"代码覆盖率","value":82},' +
        '{"label":"接口文档","value":54,"note":"目标 90%"},{"label":"告警治理","value":21,"display":"3/14"}]}\n```'
      } />
    );
    expect(container.querySelectorAll(".md-progress").length).toBe(3);
    // 色阶：≥80 绿 / 40-79 黄 / <40 红
    expect(container.querySelector(".md-progress-fill.good")).not.toBeNull();
    expect(container.querySelector(".md-progress-fill.warn")).not.toBeNull();
    expect(container.querySelector(".md-progress-fill.bad")).not.toBeNull();
    // 宽度按百分比写入
    expect((container.querySelector(".md-progress-fill.good") as HTMLElement).style.width).toBe("82%");
    // 缺省显示百分比；显式 display 优先
    expect(container.querySelector(".md-progress-value.good")?.textContent).toBe("82%");
    expect(container.querySelector(".md-progress-value.bad")?.textContent).toBe("3/14");
    expect(container.querySelector(".md-progress-note")?.textContent).toBe("目标 90%");
  });

  it("progress 越界值 clamp 到 0-100；无 items → 回退代码块", () => {
    const { container } = render(
      <Markdown text={'```progress\n{"items":[{"label":"超范围","value":180},{"label":"负值","value":-20}]}\n```'} />
    );
    const fills = container.querySelectorAll(".md-progress-fill") as NodeListOf<HTMLElement>;
    expect(fills[0].style.width).toBe("100%");
    expect(fills[1].style.width).toBe("0%");

    const { container: c2 } = render(<Markdown text={'```progress\n{"items":[]}\n```'} />);
    expect(c2.querySelector(".md-progress")).toBeNull();
    expect(c2.querySelector("code")).not.toBeNull();
  });

  it("新形态与文字混合分段（kpi + table + 前后说明）", () => {
    const { container } = render(
      <Markdown text={
        "先看指标：\n\n```kpi\n{\"items\":[{\"label\":\"L\",\"value\":\"V\"}]}\n```\n\n" +
        "再列明细：\n\n```table\n{\"columns\":[\"k\",\"v\"],\"rows\":[[\"a\",\"1\"]]}\n```"
      } />
    );
    expect(container.querySelector(".md-kpi")).not.toBeNull();
    expect(container.querySelector(".md-table")).not.toBeNull();
    expect(screen.getByText("先看指标：")).toBeInTheDocument();
    expect(screen.getByText("再列明细：")).toBeInTheDocument();
  });
});
// ---------------------------------------------------------------- 代码块增强

describe("Markdown 代码块增强（语言标签 / 复制 / 长代码折叠）", () => {
  it("带语言的代码块显示语言标签与行数，并有复制按钮", () => {
    const { container } = render(<Markdown text={"```ts\nconst a = 1;\nconst b = 2;\n```"} />);
    expect(container.querySelector(".md-code-lang")?.textContent).toBe("ts");
    expect(container.querySelector(".md-code-meta")?.textContent).toBe("2 行");
    expect(container.querySelector(".md-code-copy")).not.toBeNull();
    // 短代码不折叠
    expect(container.querySelector(".md-code.collapsed")).toBeNull();
    expect(container.querySelector(".md-code-fold")).toBeNull();
  });

  it("无语言标注回退为 text", () => {
    const { container } = render(<Markdown text={"```\nplain\n```"} />);
    expect(container.querySelector(".md-code-lang")?.textContent).toBe("text");
  });

  it("超过 24 行的长代码默认折叠并给出展开按钮，点击后展开", () => {
    const long = Array.from({ length: 30 }, (_, i) => `line ${i + 1}`).join("\n");
    const { container } = render(<Markdown text={"```py\n" + long + "\n```"} />);
    expect(container.querySelector(".md-code.collapsed")).not.toBeNull();
    expect(container.querySelector(".md-code-meta")?.textContent).toBe("30 行");
    const fold = container.querySelector(".md-code-fold") as HTMLButtonElement;
    expect(fold.textContent).toContain("展开全部（30 行）");
    fireEvent.click(fold);
    expect(container.querySelector(".md-code.collapsed")).toBeNull();
    expect((container.querySelector(".md-code-fold") as HTMLButtonElement).textContent).toBe("收起");
  });

  it("高亮后的代码文本可被复制（flattenText 从嵌套 span 树提取）", () => {
    const { container } = render(<Markdown text={"```js\nconst x = 42;\n```"} />);
    // 代码文本仍在 DOM 内（复制源即该文本）
    expect(container.querySelector(".md-code pre")?.textContent).toContain("const x = 42;");
  });
});

// ---------------------------------------------------------------- 交互式图解

describe("Markdown 交互式图解（```interactive 分块点击切换）", () => {
  const SPEC =
    '```interactive\n{"title":"7 速自行车结构","summary":"点击分块查看各部分作用",' +
    '"numbered":true,"parts":[{"name":"车架","desc":"承载全部部件的骨架","meta":"铝合金"},' +
    '{"name":"传动","desc":"通过链条把脚踏力传到后轮","meta":"11-32T"},' +
    '{"name":"制动","desc":"盘式刹车，湿滑路面也能稳定减速"}]}\n```';

  it("渲染分块标签与默认选中块（首个），带序号与 meta", () => {
    const { container } = render(<Markdown text={SPEC} />);
    const tabs = container.querySelectorAll(".md-ix-part");
    expect(tabs.length).toBe(3);
    expect(tabs[0].className).toContain("active");      // 默认选中首个
    expect(container.querySelector(".md-ix-detail-name")?.textContent).toBe("车架");
    expect(container.querySelector(".md-ix-desc")?.textContent).toContain("骨架");
    expect(container.querySelector(".md-ix-summary")?.textContent).toContain("点击分块");
    // numbered=true → 显示序号
    expect(container.querySelectorAll(".md-ix-no").length).toBeGreaterThan(0);
  });

  it("点击其他分块切换详情内容（纯 state，无脚本执行）", () => {
    const { container } = render(<Markdown text={SPEC} />);
    const tabs = container.querySelectorAll(".md-ix-part");
    fireEvent.click(tabs[2]);
    expect(container.querySelector(".md-ix-detail-name")?.textContent).toBe("制动");
    expect(container.querySelector(".md-ix-desc")?.textContent).toContain("盘式刹车");
    expect(tabs[2].className).toContain("active");
    expect(tabs[0].className).not.toContain("active");
    // 切换后仍无 iframe/script（安全模型不变）
    expect(container.querySelector("iframe")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
  });

  it("组合（interactive × diagram）：part 带 shapes 的块显示聚焦视图，无 shapes 的块纯文字", () => {
    const combo =
      '```interactive\n{"title":"自行车各系统","canvas":{"w":470,"h":190},"parts":[' +
      '{"name":"传动","desc":"把脚踏力传到后轮","shapes":[' +
      '{"t":"circle","x":218,"y":95,"r":26},{"t":"line","x1":218,"y1":95,"x2":240,"y2":117}]},' +
      '{"name":"概述","desc":"纯文字块（无 shapes）"}]}\n```';
    const { container } = render(<Markdown text={combo} />);
    const tabs = container.querySelectorAll(".md-ix-part");
    expect(tabs.length).toBe(2);
    // 首块（传动）带 shapes → 聚焦 SVG 出现，viewBox 用 canvas
    let svg = container.querySelector(".md-diagram-svg");
    expect(svg).not.toBeNull();
    expect(svg!.getAttribute("viewBox")).toBe("0 0 470 190");
    expect(container.querySelectorAll(".md-diagram-svg circle").length).toBe(1);
    expect(container.querySelectorAll(".md-diagram-svg line").length).toBe(1);
    // 切到无 shapes 的块 → 聚焦 SVG 消失（纯文字）
    fireEvent.click(tabs[1]);
    expect(container.querySelector(".md-diagram-svg")).toBeNull();
    expect(container.querySelector(".md-ix-detail-name")?.textContent).toBe("概述");
    // 切回带 shapes 的块 → SVG 回来
    fireEvent.click(tabs[0]);
    expect(container.querySelector(".md-diagram-svg")).not.toBeNull();
  });

  it("组合：shapes 里的非白名单原语被清洗（同 diagram 安全规则）", () => {
    const combo =
      '```interactive\n{"parts":[{"name":"A","desc":"d","shapes":[' +
      '{"t":"text","content":"x"},{"t":"path","d":"M1 1 <script>onload=</script>"}]}]}\n```';
    const { container } = render(<Markdown text={combo} />);
    // text 丢弃；path 保留但 d 已清洗
    const path = container.querySelector(".md-diagram-svg path");
    expect(path).not.toBeNull();
    expect(path!.getAttribute("d")).not.toContain("<");
    expect(path!.getAttribute("d")).not.toContain("onload");
    expect(container.querySelector(".md-diagram-svg text")).toBeNull();
  });

  it("缺 name/desc 的分块被丢弃；全空 → 回退代码块", () => {
    const { container: c1 } = render(
      <Markdown text={'```interactive\n{"parts":[{"name":"只有名字"},{"name":"A","desc":"说明"}]}\n```'} />
    );
    expect(c1.querySelectorAll(".md-ix-part").length).toBe(1);

    const { container: c2 } = render(<Markdown text={'```interactive\n{"parts":[]}\n```'} />);
    expect(c2.querySelector(".md-ix-part")).toBeNull();
    expect(c2.querySelector("code")).not.toBeNull();
  });
});

// ---------------------------------------------------------------- 流式渐进渲染

describe("Markdown 流式渐进渲染（未闭合围栏随生成逐步出现）", () => {
  it("streaming=true：未闭合的 kpi 围栏也能渐进渲染（半截 JSON 自动补齐）", () => {
    // 生成到一半：JSON 未闭合，围栏也没有收尾 ```
    const partial =
      "先给指标：\n\n```kpi\n{\"items\":[{\"label\":\"营收\",\"value\":\"¥1.24M\"}," +
      "{\"label\":\"活跃\",\"value\":\"38.2k\"}";
    const { container } = render(<Markdown text={partial} streaming />);
    expect(container.querySelector(".md-kpi")).not.toBeNull();
    expect(container.querySelectorAll(".md-kpi").length).toBe(2);
    expect(screen.getByText("先给指标：")).toBeInTheDocument();
  });

  it("streaming=false（完整消息）：未闭合围栏保持为代码块，不猜测", () => {
    const partial = '```kpi\n{"items":[{"label":"营收","value":"¥1.24M"}]';
    const { container } = render(<Markdown text={partial} />);
    expect(container.querySelector(".md-kpi")).toBeNull();
  });

  it("流式：未闭合围栏内 JSON 完全不成形（尚无 items）→ 不渲染空块", () => {
    const { container } = render(<Markdown text={'```kpi\n{"items":['} streaming />);
    expect(container.querySelector(".md-kpi")).toBeNull();
  });

  it("流式：已闭合围栏正常渲染，未闭合的后续围栏也渐进渲染", () => {
    const text =
      "已完成：\n\n```table\n{\"columns\":[\"k\",\"v\"],\"rows\":[[\"a\",\"1\"]]}\n```\n\n" +
      "正在写：\n\n```kpi\n{\"items\":[{\"label\":\"L\",\"value\":\"V\"}";
    const { container } = render(<Markdown text={text} streaming />);
    expect(container.querySelector(".md-table")).not.toBeNull();
    expect(container.querySelector(".md-kpi")).not.toBeNull();
    expect(screen.getByText("已完成：")).toBeInTheDocument();
    expect(screen.getByText("正在写：")).toBeInTheDocument();
  });

  it("流式：未闭合的 html 围栏含标签即渲染（片段）", () => {
    const { container } = render(
      <Markdown text={"预览：\n\n```html\n<div><b>hi</b></div>"} streaming />
    );
    expect(container.querySelector("iframe.md-html-iframe")).not.toBeNull();
  });
});

// ---------------------------------------------------------------- 图形图解
describe("Markdown 图形图解（```diagram 结构化 SVG + 部件点击高亮）", () => {
  const SPEC =
    '```diagram\n{"title":"7 速自行车","canvas":{"w":470,"h":300},"parts":[' +
    '{"name":"车轮","desc":"承载与滚动的核心","shapes":[' +
    '{"t":"circle","x":110,"y":195,"r":72},{"t":"circle","x":110,"y":195,"r":7}]},' +
    '{"name":"车架","desc":"整车的骨架","shapes":[' +
    '{"t":"polyline","pts":[[110,195],[218,208],[192,108],[110,195]]}]}]}\n```';

  it("渲染分色 SVG（白名单原语）+ 标签 + 默认选中首个部件", () => {
    const { container } = render(<Markdown text={SPEC} />);
    expect(container.querySelector(".md-diagram-svg")).not.toBeNull();
    // 两个部件各成一个 <g>；首个 hot、其余 dim（点击高亮语义）
    const groups = container.querySelectorAll(".md-diagram-part");
    expect(groups.length).toBe(2);
    expect(groups[0].getAttribute("class")).toContain("hot");
    expect(groups[1].getAttribute("class")).toContain("dim");
    // 白名单原语：circle × 2 + polyline × 1
    expect(groups[0].querySelectorAll("circle").length).toBe(2);
    expect(groups[1].querySelectorAll("polyline").length).toBe(1);
    // viewBox 来自 canvas
    expect(container.querySelector(".md-diagram-svg")?.getAttribute("viewBox")).toBe("0 0 470 300");
    // 说明区显示首个部件
    expect(container.querySelector(".md-ix-detail-name")?.textContent).toBe("车轮");
  });

  it("点击第二个部件：高亮切换 + 说明切换（无脚本执行）", () => {
    const { container } = render(<Markdown text={SPEC} />);
    const groups = container.querySelectorAll(".md-diagram-part");
    fireEvent.click(groups[1]);
    expect(groups[1].getAttribute("class")).toContain("hot");
    expect(groups[0].getAttribute("class")).toContain("dim");
    expect(container.querySelector(".md-ix-detail-name")?.textContent).toBe("车架");
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("iframe")).toBeNull();
  });
  it("非白名单原语被丢弃（不渲染 polygon/text）；坐标越界 clamp", () => {
    const { container } = render(
      <Markdown text={
        '```diagram\n{"canvas":{"w":400,"h":300},"parts":[{"name":"A","desc":"d","shapes":[' +
        '{"t":"polygon","points":"1,2 3,4"},' +
        '{"t":"circle","x":99999,"y":-99999,"r":50},' +
        '{"t":"text","content":"恶意文本"}]}]}\n```'
      } />
    );
    const groups = container.querySelectorAll(".md-diagram-part");
    // polygon / text 不在白名单 → 丢弃；circle 保留但坐标被 clamp
    expect(groups.length).toBe(1);
    expect(groups[0].querySelectorAll("polygon").length).toBe(0);
    expect(groups[0].querySelectorAll("text").length).toBe(0);
    const c = groups[0].querySelector("circle");
    expect(c).not.toBeNull();
    expect(Number(c!.getAttribute("cx"))).toBeLessThanOrEqual(450); // 400+50
    expect(Number(c!.getAttribute("cy"))).toBeGreaterThanOrEqual(-50);
  });

  it("path d 清洗：剥离尖括号/花括号/事件属性注入尝试", () => {
    const { container } = render(
      <Markdown text={
        '```diagram\n{"parts":[{"name":"A","desc":"d","shapes":[' +
        '{"t":"path","d":"M 10 10 L 20 20 <script>onload=javascript:alert(1)</script>"}]}]}\n```'
      } />
    );
    const d = container.querySelector(".md-diagram-part path")?.getAttribute("d") ?? "";
    expect(d).toContain("M 10 10");
    expect(d).not.toContain("<");
    expect(d).not.toContain("javascript:");
    expect(d).not.toContain("onload");
  });

  it("无有效原语的部件被丢弃；全部无效 → 回退代码块", () => {
    const { container } = render(
      <Markdown text={
        '```diagram\n{"parts":[{"name":"空","desc":"无形状","shapes":[]},' +
        '{"name":"B","desc":"ok","shapes":[{"t":"circle","x":1,"y":1,"r":2}]}]}\n```'
      } />
    );
    expect(container.querySelectorAll(".md-diagram-part").length).toBe(1);

    const { container: c2 } = render(
      <Markdown text={'```diagram\n{"parts":[{"name":"空","desc":"无","shapes":[]}]}\n```'} />
    );
    expect(c2.querySelector(".md-diagram-svg")).toBeNull();
    expect(c2.querySelector("code")).not.toBeNull();
  });
});