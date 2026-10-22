// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 「交 AI 应用」提示词：锁住与 ppt_edit 参数校验的对齐。
//
// 真实踩过：提示词里让 Agent「用 op="annotate" 传 note="__clear__" 清除标注」，
// 而工具侧要求 slide + element_id 同时给 → Agent 照做必报
// 「op=annotate 需要 slide / element_id / note」，标注永远清不掉。

import { describe, expect, it } from "vitest";
import { buildPptApplyPrompt } from "./pptApplyPrompt";

const PAYLOAD = {
  project: "季度复盘",
  source: "季度复盘.pptx",
  annotations: [
    { slide: 2, element_id: "shape-3", note: "这行太挤，精简成 3 个要点", text: ["很长的段落"] },
    { slide: 5, element_id: "shape-7", note: "换成更商务的配图", text: [] },
  ],
};

describe("buildPptApplyPrompt", () => {
  const prompt = buildPptApplyPrompt(PAYLOAD);

  it("逐条列出标注（页码 + 元素 id + 原话）", () => {
    expect(prompt).toContain("第 2 页 · 元素 shape-3：这行太挤，精简成 3 个要点");
    expect(prompt).toContain("第 5 页 · 元素 shape-7：换成更商务的配图");
    expect(prompt).toContain("2 处修改");
  });

  it("带上工作区与源文件，后续 op 不必再猜 project", () => {
    expect(prompt).toContain('op="outline", project="季度复盘"');
    expect(prompt).toContain("源文件 季度复盘.pptx");
  });

  it("清标注必须显式要求 slide + element_id（回归点：只传 note 会被工具拒绝）", () => {
    const clearLine = prompt.split("\n").find((l) => l.includes("__clear__"))!;
    expect(clearLine).toBeTruthy();
    expect(clearLine).toContain("slide=<页>");
    expect(clearLine).toContain('element_id="<元素>"');
    expect(clearLine).toContain('op="annotate"');
  });

  it("给出完整的执行顺序：outline → edit → export", () => {
    expect(prompt).toContain('op="outline"');
    expect(prompt).toContain('op="edit"');
    expect(prompt).toContain('op="export"');
  });
});
