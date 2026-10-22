// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 「套用模板重排」页签的行为（用户反馈过两次）：
//
// 1. 旧实现点「交给 AI 规划重排」只把提示词丢进对话页签，模板页签留在原地
//    什么也不做 —— 那个 tab 就废了。现在重排完全在页签内跑：读源结构 →
//    自动规划 → 生成 pptx → 原地变成编辑器。
// 2. 旧「从模板做 PPT」要逐页手填占位符（10 页要点 30+ 次输入框）。现在
//    主路径是「一句话需求 → 规划 → 微调 → 生成」，手填降级为高级模式。

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import PptTemplatePicker from "./PptTemplatePicker";

const EXTRACT = {
  source: "源稿.pptx",
  total: 2,
  slides: [
    { slide: 1, title: "标题一", paragraphs: ["段落 a"], layout_guess: "content",
      has_table: false, has_chart: false, has_picture: false },
    { slide: 2, title: "标题二", paragraphs: ["段落 b"], layout_guess: "content",
      has_table: false, has_chart: false, has_picture: false },
  ],
};

const PLAN = {
  ok: true,
  fallback: false,
  error: null,
  pages: [
    { page: "title_slide", label: "封面", values: { TITLE: "AI 标题", SUBTITLE: "副" } },
    { page: "title_content", label: "标题 + 内容",
      values: { PAGE_TITLE: "AI 页标题", CONTENT_AREA: "要点甲\n要点乙" } },
  ],
};

vi.mock("../api", () => ({
  api: {
    pptTemplates: vi.fn(),
    pptExtract: vi.fn(),
    pptPlan: vi.fn(),
    pptCreate: vi.fn(),
    pptImages: vi.fn(),
  },
}));

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const mocks = (await import("../api")).api as any;

const LIB = {
  layouts: [{
    suite: "presentation_core", label: "通用演示（16:9，20 页）", page_count: 20,
    pages: [
      { file: "01_title_slide.svg", slug: "title_slide", label: "封面", placeholders: ["TITLE", "SUBTITLE"] },
      { file: "02_title_content.svg", slug: "title_content", label: "标题 + 内容",
        placeholders: ["PAGE_TITLE", "CONTENT_AREA"] },
    ],
  }],
  brands: [], styles: [],
};

beforeEach(() => {
  mocks.pptTemplates.mockResolvedValue(LIB);
  mocks.pptExtract.mockResolvedValue(EXTRACT);
  mocks.pptPlan.mockResolvedValue(PLAN);
  mocks.pptCreate.mockResolvedValue({ output: "out.pptx", project: "out", slides: 2, warnings: [] });
  // 默认「没有生图模型，但有图库」——这是未配 OpenAI 的用户最常见的状态，
  // 配图应当**可用**（走开放图库）而不是被禁用
  mocks.pptImages.mockResolvedValue({
    mode: "stock", can_generate: false, can_stock: true,
    backends: [], providers: [
      { provider: "openverse", label: "Openverse（CC0/公版，免署名）", zero_config: true, no_attribution: true },
      { provider: "wikimedia", label: "Wikimedia Commons（多为 CC BY-SA，需署名）", zero_config: true, no_attribution: false },
    ],
    default_backend: "", enabled: false,
  });
});

afterEach(() => { vi.clearAllMocks(); });

