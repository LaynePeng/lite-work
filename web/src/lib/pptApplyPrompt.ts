// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

// 「交 AI 应用」把编辑器里的标注打包成一条普通 chat 消息。
//
// 单独成文件的理由：提示词里写死的工具调用签名必须与 ppt_edit 的实际参数
// 校验一致——清标注要**逐个**带 slide + element_id（只传 note="__clear__"
// 会被工具直接拒），这类约束容易被后续改动悄悄改坏，抽成纯函数才能锁测试。

import type { PptAnnotation } from "../api";

export function buildPptApplyPrompt(payload: {
  project: string;
  source: string;
  annotations: PptAnnotation[];
}): string {
  const lines = payload.annotations.map(
    (a) => `- 第 ${a.slide} 页 · 元素 ${a.element_id}：${a.note}`,
  );
  return (
    `[PPT 编辑器] 我在编辑器里标注了 ${payload.annotations.length} 处修改，请逐条应用：\n` +
    `工作区：${payload.project}（源文件 ${payload.source}）\n` +
    lines.join("\n") +
    `\n\n请用 ppt_edit(op="outline", project="${payload.project}") 读取当前结构，` +
    `用 op="edit" 逐条落实改动；确定性的属性改动（文字/字号/颜色/位置/表格单元格/` +
    `图表数值）直接改，需要重新排版或改措辞的按标注意图改；` +
    `完成后用 op="export" 导出新 pptx。\n` +
    // 逐条清标注：ppt_edit 的 annotate 要求 slide + element_id 同时给，
    // 只传 note="__clear__" 会报「op=annotate 需要 slide / element_id / note」
    `每应用完一处，用该处的 slide 与 element_id 调 ` +
    `ppt_edit(op="annotate", project="${payload.project}", slide=<页>, ` +
    `element_id="<元素>", note="__clear__") 清掉标注。`
  );
}
