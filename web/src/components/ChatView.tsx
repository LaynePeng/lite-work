import { useMemo, useRef, useEffect, useState, useCallback } from "react";
import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { DiffPre, DiffStats, isFileDiff } from "./FileDiff";
import AppIcon from "./AppIcon";
import type { Msg, SubAgentProgress, ToolCardInfo, WorkItem } from "../types";

// ---------------------------------------------------------------- 渲染助手

const toolArgsOf = (m: Msg): { name: string; args: string }[] =>
  (m.tool_calls ?? []).map((tc) => ({
    name: tc.function.name,
    args: tc.function.arguments,
  }));

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
              <pre className="subagent-streaming-body">{sa.streaming_text}</pre>
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

interface RenderTurn {
  key: string;
  user?: Msg;
  items: WorkItem[];
  assistant?: Msg;
}

function parseToolArgs(raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

function buildTurns(messages: Msg[]): RenderTurn[] {
  const turns: RenderTurn[] = [];
  let current: RenderTurn | null = null;
  const pending = new Map<string, ToolCardInfo>();

  for (const m of messages) {
    if (m.role === "user") {
      current = null;
      turns.push({ key: `u-${turns.length}`, user: m, items: [] });
    } else if (m.role === "assistant") {
      const hasTools = (m.tool_calls ?? []).length > 0;
      if (!hasTools) {
        current = null;
        turns.push({ key: `a-${turns.length}`, items: [], assistant: m });
        continue;
      }
      if (!current) {
        current = { key: `t-${turns.length}`, items: [] };
        turns.push(current);
      }
      if (m.content) current.items.push({ type: "text", id: `text-${turns.length}-${current.items.length}`, content: m.content });
      for (const tc of m.tool_calls ?? []) {
        const card: ToolCardInfo = {
          id: tc.id || `h-${current.items.length}-${Math.random().toString(36).slice(2, 6)}`,
          name: tc.function.name,
          args: parseToolArgs(tc.function.arguments),
          status: "done",
        };
        pending.set(card.id, card);
        current.items.push({ type: "tool", id: card.id, card });
      }
    } else if (m.role === "tool") {
      const target = m.tool_call_id ? pending.get(m.tool_call_id) : undefined;
      if (!target) continue;
      target.result = m.content ?? "";
      const content = m.content ?? "";
      if (content.startsWith("[Tool Execution Cancelled]") || content.startsWith("[Blocked")) target.status = "cancelled";
      else if (content.startsWith("[Execution Exception]") || content.startsWith("[Error]")) target.status = "error";
    }
  }
  return turns;
}

// ---------------------------------------------------------------- 主组件

// 各 Agent 的空态欢迎语与场景入口（GAI 通用入口：办公/调研/代码一站式）
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

export default function ChatView({
  sessionId,
  sessionTitle,
  messages,
  streaming,
  running,
  turn,
  pendingApprovals,
  subAgentRecords,
  skillLoaded,
  pendingQueue,
  onSend,
  onStop,
  onApprove,
  currentAgent,
  onRemovePending,
  onReorderPending,
  onSendPending,
}: {
  sessionId: string;
  sessionTitle: string;
  messages: Msg[];
  streaming: { items: WorkItem[]; turn?: number } | null;
  running: boolean;
  turn: number;
  pendingApprovals: { id: string; action: string; reason: string }[];
  subAgentRecords: SubAgentProgress[];
  skillLoaded?: string[];
  pendingQueue: string[];
  onSend: (prompt: string) => void;
  onStop: () => void;
  onApprove: (approvalId: string, approved: boolean) => void;
  currentAgent: string;
  onRemovePending?: (index: number) => void;
  onReorderPending?: (from: number, to: number) => void;
  onSendPending?: (index: number) => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);

  // 拖拽排序状态
  const [dragIdx, setDragIdx] = useState<number | null>(null);
  const [dragOverIdx, setDragOverIdx] = useState<number | null>(null);

  const handleDragStart = useCallback((idx: number) => (e: React.DragEvent) => {
    setDragIdx(idx);
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", String(idx));
  }, []);

  const handleDragOver = useCallback((idx: number) => (e: React.DragEvent) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    setDragOverIdx(idx);
  }, []);

  const handleDragLeave = useCallback(() => {
    setDragOverIdx(null);
  }, []);

  const handleDrop = useCallback((idx: number) => (e: React.DragEvent) => {
    e.preventDefault();
    if (dragIdx !== null && dragIdx !== idx) {
      onReorderPending?.(dragIdx, idx);
    }
    setDragIdx(null);
    setDragOverIdx(null);
  }, [dragIdx, onReorderPending]);

  const handleDragEnd = useCallback(() => {
    setDragIdx(null);
    setDragOverIdx(null);
  }, []);

  const pendingQueueEl = pendingQueue.length > 0 ? (
    <div className="pending-queue">
      <div className="pending-queue-header">
        <span className="pending-queue-title">待发送队列（{pendingQueue.length}）</span>
      </div>
      {pendingQueue.map((item, idx) => (
        <div
          key={idx}
          className={`pending-queue-item ${dragOverIdx === idx ? "drag-over" : ""} ${dragIdx === idx ? "dragging" : ""}`}
          draggable
          onDragStart={handleDragStart(idx)}
          onDragOver={handleDragOver(idx)}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop(idx)}
          onDragEnd={handleDragEnd}
        >
          <span className="pending-queue-drag">⠿</span>
          <span className="pending-queue-text">{item}</span>
          <button className="pending-queue-send" onClick={() => onSendPending?.(idx)} title="立即发送">➤</button>
          <button className="pending-queue-remove" onClick={() => onRemovePending?.(idx)} title="移除">✕</button>
        </div>
      ))}
    </div>
  ) : null;

  const turns = useMemo(() => buildTurns(messages), [messages]);
  // 提问条状态已移至独立的 QuestionBar 组件

  // 用户手动上翻浏览历史时暂停自动跟随；滚回底部附近自动恢复
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onScroll = () => {
      stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    if (!stickRef.current) return;
    // 流式期间瞬时定位（smooth 动画追不上高频内容增长会产生抖动）
    bottomRef.current?.scrollIntoView({
      behavior: streaming ? "auto" : "smooth",
      block: "end",
    });
  }, [messages, streaming, turns.length]);

  return (
    <div className="chat-view">
      <div className="chat-scroll" ref={scrollRef}>
        {turns.length === 0 && !streaming ? (
          <EmptyState currentAgent={currentAgent} onSend={onSend} />
        ) : (
          <>
            <div className="session-badge">{sessionTitle}</div>
            {pendingQueueEl}
            {turns.map((t) => (
              <div key={t.key}>
                {t.user && <MessageBubble message={t.user} />}
                {t.items.length > 0 && (
                  <WorkItems items={t.items} />
                )}
                {t.assistant && t.assistant.content && (
                  <div className="msg-row assistant">
                    <div className="assistant-avatar"><AppIcon size={26} /></div>
                    <div className="bubble assistant-bubble">
                      <Markdown text={t.assistant.content} />
                    </div>
                  </div>
                )}
              </div>
            ))}
            {streaming && (
              <StreamingTurn items={streaming.items} turn={streaming.turn} />
            )}
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
          </>
        )}
        <div ref={bottomRef} />
      </div>

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
