// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { DiffPre, DiffStats, isFileDiff } from "./FileDiff";
import AppIcon from "./AppIcon";
import type { Msg, SubAgentProgress, ToolCardInfo, WorkItem } from "../types";
import { buildTurnsCached, type RenderTurn, type TurnsCache } from "../lib/turnsBuilder";

// ---------------------------------------------------------------- 渲染助手

function ToolIcon({ name }: { name: string }) {
  const emoji = name.startsWith("git")
    ? "𑁍"
    : name.includes("search") || name.includes("outline") || name.includes("focus")
      ? "◆"
      : name.includes("write") || name.includes("replace") || name.includes("diff") || name.includes("patch")
        ? "✎"
        : name === "execute_command"
          ? "⚡"
          : name === "spawn_sub_agent"
            ? "◈"
            : name === "webfetch" || name === "webfetch_batch"
              ? "➤"
              : "☰";
  return <span className="tool-icon">{emoji}</span>;
}

function Markdown({ text }: { text: string }) {
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]} rehypePlugins={[rehypeHighlight]}>
        {text}
      </ReactMarkdown>
    </div>
  );
}

// ---------------------------------------------------------------- 工具卡片

function ToolCard({ card }: { card: ToolCardInfo }) {
  const important = card.name === "execute_command" || card.name.startsWith("mcp_") || isFileDiff(card.result ?? "") || card.status === "error" || card.status === "cancelled";
  // 只有 diff / 异常默认展开；普通调用保持为紧凑的一行
  const [open, setOpen] = useState(() => important);

  useEffect(() => {
    if (isFileDiff(card.result ?? "") || card.status === "error" || card.status === "cancelled") setOpen(true);
  }, [card.result, card.status]);

  const statusClass = card.status === "running" ? "running" : card.status;
  const argsPreview = typeof card.args === "string"
    ? card.args
    : JSON.stringify(card.args) ?? "";

  return (
    <div className={`tool-card ${statusClass} ${important ? "tool-card-important" : "tool-card-compact"}`}>
      <button className="tool-card-header" onClick={() => setOpen(!open)}>
        <ToolIcon name={card.name} />
        <span className="tool-name">{card.name}</span>
        <span className="tool-args-preview">{argsPreview.slice(0, 120)}</span>
        {card.status === "running" && <span className="tool-status running">运行中…</span>}
        {card.status === "done" && <span className="tool-status done">✓ {card.durationMs !== undefined ? `${card.durationMs}ms` : "完成"}</span>}
        {card.status === "cancelled" && <span className="tool-status cancelled">已取消</span>}
        {card.status === "error" && <span className="tool-status error">执行异常</span>}
        <span className="tool-chevron">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="tool-card-body">
          <div className="tool-block">
            <div className="tool-block-label">参数</div>
            <pre>{JSON.stringify(card.args, null, 2)}</pre>
          </div>
          {card.result && <div className="tool-block"><div className="tool-block-label">结果</div><DiffStats text={card.result} /><DiffPre text={card.result} /></div>}
        </div>
      )}
    </div>
  );
}

function ToolActivity({ tools }: { tools: ToolCardInfo[] }) {
  return (
    <div className="tool-activity" title={tools.map((tool) => tool.name).join(" · ")}>
      <span className="tool-activity-label">工具</span>
      {tools.slice(-8).map((tool) => (
        <span className={`tool-activity-item ${tool.status}`} key={tool.id}>
          <ToolIcon name={tool.name} />
          <span>{tool.name}</span>
          <span className="tool-activity-state">
            {tool.status === "running" ? "…" : tool.status === "done" ? "✓" : "!"}
          </span>
        </span>
      ))}
      {tools.length > 8 && <span className="tool-activity-more">+{tools.length - 8}</span>}
    </div>
  );
}

// ---------------------------------------------------------------- 子 Agent 活动卡

