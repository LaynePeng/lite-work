// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

// 「发起对话」的落点页签解析。
//
// 为什么需要：文件页签 / PPT 编辑器**没有 sessionId**，而在这些页签里点
// 「交 AI 应用」时，消息最终走 App.send()。旧实现直接把 activeTabId 当成
// 会话归属页签，于是出现：
//   1) send 发现该页签没有 session → 新建 session 并**绑到 PPT 页签上**；
//   2) activeSessionId 只认 kind==="chat"，界面仍显示编辑器；
//   3) 会话建好了、任务也跑起来了，但没有任何页签能看见它 ——
//      用户视角就是「点了没反应」。
//
// 因此 send 前先解析出真正的对话页签：优先当前页签（本来就是对话），
// 否则复用已存在的空对话页签，都没有才新建一个。

import type { TabItem } from "../types";

export interface ChatTabTarget {
  /** 消息应当落到哪个对话页签 */
  tabId: string;
  /** 需要新插入的页签（tabs 里已有可用页签时为 null） */
  create: TabItem | null;
}

/**
 * @param tabs 当前页签列表
 * @param activeTabId 当前激活页签
 * @param makeId 生成新页签 id（App 侧复用同一个自增序列）
 */
export function resolveChatTab(
  tabs: TabItem[],
  activeTabId: string,
  makeId: () => string,
): ChatTabTarget {
  const active = tabs.find((t) => t.id === activeTabId);
  if (active?.kind === "chat") return { tabId: active.id, create: null };
  // 复用「还没绑定会话的空对话页签」，避免每点一次就多堆一个页签
  const draft = tabs.find((t) => t.kind === "chat" && !t.sessionId);
  if (draft) return { tabId: draft.id, create: null };
  const id = makeId();
  return { tabId: id, create: { id, kind: "chat", title: "新会话" } };
}
