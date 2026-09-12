// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors

/** Agent 图标元数据共享模块：AGENT_META / ICON_CHOICES / agentIconOf。

  三处消费（ChatView 气泡 / Composer Agent 条 / ToolPanel 编排者卡片）统一
  走 agentIconOf 的 fallback 链，避免各处各写一份映射：
    profile.icon（自定义 agent 显式选择）→ AGENT_META 内置映射 → 🤖 兜底
*/

export const AGENT_META: Record<string, { icon: string; label: string }> = {
  build: { icon: "💻", label: "代码" },
  plan: { icon: "📋", label: "规划" },
  office: { icon: "📄", label: "办公" },
  research: { icon: "🔎", label: "调研" },
};

export const agentMeta = (id: string) => AGENT_META[id] ?? { icon: "🤖", label: id };

/** 自定义 Agent 的图标候选（emoji 文本，零资源成本；保存到 profile.icon） */
export const ICON_CHOICES = [
  "💻", "📋", "📄", "🔎", "🤖", "⚡", "🧠", "📦", "🔧", "🧪",
  "🎨", "📊", "📚", "🛠️", "🚀", "💡", "🔮", "🧭", "🗂️", "⚙️",
  "🧩", "🌐", "📝", "🎯",
];

/** 图标 fallback 链：显式 icon（自定义 agent 选择的 emoji）→ 内置映射 → 🤖 */
export function agentIconOf(agentId: string | undefined, profileIcon?: string | null): string {
  const id = (agentId || "").trim();
  if (!id) return "🤖";
  if (profileIcon && profileIcon.trim()) return profileIcon.trim();
  return agentMeta(id).icon;
}