function SubAgentCard({ card }: { card: ToolCardInfo }) {
  const sa = card.subagent;
  const [open, setOpen] = useState(!sa || sa.status === "running");
  const [showSummary, setShowSummary] = useState(false);
  const running = card.status === "running" && (!sa || sa.status === "running");
  const roleLabel = sa?.role ?? "general";
  // 实时输出框贴底跟随：stickRef 只由真实用户滚动事件改变，
  // 流式追加内容后同步滚到底部（与主聊天流 ChatView 同一套策略）
  const streamRef = useRef<HTMLPreElement>(null);
  const streamStickRef = useRef(true);
  const handleStreamScroll = useCallback(() => {
    const el = streamRef.current;
    if (!el) return;
    streamStickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  }, []);
  useLayoutEffect(() => {
    const el = streamRef.current;
    if (el && streamStickRef.current) el.scrollTop = el.scrollHeight;
  }, [sa?.streaming_text, open]);
  return (
    <div className={`subagent-card ${running ? "running" : "finished"}`}>
      <button className="subagent-header" onClick={() => setOpen(!open)}>
        <span className="subagent-icon">◈</span>
        <span className="subagent-role">子 Agent · {roleLabel}</span>
        {running && <span className="subagent-spinner" />}
        <span className="subagent-status">
          {running
            ? `运行中${sa?.turn ? ` · 第 ${sa.turn} 轮` : ""}`
            : sa?.status === "error" ? "✗ 异常结束" : "✓ 已完成"}
          {sa?.tokens != null && !running ? ` · ${sa.tokens} tokens` : ""}
        </span>
        <span className="tool-chevron">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="subagent-body">
          {sa?.task && <div className="subagent-task" title={sa.task}>{sa.task}</div>}
          <div className="subagent-steps">
            {(sa?.steps ?? []).map((step, i) => (
              <div className={`subagent-step ${step.status}`} key={`${step.tool}-${i}`}>
                <span className="subagent-step-state">
                  {step.status === "running" ? "⋯" : step.status === "done" ? "✓" : "!"}
                </span>
                <span className="subagent-step-tool">{step.tool}</span>
                {step.brief && <span className="subagent-step-brief" title={step.brief}>{step.brief}</span>}
                {step.durationMs != null && <span className="subagent-step-dur">{step.durationMs}ms</span>}
              </div>
            ))}
            {running && (!sa || sa.steps.length === 0) && <div className="subagent-step running"><span className="subagent-step-state">⋯</span><span>正在启动…</span></div>}
          </div>
          {sa?.streaming_text && running && (
            <div className="subagent-streaming">
              <div className="subagent-streaming-label">实时输出</div>
              <pre className="subagent-streaming-body" ref={streamRef} onScroll={handleStreamScroll}>{sa.streaming_text}</pre>
            </div>
          )}
          {!running && sa?.summary && (
            <div className="subagent-summary">
              <button className="subagent-summary-toggle" onClick={() => setShowSummary(!showSummary)}>
                {showSummary ? "▾ 隐藏总结" : "▸ 查看总结"}
              </button>
              {showSummary && <div className="subagent-summary-body"><Markdown text={sa.summary} /></div>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function WorkItems({ items, streaming = false }: { items: WorkItem[]; streaming?: boolean }) {
  return (
    <div className="work-timeline">
      {items.map((item) => {
        if (item.type === "text") {
          return (
            <div className="msg-row assistant timeline-text" key={item.id}>
              <div className="assistant-avatar"><AppIcon size={26} /></div>
              <div className="bubble assistant-bubble">
                <Markdown text={item.content} />
                {streaming && item.id === items[items.length - 1]?.id && <span className="cursor"><span /></span>}
              </div>
            </div>
          );
        }
        if (item.type === "activity") {
          const important = item.tools.filter((tool) =>
            tool.name === "execute_command" || tool.name.startsWith("mcp_") || isFileDiff(tool.result ?? "") || tool.status === "error" || tool.status === "cancelled"
          );
          const compact = item.tools.filter((tool) => !important.includes(tool));
          return (
            <div className="work-item-group" key={item.id}>
              {compact.length > 0 && <ToolActivity tools={compact} />}
              {important.map((tool) => <ToolCard key={tool.id} card={tool} />)}
            </div>
          );
        }
        // spawn_sub_agent 独立卡：渲染子 Agent 活动面板
        if (item.card.name === "spawn_sub_agent") {
          return <SubAgentCard key={item.id} card={item.card} />;
        }
        return <ToolCard key={item.id} card={item.card} />;
      })}
    </div>
  );
}

// ---------------------------------------------------------------- 消息气泡

function MessageBubble({ message }: { message: Msg }) {
  if (message.role === "user") {
    return (
      <div className="msg-row user">
        <div className="bubble user-bubble">
          {message.queued && <span className="queued-badge" title="已提交，Agent 将在当前任务下一回合处理">已入队 ⏳</span>}
          <Markdown text={message.content ?? ""} />
        </div>
      </div>
    );
  }
  return (
    <div className="msg-row assistant">
      <div className="assistant-avatar"><AppIcon size={26} /></div>
      <div className="bubble assistant-bubble">
        {message.content && <Markdown text={message.content} />}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 正在生成

function StreamingTurn({ items, turn }: { items: WorkItem[]; turn?: number }) {
  return (
    <>
      {items.length === 0 && <div className="timeline-thinking"><span className="dot" /><span className="dot" /><span className="dot" /><span className="thinking-text">思考中…{turn ? ` (第 ${turn} 轮)` : ""}</span></div>}
      <WorkItems items={items} streaming />
    </>
  );
}

// ---------------------------------------------------------------- 历史消息分组

// buildTurns/RenderTurn 已抽出为 lib/turnsBuilder.ts 的增量构建器
// （P0 性能防御）：纯追加时历史 turn 对象引用复用 → memo 跳过重渲染；
// 跨批次 tool 回填以不可变替换生成新 turn 对象，仅该轮重渲；
// 前缀指纹校验失败（编辑/删除）时自动全量重建。

// ---------------------------------------------------------------- 轮次渲染（memo）

// 单轮渲染组件：props 只有 turn 对象本身。流式刷新（~80ms/次）时
// 历史 turns 引用不变（useMemo 缓存），memo 直接跳过重渲染，
// 只有正在增长的流式轮次重新渲染——长会话也不怕
const TurnItem = memo(function TurnItem({ turn }: { turn: RenderTurn }) {
  return (
    <div>
      {turn.key.startsWith("streaming-") ? (
        <StreamingTurn items={turn.items} turn={turn.streamTurn} />
      ) : (
        <>
          {turn.user && <MessageBubble message={turn.user} />}
          {turn.items.length > 0 && (
            <WorkItems items={turn.items} />
          )}
          {turn.assistant && turn.assistant.content && (
            <div className="msg-row assistant">
              <div className="assistant-avatar"><AppIcon size={26} /></div>
              <div className="bubble assistant-bubble">
                <Markdown text={turn.assistant.content} />
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
});

// ---------------------------------------------------------------- 主组件

// 各 Agent 的空态欢迎语与场景入口（AGI 通用入口：办公/调研/代码一站式）
const AGENT_WELCOME: Record<string, { title: string; sub: string; hints: [string, string][] }> = {
  build: {
    title: "lite-work",
    sub: "手写内核的 Code 开发 Agent，已就绪。",
    hints: [
      ["🔍 查看项目结构", "帮我查看这个项目的代码结构"],
      ["🕵️ 搜索 TODO 并分析", "帮我搜索代码里所有 TODO 标记并分析"],
      ["🧪 运行测试并总结", "运行测试并总结结果"],
      ["🔎 审查代码改动", "审查当前未提交的代码改动"],
    ],
  },
  office: {
    title: "lite-work · 办公助手",
    sub: "写文档、做表格、生成 PPT、数据分析，产出直接保存为文件。",
    hints: [
      ["📄 写周报", "帮我写一份本周周报，生成 Word 文档"],
      ["📊 做数据表格", "帮我把这些数据整理成 Excel 表格"],
      ["🎞️ 生成 PPT", "帮我做一份汇报 PPT，先给我大纲"],
      ["📈 数据分析", "帮我分析一组数据并生成图表"],
      ["📝 写正式文档", "帮我起草一份项目实施方案文档"],
      ["📝 会议纪要", "帮我整理这段会议记录，生成会议纪要"],
    ],
  },
  research: {
    title: "lite-work · 调研助手",
    sub: "联网查证、多来源交叉验证，输出带来源标注的调研报告。",
    hints: [
      ["🔎 快速查证", "帮我查证一个问题的最新权威说法，并注明来源"],
      ["📋 行业调研", "帮我调研某个行业的现状与趋势，生成调研报告"],
      ["⚖️ 对比分析", "帮我对比两个产品的差异，输出对比表格"],
      ["📄 生成报告", "把刚才的调研结果整理成 Word 报告"],
    ],
  },
  plan: {
    title: "lite-work · 规划模式",
    sub: "只读分析：探查代码库、设计实现方案，不改任何文件。",
    hints: [
      ["📋 制定实现计划", "分析需求并列出实现计划的 TODO 清单"],
      ["🏗️ 评估技术方案", "评估当前架构，给出改进建议"],
      ["🧭 阅读并讲解代码", "阅读核心模块并讲解执行流程"],
    ],
  },
};

function EmptyState({ currentAgent, onSend }: { currentAgent: string; onSend: (p: string) => void }) {
  const w = AGENT_WELCOME[currentAgent] ?? AGENT_WELCOME.build;
  return (
    <div className="empty-state">
      <div className="empty-logo"><AppIcon size={76} /></div>
      <h2>{w.title}</h2>
      <p>{w.sub}</p>
      <div className="empty-hints">
        {w.hints.map(([label, prompt]) => (
          <button key={label} onClick={() => onSend(prompt)}>{label}</button>
        ))}
      </div>
    </div>
  );
}

/** 贴底判定阈值（px）：距底部小于该值视为"在底部" */
const STICK_THRESHOLD_PX = 48;

/** 超长会话折叠（P1）：超过该轮次开始折叠中间历史（数据不删，仅 UI 折叠） */
const FOLD_THRESHOLD = 500;
/** 折叠的消息数阈值：高工具密度会话里 1 轮可含几十张工具卡片，只按轮数
 *  阈值会形同虚设（实测 973 条消息仅折算 71 轮），故补充消息数维度（v1.6.0） */
const FOLD_MESSAGES = 600;
/** 折叠时始终渲染的最近轮次数 */
const KEEP_RECENT = 300;
/** 点击占位条每次展开的轮次数 */
const EXPAND_STEP = 300;

export default function ChatView({
  sessionId,
  sessionTitle,
  messages,
  streaming,
  running,
  turn,
  goal,
  loop,
  pendingApprovals,
  subAgentRecords,
  skillLoaded,
  onSend,
  onStop,
  onApprove,
  currentAgent,
  foldTurns = FOLD_THRESHOLD,
  foldMessages = FOLD_MESSAGES,
}: {
  sessionId: string;
  sessionTitle: string;
  messages: Msg[];
  streaming: { items: WorkItem[]; turn?: number } | null;
  running: boolean;
  turn: number;
  /** 会话目标（/goal）：非空时展示目标横幅 */
  goal?: string | null;
  /** 目标循环（/loop）运行状态：null=未开启 */
  loop: { count: number; max: number } | null;
  pendingApprovals: { id: string; action: string; reason: string }[];
  subAgentRecords: SubAgentProgress[];
  skillLoaded?: string[];
  onSend: (prompt: string) => void;
  onStop: () => void;
  onApprove: (approvalId: string, approved: boolean) => void;
  currentAgent: string;
  /** 展示折叠阈值（轮数）——可在设置中配置 */
  foldTurns?: number;
  /** 展示折叠阈值（消息条数）：高工具密度会话里 1 轮可含几十张工具卡片，
   *  只按轮数阈值会形同虚设，故补充消息数维度（v1.6.0） */
  foldMessages?: number;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);
  const prevUserSeqRef = useRef(0);
  const turnsCacheRef = useRef<TurnsCache | null>(null);

  // 增量构建：纯追加只构建新 turn、历史对象引用复用（memo 生效）；
  // 无新增时返回同一引用（StrictMode 双调用安全）；
  // 前缀指纹校验失败（编辑/删除）自动全量重建
  const turns = useMemo(() => {
    const built = buildTurnsCached(turnsCacheRef.current, messages);
    turnsCacheRef.current = built.cache;
    return built.turns;
  }, [messages]);

  // 流式内容并入末尾轮次：与历史轮次同一渲染通道，滚动逻辑统一处理
  const displayTurns = useMemo<RenderTurn[]>(() => {
    if (!streaming) return turns;
    return [...turns, {
      key: `streaming-${streaming.turn ?? 0}`,
      items: streaming.items,
      streamTurn: streaming.turn,
    }];
  }, [turns, streaming]);

  // ------------------------------------------------------------ 超长会话折叠（P1）
  // 数据不删，仅 UI 折叠：超过 FOLD_THRESHOLD 轮时，只渲染最近
  // KEEP_RECENT(+expanded) 轮，更早的折叠为占位条（点击 +EXPAND_STEP）。
  // 折叠发生在追加消息跨过阈值的时刻，贴底逻辑（scrollTop = scrollHeight）
  // 自动适配变小的总高度；用户上翻浏览的多在保留区内，不受打扰。
  const [foldExpanded, setFoldExpanded] = useState(0);
  useEffect(() => { setFoldExpanded(0); }, [sessionId]); // 切换会话重置展开状态
  const totalTurns = displayTurns.length;
  // 折叠条件：轮数或消息数任一超限（v1.6.0 起支持消息数维度，可在设置中配置）。
  // 高工具密度会话里 1 轮可含几十张工具卡片：973 条消息只折算出 71 轮，
  // 只按轮数阈值会形同虚设——因此消息数超限时按「每轮消息权重」保留尾部。
  const messagesOver = foldMessages > 0 && messages.length > foldMessages;
  const foldFrom = totalTurns > foldTurns
    // 轮数超限：维持原有口径（保留最近 KEEP_RECENT+expanded 轮）
    ? Math.max(0, totalTurns - KEEP_RECENT - foldExpanded)
    : messagesOver
      ? (() => {
          // 消息数超限：从尾部累计「每轮权重」（1 + 工具卡片×2 ≈ 该轮的消息数），
          // 保留约 40% 阈值量的消息，其余折叠；点击占位条仍可逐级展开
          const keepWeight = Math.max(50, Math.floor(foldMessages * 0.4)) + foldExpanded * 2;
          let acc = 0;
          let idx = 0;
          for (let i = displayTurns.length - 1; i >= 0; i--) {
            const weight = 1 + (displayTurns[i].items?.length ?? 0) * 2;
            if (acc + weight > keepWeight) { idx = i + 1; break; }
            acc += weight;
            idx = i;
          }
          return idx;
        })()
      : 0;
  const renderTurns = foldFrom > 0 ? displayTurns.slice(foldFrom) : displayTurns;

  // 贴底状态只由真实用户滚动事件改变。此前用 Virtuoso 的
  // atBottomStateChange 时，流式内容快速长高会让"距底部距离"瞬间
  // 超过阈值而被误判为用户上翻 → stickRef 永久 false → 跟随断开。
  // 这是"随着对话变多就不跟随"的直接根因之一。
  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < STICK_THRESHOLD_PX;
  }, []);

  // 用户新发消息（含运行中排队的补充指令）时强制回到底部：
  // 上翻浏览历史时发送新内容，也应立即跟随到最新
  const lastMsg = messages[messages.length - 1];
  const userSeq = lastMsg?.role === "user" ? messages.length : 0;
  if (userSeq > prevUserSeqRef.current) stickRef.current = true;
  prevUserSeqRef.current = userSeq;

  // 内容变化后同步贴底。useLayoutEffect 在 DOM 更新后、浏览器绘制前
  // 同步执行：读取 scrollHeight 触发同步 layout，随后设置 scrollTop，
  // 一次绘制内完成——没有虚拟列表"异步测量 vs 滚动"的竞态，
  // 流式气泡持续增高也能逐帧稳定跟随（这是弃用 Virtuoso 的原因）。
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  }, [messages, streaming, displayTurns]);

  return (
    <div className="chat-view">
      {turns.length === 0 && !streaming ? (
        <div className="chat-scroll">
          <EmptyState currentAgent={currentAgent} onSend={onSend} />
        </div>
      ) : (
        <div className="chat-scroll" ref={scrollRef} onScroll={handleScroll}>
          <div className="session-badge">{sessionTitle}</div>
          {goal && (
            <div className="goal-banner" title={goal}>
              <span className="goal-icon">🎯</span>
              <span className="goal-text">{goal}</span>
              {loop && (
                <span className="goal-loop">
                  🔁 自动推进 第 {Math.max(1, loop.count)}/{loop.max} 轮
                </span>
              )}
            </div>
          )}
          {foldFrom > 0 && (
            <button
              className="folded-divider"
              onClick={() => setFoldExpanded((e) => e + EXPAND_STEP)}
              title="展开更早的对话（每次 300 轮）"
            >
              ▲ 查看更早的 {foldFrom} 轮对话
            </button>
          )}
          {renderTurns.map((t) => (
            <TurnItem key={t.key} turn={t} />
          ))}
          {skillLoaded && skillLoaded.length > 0 && (
            <div className="skill-loaded-hint">📦 已注入技能：{skillLoaded.join("、")}</div>
          )}
          {subAgentRecords.length > 0 && (
            <div className="subagent-records">
              {subAgentRecords.map((r, i) => (
                <div className="subagent-record" key={`${r.subagentId}-${i}`}>
                  <span className="subagent-record-role">◈ {r.role}</span>
                  <span className={r.status === "error" ? "rec-error" : "rec-done"}>
                    {r.status === "error" ? "✗ 异常" : "✓ 完成"}
                  </span>
                  {r.tokens != null && <span className="rec-tokens">{r.tokens} tokens</span>}
                  <span className="subagent-record-task" title={r.task}>{r.task}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {pendingApprovals.map((pa) => (
        <div className="approval-overlay" key={pa.id}>
          <div className="approval-card">
            <div className="approval-icon">🛡️</div>
            <h3>需要你的确认{pendingApprovals.length > 1 ? `（${pendingApprovals.length} 个待审批）` : ""}</h3>
            <p className="approval-action">{pa.action}</p>
            <p className="approval-reason">{pa.reason}</p>
            <div className="approval-buttons">
              <button className="btn-deny" onClick={() => onApprove(pa.id, false)}>
                拒绝
              </button>
              <button className="btn-approve" onClick={() => onApprove(pa.id, true)}>
                允许执行
              </button>
            </div>
          </div>
        </div>
      ))}

      </div>
  );
}

// ---------------------------------------------------------------- 非阻塞提问条

export function QuestionBar({
  pendingQuestions,
  onAnswerQuestion,
}: {
  pendingQuestions: { id: string; question: string; options: string[] }[];
  onAnswerQuestion: (questionId: string, answer: string) => void;
}) {
  const [activeQuestionIdx, setActiveQuestionIdx] = useState(0);
  const [customAnswer, setCustomAnswer] = useState("");

  if (pendingQuestions.length === 0) return null;

  const active = pendingQuestions[activeQuestionIdx] ?? pendingQuestions[0];

  return (
    <div className="question-bar">
      <div className="question-bar-header">
        <span className="question-bar-icon">❓</span>
        <span>Agent 需要你的回答</span>
      </div>
      {pendingQuestions.length > 1 && (
        <div className="question-tabs">
          {pendingQuestions.map((q, i) => (
            <button
              key={q.id}
              className={`question-tab ${i === activeQuestionIdx ? "active" : ""}`}
              onClick={() => { setActiveQuestionIdx(i); setCustomAnswer(""); }}
            >
              问题 {i + 1}
            </button>
          ))}
        </div>
      )}
      <div className="question-card">
        <p className="question-text">{active.question}</p>
        {active.options.length > 0 && (
          <div className="question-options">
            {active.options.map((opt, i) => (
              <button
                key={i}
                className="btn-option"
                onClick={() => onAnswerQuestion(active.id, opt)}
              >
                {opt}
              </button>
            ))}
          </div>
        )}
        <div className="question-custom-row">
          <input
            className="form-input"
            placeholder="输入自定义回答…"
            value={customAnswer}
            onChange={(e) => setCustomAnswer(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && customAnswer.trim()) {
                onAnswerQuestion(active.id, customAnswer.trim());
                setCustomAnswer("");
              }
            }}
          />
          <button
            className="btn-approve"
            disabled={!customAnswer.trim()}
            onClick={() => {
              if (customAnswer.trim()) {
                onAnswerQuestion(active.id, customAnswer.trim());
                setCustomAnswer("");
              }
            }}
          >
            提交回答
          </button>
        </div>
      </div>
    </div>
  );
}

// 导出供 App 使用
export { ToolCard, Markdown };
export type { RenderTurn };