describe("从模板做 PPT · 提示词生成（主路径）", () => {
  it("默认就是 AI 模式，且空需求时主按钮禁用", async () => {
    render(<PptTemplatePicker onCreated={() => {}} onCancel={() => {}} />);
    expect(await screen.findByRole("tab", { name: /AI 生成/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("button", { name: /规划方案/ })).toBeDisabled();
  });

  it("输入需求 → 规划 → 逐页方案出现（版式与文案来自后端）", async () => {
    const user = userEvent.setup();
    render(<PptTemplatePicker onCreated={() => {}} onCancel={() => {}} />);
    const box = await screen.findByPlaceholderText(/一句话说明主题/);
    await user.type(box, "给管理层做 Q3 复盘");
    await user.click(screen.getByRole("button", { name: /规划方案/ }));

    await waitFor(() => expect(mocks.pptPlan).toHaveBeenCalledTimes(1));
    expect(mocks.pptPlan.mock.calls[0][0]).toMatchObject({
      suite: "presentation_core", brief: "给管理层做 Q3 复盘",
    });
    // 页标题出现在页列表里
    expect(await screen.findByText("AI 标题")).toBeInTheDocument();
    expect(screen.getByText(/排好 2 页/)).toBeInTheDocument();
  });

  it("规划后主按钮变成「生成」，且把规划结果送进 pptCreate", async () => {
    const user = userEvent.setup();
    const onCreated = vi.fn();
    render(<PptTemplatePicker onCreated={onCreated} onCancel={() => {}} />);
    await user.type(await screen.findByPlaceholderText(/一句话说明主题/), "做个复盘");
    await user.click(screen.getByRole("button", { name: /规划方案/ }));
    await screen.findByText("AI 标题");

    await user.click(screen.getByRole("button", { name: /生成并打开编辑器/ }));
    await waitFor(() => expect(mocks.pptCreate).toHaveBeenCalledTimes(1));
    const spec = mocks.pptCreate.mock.calls[0][0];
    // slug 归一成模板文件名
    expect(spec.pages[0]).toMatchObject({ page: "01_title_slide.svg" });
    expect(spec.pages[1].values.CONTENT_AREA).toBe("要点甲\n要点乙");
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(
      { project: "out", output: "out.pptx", slides: 2 }));
  });

  it("改了文案后生成，用改后的值", async () => {
    const user = userEvent.setup();
    render(<PptTemplatePicker onCreated={() => {}} onCancel={() => {}} />);
    await user.type(await screen.findByPlaceholderText(/一句话说明主题/), "做个复盘");
    await user.click(screen.getByRole("button", { name: /规划方案/ }));
    await screen.findByText("AI 标题");

    // 第 2 页（内容页）的正文是一个 wide textarea
    await user.click(screen.getByText("AI 页标题"));
    const area = document.querySelector(".ppt-tpl-plan-editor textarea") as HTMLTextAreaElement;
    expect(area.value).toBe("要点甲\n要点乙");
    await user.clear(area);
    await user.type(area, "改过的要点");

    await user.click(screen.getByRole("button", { name: /生成并打开编辑器/ }));
    await waitFor(() => expect(mocks.pptCreate).toHaveBeenCalled());
    const spec = mocks.pptCreate.mock.calls[0][0];
    expect(spec.pages[1].values.CONTENT_AREA).toBe("改过的要点");
  });

  it("模型降级时说明原因并仍给出可编辑骨架", async () => {
    const user = userEvent.setup();
    mocks.pptPlan.mockResolvedValue({
      ok: true, fallback: true, error: "模型调用失败：超时",
      pages: [{ page: "title_slide", label: "封面", values: { TITLE: "骨架标题" } }],
    });
    render(<PptTemplatePicker onCreated={() => {}} onCancel={() => {}} />);
    await user.type(await screen.findByPlaceholderText(/一句话说明主题/), "做个复盘");
    await user.click(screen.getByRole("button", { name: /规划方案/ }));
    expect(await screen.findByText(/模型调用失败：超时/)).toBeInTheDocument();
    expect(screen.getByText("骨架标题")).toBeInTheDocument();
  });

  it("规划报错时把错误显示出来（不再静默）", async () => {
    const user = userEvent.setup();
    mocks.pptPlan.mockRejectedValue(new Error("422 请填写需求描述"));
    render(<PptTemplatePicker onCreated={() => {}} onCancel={() => {}} />);
    await user.type(await screen.findByPlaceholderText(/一句话说明主题/), "x");
    await user.click(screen.getByRole("button", { name: /规划方案/ }));
    expect(await screen.findByText(/请填写需求描述/)).toBeInTheDocument();
  });
});

describe("从模板做 PPT · 手动编排（降级路径）", () => {
  it("切到手动模式后可以逐页填并生成（不再需要 AI）", async () => {
    const user = userEvent.setup();
    render(<PptTemplatePicker onCreated={() => {}} onCancel={() => {}} />);
    await user.click(await screen.findByRole("tab", { name: /手动编排/ }));
    expect(screen.getByRole("button", { name: /生成并打开编辑器/ })).toBeInTheDocument();
    expect(mocks.pptPlan).not.toHaveBeenCalled();
  });
});

describe("套用模板重排 · 页签内闭环", () => {
  it("读完源结构后**自动**规划一次，不需要再点按钮", async () => {
    render(<PptTemplatePicker sourcePath="源稿.pptx" onCreated={() => {}} onCancel={() => {}} />);
    await waitFor(() => expect(mocks.pptPlan).toHaveBeenCalledTimes(1));
    expect(mocks.pptPlan.mock.calls[0][0]).toMatchObject({
      suite: "presentation_core", source_path: "源稿.pptx",
    });
    // 源结构与方案同时在场
    expect(await screen.findByText(/段落 a/)).toBeInTheDocument();
    expect(await screen.findByText("AI 标题")).toBeInTheDocument();
  });

  it("生成完成后回调 onCreated（由上层原地替换成编辑器页签）", async () => {
    const user = userEvent.setup();
    const onCreated = vi.fn();
    render(<PptTemplatePicker sourcePath="源稿.pptx" onCreated={onCreated} onCancel={() => {}} />);
    await screen.findByText("AI 标题");
    await user.click(screen.getByRole("button", { name: /生成并打开编辑器/ }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(
      { project: "out", output: "out.pptx", slides: 2 }));
  });

  it("重排不再有「交给 AI 规划重排」这种把用户踢到对话页签的按钮", async () => {
    render(<PptTemplatePicker sourcePath="源稿.pptx" onCreated={() => {}} onCancel={() => {}} />);
    await screen.findByText(/段落 a/);
    expect(screen.queryByRole("button", { name: /交给 AI 规划/ })).toBeNull();
  });
});

describe("PptTemplatePicker · 样式契约", () => {
  const css = readFileSync(resolve(process.cwd(), "src/styles.css"), "utf-8");

  // 回归点：全局 button 重置 border/background，模板页签曾没补 `.btn` 样式，
  // 主按钮被渲染成 19px 高的透明裸文字。
  it("模板页签内必须给 .btn 补回背景/边框/内边距", () => {
    const i = css.indexOf("\n.ppt-tpl .btn {");
    expect(i, "缺少 `.ppt-tpl .btn` 规则").toBeGreaterThan(-1);
    const block = css.slice(i, css.indexOf("}", i));
    expect(block).toMatch(/padding:\s*\d/);
    expect(block).toMatch(/background:/);
    expect(block).toMatch(/border:/);
  });

  it(".ppt-tpl-actions 固定在滚动区之外（flex-shrink:0 + 上边框）", () => {
    const i = css.indexOf("\n.ppt-tpl-actions {");
    expect(i, "缺少 `.ppt-tpl-actions` 规则").toBeGreaterThan(-1);
    const block = css.slice(i, css.indexOf("}", i));
    expect(block).toMatch(/flex-shrink:\s*0/);
    expect(block).toMatch(/border-top:/);
  });

  it("模式切换与提示词卡片有样式（新增的主路径不能是裸元素）", () => {
    for (const sel of ["\n.ppt-tpl-modes {", "\n.ppt-tpl-brief {", "\n.ppt-tpl-plan {"]) {
      expect(css.indexOf(sel), `缺少 ${sel.trim()} 规则`).toBeGreaterThan(-1);
    }
  });
});

describe("ppt-master 缺失 · 必须提示而不是显示空模板库", () => {
  const UNAVAILABLE = {
    ready: false,
    skill_dir: null,
    layouts: 0,
    scripts: false,
    reason: "找不到内置 ppt-master 技能（skills/ppt-master）。",
    hint: "ppt-master 是随安装包分发的内置技能，不需要单独安装。请确认安装完整。",
  };

  it("说明缺什么、怎么修，并给重新检测入口", async () => {
    mocks.pptTemplates.mockResolvedValue({
      layouts: [], brands: [], styles: [], available: UNAVAILABLE,
    });
    render(<PptTemplatePicker onCreated={() => {}} onCancel={() => {}} />);

    expect(await screen.findByText(/缺少内置技能 ppt-master/)).toBeInTheDocument();
    expect(screen.getByText(UNAVAILABLE.reason)).toBeInTheDocument();
    expect(screen.getByText(UNAVAILABLE.hint)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /重新检测/ })).toBeInTheDocument();
    // 关键：不能只是「正在读取模板库…」那种含糊状态
    expect(screen.queryByText(/正在读取模板库/)).toBeNull();
  });

  it("明说 ppt-master 是内置技能、不需要单独安装", async () => {
    mocks.pptTemplates.mockResolvedValue({
      layouts: [], brands: [], styles: [], available: UNAVAILABLE,
    });
    const { container } = render(
      <PptTemplatePicker onCreated={() => {}} onCancel={() => {}} />);
    await screen.findByRole("button", { name: /重新检测/ });
    // 文案跨 <b> 断行，按整段文本断言
    const note = container.querySelector(".ppt-tpl-unavailable-note")?.textContent ?? "";
    expect(note).toContain("随安装包分发的内置技能");
    expect(note).toContain("不需要单独安装");
  });

  it("修复后点「重新检测」能恢复出模板列表", async () => {
    mocks.pptTemplates.mockResolvedValueOnce({
      layouts: [], brands: [], styles: [], available: UNAVAILABLE,
    });
    const user = userEvent.setup();
    render(<PptTemplatePicker onCreated={() => {}} onCancel={() => {}} />);
    await screen.findByRole("button", { name: /重新检测/ });

    mocks.pptTemplates.mockResolvedValueOnce({ ...LIB, available: {
      ready: true, skill_dir: "/x/ppt-master", layouts: 1, scripts: true,
      reason: "", hint: "",
    } });
    await user.click(screen.getByRole("button", { name: /重新检测/ }));
    expect(await screen.findByRole("tab", { name: /AI 生成/ })).toBeInTheDocument();
  });

  it("模板库为空但技能正常时，不误报「缺技能」", async () => {
    mocks.pptTemplates.mockResolvedValue({
      layouts: [], brands: [], styles: [],
      available: { ready: true, skill_dir: "/x", layouts: 0, scripts: true, reason: "", hint: "" },
    });
    render(<PptTemplatePicker onCreated={() => {}} onCancel={() => {}} />);
    expect(await screen.findByRole("tab", { name: /手动编排/ })).toBeInTheDocument();
    expect(screen.queryByText(/缺少内置技能/)).toBeNull();
  });
});

