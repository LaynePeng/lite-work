// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

// 历史消息 → 渲染轮次（RenderTurn）的增量构建器。
//
// 为什么需要增量：ChatView 弃用虚拟列表后，所有轮次都进 DOM。
// 原实现每次 messages 变化（追加消息/任务落定）都全量重建所有
// turn 对象 → React.memo 引用比较全部失效 → 整棵列表（含全部
// Markdown）重渲染。超长会话（几百轮+）下任务落定是高频事件，
// 全量重渲染会造成可感知的卡顿。
//
// 增量策略（P0 性能防御，设计经过辩论评审）：
// 1. 前缀指纹校验：缓存按「已消费前缀的消息引用」逐个 === 比对。
//    App 侧 messages 是不可变更新（纯追加时历史引用不变；编辑/删除
//    时引用变化）——引用比对即可区分追加与回退，O(n) 指针比较。
//    不可只用「数组引用+长度」：不可变更新数组引用每次都变，且
//    「删 2 条再补 2 条」长度相同会误判为追加。
// 2. 纯追加：历史 turn 对象引用复用 → memo 跳过重渲染，只有新
//    turn 构建，成本 O(新增消息数)。
// 3. 跨批次 tool 回填（不可变更新）：tool 结果晚于 tool_call 到达时，
//    为目标 turn 生成新对象（新 items + 新 card），替换 turns[idx]——
//    引用变化触发该轮 memo 重渲染，其余轮次不受影响。禁止原地改
//    card.result：复用 turn 引用 + 原地改嵌套字段 = memo 跳过 = UI
//    不更新（辩论复审发现的矛盾点）。
// 4. 前缀校验失败（编辑/删除）：重置缓存从头全量消费，等价于原
//    全量重建；索引 key（u-N/a-N/t-N）重新生成，React 按 key 协调
//    仍然正确，只是 memo 优化在回退场景失效（可接受）。

import type { Msg, ToolCardInfo, WorkItem } from "../types";

export interface RenderTurn {
  key: string;
  user?: Msg;
  items: WorkItem[];
  assistant?: Msg;
  /** 流式轮次的回合号（仅 key 以 streaming- 开头的轮次有值） */
  streamTurn?: number;
}

/** cardId → 所在 turn/items 的位置（跨批次 tool 回填索引） */
interface PendingCard {
  turnIdx: number;
  itemIdx: number;
}

export interface TurnsCache {
  /** 已消费的 messages 前缀长度（尾部游标） */
  consumed: number;
  /** 前缀消息引用指纹（与 messages[0..consumed) 逐个 === 比对） */
  prefixRefs: Msg[];
  /** 已构建的轮次（与 consumed 前缀对应；元素可能被回填替换为新对象） */
  turns: RenderTurn[];
  /** cardId → 位置；回填后保留（同 id 重复回填幂等覆盖，与原实现一致） */
  pending: Map<string, PendingCard>;
  /** 当前可延续的 tool-turn 下标（连续 assistant-with-tools 合并进同一轮）；null=无开放轮 */
  openTurnIdx: number | null;
  /** 上次返回给调用方的数组（幂等分支同引用返回；消费分支浅拷贝，避免旧返回值被后续 push 变异） */
  lastReturned: RenderTurn[];
}

export interface BuildResult {
  cache: TurnsCache;
  turns: RenderTurn[];
}

