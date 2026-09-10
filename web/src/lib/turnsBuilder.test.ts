// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import { describe, expect, it } from "vitest";
import { buildTurnsCached, type TurnsCache } from "./turnsBuilder";
import type { Msg } from "../types";

// ---------------------------------------------------------------- 夹具

function userMsg(content: string): Msg {
  return { role: "user", content } as Msg;
}

function toolCallMsg(callId: string, name = "read_file", args = '{"filePath":"a.ts"}'): Msg {
  return {
    role: "assistant",
    content: null,
    tool_calls: [{ id: callId, type: "function", function: { name, arguments: args } }],
  } as unknown as Msg;
}

/** 带 content 的 tool_call 消息：content 作为文本项并入当前开放轮（不打断合并） */
function toolCallWithContentMsg(content: string, callId: string, name = "read_file", args = '{"filePath":"a.ts"}'): Msg {
  return {
    role: "assistant",
    content,
    tool_calls: [{ id: callId, type: "function", function: { name, arguments: args } }],
  } as unknown as Msg;
}

function toolResultMsg(callId: string, content: string): Msg {
  return { role: "tool", tool_call_id: callId, content } as unknown as Msg;
}

function textMsg(content: string): Msg {
  return { role: "assistant", content } as Msg;
}

/** 便捷驱动：传入缓存与新消息数组，返回 {缓存, 轮次} */
function step(cache: TurnsCache | null, messages: Msg[]) {
  return buildTurnsCached(cache, messages);
}

// ---------------------------------------------------------------- 基础分组

describe("turnsBuilder 基础分组（与原 buildTurns 行为一致）", () => {
  it("user / assistant-with-tools / tool / assistant 分组", () => {
    const messages = [
      userMsg("帮我读取"),
      toolCallMsg("c1"),
      toolResultMsg("c1", "[File OK] 10 行"),
      textMsg("读取完成"),
    ];
    const { turns } = step(null, messages);
    expect(turns).toHaveLength(3);
    expect(turns[0].user?.content).toBe("帮我读取");
    expect(turns[1].items).toHaveLength(1); // tool call + 回填
    const tool = turns[1].items[0];
    expect(tool.type).toBe("tool");
    if (tool.type === "tool") {
      expect(tool.card.result).toBe("[File OK] 10 行");
      expect(tool.card.status).toBe("done");
    }
    expect(turns[2].assistant?.content).toBe("读取完成");
  });

  it("异常/取消结果推导状态", () => {
    const messages = [
      userMsg("x"),
      toolCallMsg("c1"), toolResultMsg("c1", "[Execution Exception] boom"),
      toolCallMsg("c2"), toolResultMsg("c2", "[Tool Execution Cancelled] "),
    ];
    const { turns } = step(null, messages);
    const items = turns[1].items;
    expect(items[0].type === "tool" && items[0].card.status).toBe("error");
    expect(items[1].type === "tool" && items[1].card.status).toBe("cancelled");
  });

  it("连续 assistant-with-tools 合并进同一轮", () => {
    const messages = [
      userMsg("x"),
      toolCallMsg("c1"),
      toolCallWithContentMsg("中间说明", "c2"),
    ];
    const { turns } = step(null, messages);
    expect(turns).toHaveLength(2);
    expect(turns[1].items.map((i) => i.type)).toEqual(["tool", "text", "tool"]);
  });
});

// ---------------------------------------------------------------- 增量语义（P0 核心）