describe("生图配图 · 复用已配置的供应商凭据", () => {
  const BACKENDS = [
    { backend: "openai", label: "OpenAI", key_env: "OPENAI_API_KEY", from_llm_provider: true },
    { backend: "qwen", label: "阿里通义万相", key_env: "QWEN_API_KEY", from_llm_provider: true },
  ];
  const GEN_CAP = {
    mode: "generate", can_generate: true, can_stock: true,
    backends: BACKENDS, providers: [],
    default_backend: "openai", enabled: true,
  };

  it("没有生图模型但有图库：开关**可用**，并说明走图库", async () => {
    render(<PptTemplatePicker onCreated={() => {}} onCancel={() => {}} />);
    const box = await screen.findByRole("checkbox", { name: /自动配图/ });
    await waitFor(() => expect(box).toBeEnabled());
    expect(screen.getByText(/开放图库检索配图/)).toBeInTheDocument();
  });

  it("两者都没有：开关禁用并说明原因", async () => {
    mocks.pptImages.mockResolvedValue({
      mode: "none", can_generate: false, can_stock: false,
      backends: [], providers: [], default_backend: "", enabled: false,
    });
    render(<PptTemplatePicker onCreated={() => {}} onCancel={() => {}} />);
    const box = await screen.findByRole("checkbox", { name: /自动配图/ });
    await waitFor(() => expect(box).toBeDisabled());
    expect(screen.getByText(/无法配图/)).toBeInTheDocument();
  });

  it("有凭据时可勾选，并在规划时带上 with_images", async () => {
    mocks.pptImages.mockResolvedValue(GEN_CAP);
    const user = userEvent.setup();
    render(<PptTemplatePicker onCreated={() => {}} onCancel={() => {}} />);
    const box = await screen.findByRole("checkbox", { name: /用生图模型配图/ });
    await waitFor(() => expect(box).toBeEnabled());
    await user.click(box);

    await user.type(await screen.findByPlaceholderText(/一句话说明主题/), "做一份复盘");
    await user.click(screen.getByRole("button", { name: /规划方案/ }));
    await waitFor(() => expect(mocks.pptPlan).toHaveBeenCalled());
    expect(mocks.pptPlan.mock.calls[0][0].with_images).toBe(true);
  });

  it("生成时把配图提示词作为 images 传给后端（键为「页码:图片位」）", async () => {
    mocks.pptImages.mockResolvedValue(GEN_CAP);
    mocks.pptPlan.mockResolvedValue({
      ...PLAN,
      pages: [
        { ...PLAN.pages[0], image_prompts: { IMAGE_1: "modern office, corporate photography" } },
        PLAN.pages[1],
      ],
    });
    const user = userEvent.setup();
    render(<PptTemplatePicker onCreated={() => {}} onCancel={() => {}} />);
    const box = await screen.findByRole("checkbox", { name: /用生图模型配图/ });
    await waitFor(() => expect(box).toBeEnabled());
    await user.click(box);
    await user.type(await screen.findByPlaceholderText(/一句话说明主题/), "做个复盘");
    await user.click(screen.getByRole("button", { name: /规划方案/ }));
    await screen.findByText("AI 标题");
    // 页编辑器里能看到配图提示词
    expect(await screen.findByText(/本页配图/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /生成并打开编辑器/ }));
    await waitFor(() => expect(mocks.pptCreate).toHaveBeenCalled());
    const third = mocks.pptCreate.mock.calls[0][2];
    expect(third).toMatchObject({
      spec: { "1:IMAGE_1": "modern office, corporate photography" },
      backend: "openai",
    });
  });

  it("不勾配图时 pptCreate 不带 images（不花额度）", async () => {
    mocks.pptImages.mockResolvedValue(GEN_CAP);
    const user = userEvent.setup();
    render(<PptTemplatePicker onCreated={() => {}} onCancel={() => {}} />);
    await user.type(await screen.findByPlaceholderText(/一句话说明主题/), "做个复盘");
    await user.click(screen.getByRole("button", { name: /规划方案/ }));
    await screen.findByText("AI 标题");
    await user.click(screen.getByRole("button", { name: /生成并打开编辑器/ }));
    await waitFor(() => expect(mocks.pptCreate).toHaveBeenCalled());
    expect(mocks.pptCreate.mock.calls[0][2]).toBeUndefined();
  });
});