function parseToolArgs(raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

/** 从 tool 结果文本推导卡片状态（与历史实现一致） */
function cardStatusOf(content: string): ToolCardInfo["status"] {
  if (content.startsWith("[Tool Execution Cancelled]") || content.startsWith("[Blocked")) return "cancelled";
  if (content.startsWith("[Execution Exception]") || content.startsWith("[Error]")) return "error";
  return "done";
}

/**
 * 增量消费 messages[start..)：状态机与原全量 buildTurns 一致——
 * user → 新轮并关闭开放轮；纯 assistant → 新轮并关闭；assistant
 * with tool_calls → 延续开放轮（新对象）或新建；tool → 查 pending 回填。
 */
function consume(cache: TurnsCache, messages: Msg[], start: number): void {
  const { turns, pending } = cache;
  for (let i = start; i < messages.length; i++) {
    const m = messages[i];
    if (m.role === "user") {
      cache.openTurnIdx = null;
      turns.push({ key: `u-${turns.length}`, user: m, items: [] });
    } else if (m.role === "assistant") {
      const toolCalls = m.tool_calls ?? [];
      if (toolCalls.length === 0) {
        cache.openTurnIdx = null;
        turns.push({ key: `a-${turns.length}`, items: [], assistant: m });
        continue;
      }
      // 延续开放轮（不可变更新：新 items 数组 + 新 turn 对象）或新建
      const turnIdx = cache.openTurnIdx ?? (() => {
        turns.push({ key: `t-${turns.length}`, items: [] });
        return turns.length - 1;
      })();
      cache.openTurnIdx = turnIdx;
      const items = [...turns[turnIdx].items];
      if (m.content) {
        items.push({ type: "text", id: `text-${turns.length}-${items.length}`, content: m.content, agent: m.agent });
      }
      for (const tc of toolCalls) {
        const card: ToolCardInfo = {
          id: tc.id || `h-${items.length}-${Math.random().toString(36).slice(2, 6)}`,
          name: tc.function.name,
          args: parseToolArgs(tc.function.arguments),
          status: "done",
        };
        pending.set(card.id, { turnIdx, itemIdx: items.length });
        items.push({ type: "tool", id: card.id, card });
      }
      turns[turnIdx] = { ...turns[turnIdx], items };
    } else if (m.role === "tool") {
      const loc = m.tool_call_id ? pending.get(m.tool_call_id) : undefined;
      if (!loc) continue; // 孤儿结果（找不到对应 tool_call）：跳过，与原实现一致
      const turn = turns[loc.turnIdx];
      const item = turn.items[loc.itemIdx];
      if (item.type !== "tool") continue;
      // 不可变回填：新 card + 新 items + 新 turn，仅该轮引用变化
      const newCard: ToolCardInfo = { ...item.card, result: m.content ?? "", status: cardStatusOf(m.content ?? "") };
      const newItems = turn.items.slice();
      newItems[loc.itemIdx] = { type: "tool", id: item.id, card: newCard };
      turns[loc.turnIdx] = { ...turn, items: newItems };
    }
  }
}

/** 新建空缓存（也用于前缀失效后的全量重建起点） */
function emptyCache(): TurnsCache {
  return { consumed: 0, prefixRefs: [], turns: [], pending: new Map(), openTurnIdx: null, lastReturned: [] };
}

/**
 * 带 cache 的增量 buildTurns。
 *
 * - 首次（cache=null）或前缀校验失败：重置缓存从头消费（全量重建）
 * - 无新增（consumed === messages.length）：原样返回同一 turns 引用
 *   （幂等：React StrictMode 双调用 / 相同 props 重渲染均安全）
 * - 纯追加：只消费新增段，历史 turn 对象引用复用
 *
 * 注意：cache 由调用方持有（如 useRef），每次传入上次结果。
 */
export function buildTurnsCached(prev: TurnsCache | null, messages: Msg[]): BuildResult {
  let cache = prev;
  if (cache) {
    const n = cache.consumed;
    let prefixOk = n <= messages.length;
    if (prefixOk) {
      for (let i = 0; i < n; i++) {
        if (cache.prefixRefs[i] !== messages[i]) {
          prefixOk = false;
          break;
        }
      }
    }
    if (!prefixOk) cache = null; // 编辑/删除/回退：全量重建
  }
  if (!cache) cache = emptyCache();

  if (cache.consumed === messages.length) {
    return { cache, turns: cache.lastReturned }; // 无新增：同引用返回（幂等）
  }

  consume(cache, messages, cache.consumed);
  cache.prefixRefs = messages.slice(); // 全量引用指纹（O(n) 指针，成本可忽略）
  cache.consumed = messages.length;
  // 浅拷贝返回：cache.turns 是内部数组，后续消费会继续 push/替换元素——
  // 直接返回内部数组会让「上次返回值」在下次追加时被变异（React 渲染
  // 期间旧结果被改写，违反不可变预期）
  cache.lastReturned = [...cache.turns];
  return { cache, turns: cache.lastReturned };
}