describe("turnsBuilder 增量缓存", () => {
  it("无新增消息：返回同一 turns 引用（StrictMode 双调用幂等）", () => {
    const messages = [userMsg("hi"), textMsg("hello")];
    const first = step(null, messages);
    const second = step(first.cache, messages);
    expect(second.turns).toBe(first.turns);
  });

  it("纯追加：历史 turn 引用复用，只有新 turn 是新对象", () => {
    const m1 = [userMsg("第一问"), textMsg("第一答")];
    const first = step(null, m1);
    const oldRefs = first.turns;

    const m2 = [...m1, userMsg("第二问"), textMsg("第二答")];
    const second = step(first.cache, m2);

    expect(second.turns).toHaveLength(4);
    // 历史轮引用不变 → TurnItem memo 跳过重渲染
    expect(second.turns[0]).toBe(oldRefs[0]);
    expect(second.turns[1]).toBe(oldRefs[1]);
    // 新轮是新建对象，内容正确
    expect(second.turns[2].user?.content).toBe("第二问");
    expect(second.turns[3].assistant?.content).toBe("第二答");
    // 旧返回数组未被变异（长度仍为 2）
    expect(oldRefs).toHaveLength(2);
  });

  it("跨批次 tool 回填：目标轮生成新对象（引用变化触发重渲），其余轮引用不变", () => {
    // 批次1：user + assistant(tool_call)，结果未到
    const batch1 = [userMsg("读取"), toolCallMsg("c1")];
    const s1 = step(null, batch1);
    const toolTurnBefore = s1.turns[1];
    expect(toolTurnBefore.items[0].type === "tool" && toolTurnBefore.items[0].card.result).toBeUndefined();

    // 批次2：tool 结果到达
    const batch2 = [...batch1, toolResultMsg("c1", "[File OK] done")];
    const s2 = step(s1.cache, batch2);

    // 历史轮引用不变
    expect(s2.turns[0]).toBe(s1.turns[0]);
    // 目标轮是新对象（不可变回填，原地改会让 memo 跳过导致 UI 不更新）
    expect(s2.turns[1]).not.toBe(toolTurnBefore);
    expect(s2.turns[1].items).not.toBe(toolTurnBefore.items);
    const tool = s2.turns[1].items[0];
    expect(tool.type === "tool" && tool.card.result).toBe("[File OK] done");
    expect(tool.type === "tool" && tool.card.status).toBe("done");
  });

  it("跨批次连续 assistant 合并：第二批 tool_call 延续开放轮（同 key、items 合并）", () => {
    const batch1 = [userMsg("x"), toolCallMsg("c1")];
    const s1 = step(null, batch1);

    const batch2 = [...batch1, toolCallWithContentMsg("继续", "c2")];
    const s2 = step(s1.cache, batch2);

    expect(s2.turns).toHaveLength(2);
    expect(s2.turns[1].key).toBe(s1.turns[1].key); // 延续同一轮
    expect(s2.turns[1].items.map((i) => i.type)).toEqual(["tool", "text", "tool"]);
    // 延续轮是新对象（引用变化，memo 重渲该轮）
    expect(s2.turns[1]).not.toBe(s1.turns[1]);
  });

  it("编辑历史消息（替换引用）：前缀指纹失败 → 全量重建，内容正确", () => {
    const m1 = [userMsg("原文"), textMsg("原答")];
    const s1 = step(null, m1);

    // 编辑第一条消息（不可变替换：新对象）
    const edited = [userMsg("改过的文"), ...m1.slice(1)];
    const s2 = step(s1.cache, edited);

    expect(s2.turns).toHaveLength(2);
    expect(s2.turns[0].user?.content).toBe("改过的文");
    // 全量重建：所有轮都是新对象（memo 优化在编辑场景失效，可接受）
    expect(s2.turns[0]).not.toBe(s1.turns[0]);
  });

  it("删除尾部再补同数量消息（长度相同）：指纹识别 → 全量重建", () => {
    const m1 = [userMsg("q1"), textMsg("a1"), userMsg("q2"), textMsg("a2")];
    const s1 = step(null, m1);

    // 删掉后 2 条换 2 条新消息：前 2 条保留原引用（前缀通过），第 3 条引用变化
    const m2 = [...m1.slice(0, 2), userMsg("q3"), textMsg("a3")];
    const s2 = step(s1.cache, m2);
    expect(s2.turns).toHaveLength(4);
    expect(s2.turns[2].user?.content).toBe("q3");
    // 前缀在第3条断裂 → 全量重建
    expect(s2.turns[0]).not.toBe(s1.turns[0]);
  });

  it("纯删除尾部（长度回退）：全量重建", () => {
    const m1 = [userMsg("q1"), textMsg("a1"), userMsg("q2")];
    const s1 = step(null, m1);
    const s2 = step(s1.cache, m1.slice(0, 2));
    expect(s2.turns).toHaveLength(2);
    expect(s2.turns[0]).not.toBe(s1.turns[0]); // 重建（游标回退）
  });

  it("孤儿 tool 结果（无对应 tool_call）：跳过不报错", () => {
    const messages = [userMsg("x"), toolResultMsg("ghost", "无人认领")];
    const { turns } = step(null, messages);
    expect(turns).toHaveLength(1); // 只剩 user 轮，孤儿结果被忽略
  });

  it("超长会话追加成本：1000 轮历史 + 追加 1 条，历史引用全部不变", () => {
    const big: Msg[] = [];
    for (let i = 0; i < 500; i++) {
      big.push(userMsg(`问题 ${i}`), textMsg(`回答 ${i}`));
    }
    const s1 = step(null, big);
    const refs = s1.turns.map((t) => t);

    const t0 = performance.now();
    const s2 = step(s1.cache, [...big, userMsg("新问题")]);
    const elapsed = performance.now() - t0;
    // 1000 个历史轮引用逐个不变（memo 全部跳过）
    for (let i = 0; i < refs.length; i++) {
      expect(s2.turns[i]).toBe(refs[i]);
    }
    expect(s2.turns).toHaveLength(1001);
    // 增量追加必须是 O(新增) 级别：500+ 轮历史下毫秒级
    expect(elapsed).toBeLessThan(5);
  });
});
