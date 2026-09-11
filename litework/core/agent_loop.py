# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""AgentLoop 主循环状态机（对应课程第16课（实战 AgentLoop）+ 第2/3课全部增强）。

完整 Think-Act-Observe 闭环：
LLM 调用(带动态 System Prompt / Token 预算裁剪 / beforeLLM 管道)
  → 解析 tool_calls（JSON 自愈 / 死循环检测 / beforeTool 安全管道 / 审批）
  → 执行工具（超时 / 输出截断 / afterTool 管道）
  → 结果回填消息链 → 会话落盘 → 回到 LLM 调用
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import time
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from .context_manager import ContextManager, repair_tool_call_pairs
from .json_repair import safe_json_parse
from .kernel import Kernel
from .session_store import SessionStore
from .state_tracker import AgentStateTracker, AgentStatus
from .system_prompt import SystemPromptBuilder
from .token_counter import TokenCounter
from .truncator import truncate_tool_output
from .types import Message, ToolCall, ToolDefinition, header_context
from ..tools.todos import current_session_id, current_root_session_id

logger = logging.getLogger("litework.agentloop")

# 当前执行的工具调用 ID（跨层传递给 spawn_sub_agent 等需要关联事件的工具；
# asyncio 同一 task 内 ContextVar 可靠传播，并行协程各自独立 context）
current_tool_call: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_tool_call", default=None
)

# 写类工具：auto 模式下写类工具串行执行、只读工具并行执行（顺序依赖风险，如 write A + read A）
WRITE_TOOLS = frozenset({
    "write_file", "apply_search_replace", "apply_unified_diff",
    "execute_command", "git_commit", "git_push",
})


def cache_price_of(pricing: Dict[str, float]) -> float:
    """缓存命中单价：显式配置优先，否则按 input 的 10% 折算（0.1x 惯例）。"""
    configured = pricing.get("cache_hit_per_mtok")
    if configured is None:
        configured = float(pricing.get("input_per_mtok", 0) or 0) * 0.1
    return float(configured or 0)


def pricing_payload(pricing: Optional[Dict[str, float]]) -> Dict[str, float]:
    """面板/接口统一口径的计费单价（每 M token，美元）。

    成本估算完全由这三个单价决定，因此随统计一起下发，供前端直接展示对账。
    """
    p = pricing or {}
    return {
        "input_per_mtok": float(p.get("input_per_mtok", 0) or 0),
        "output_per_mtok": float(p.get("output_per_mtok", 0) or 0),
        "cache_hit_per_mtok": cache_price_of(p),
    }


class AgentLoop:
    def __init__(
        self,
        kernel: Kernel,
        adapter,
        registry,
        session_store: Optional[SessionStore] = None,
        context_manager: Optional[ContextManager] = None,
        max_steps: int = 100,
        tool_timeout: float = 120.0,
        llm_timeout: float = 180.0,
        llm_retries: int = 2,
        token_budget: int = 48000,
        pricing: Optional[Dict[str, float]] = None,
        auto_approve: bool = False,
        context_window: Optional[int] = None,
    ) -> None:
        self.kernel = kernel
        self.adapter = adapter
        self.registry = registry
        self.session_store = session_store
        self.context_manager = context_manager or ContextManager(token_budget)
        self.max_steps = max_steps
        self.tool_timeout = tool_timeout
        self.llm_timeout = llm_timeout
        # LLM 瞬时故障（超时/网络/限流/5xx）的自动重试次数
        self.llm_retries = max(0, int(llm_retries))
        self.auto_approve = auto_approve
        self.pricing = pricing or {"input_per_mtok": 2.0, "output_per_mtok": 8.0}
        self.context_window = context_window or 128_000
        self.state = AgentStateTracker()
        self.abort_event: Optional[asyncio.Event] = None
        self.workspace: str = "."
        # 截断落盘目录（第4/5课：超限工具输出保存到磁盘，上下文只放句柄）
        self.truncation_dir: Optional[str] = None
        # 上下文压缩/缓存统计（供「上下文情况」面板）
        self._compression_count = 0
        self._compressed_tokens = 0
        self._last_usage: Optional[Dict[str, int]] = None
        # 最近一次 LLM 调用的用量（「本次调用」口径，与 stats 里的任务累计区分）
        self._last_call: Optional[Dict[str, int]] = None
        # 无 usage 时每轮估算的 prompt 规模（用于「本次调用」的兜底口径）
        self._last_prompt_estimate: int = 0
        # 并行工具执行模式："auto"（只读轮并行/含写串行）| "always" | "never"
        self.parallel_tool_calls: str = "auto"
        # 任务运行期间用户补充的输入队列（TaskHandle 持有同一个 deque，跨回合注入）
        self.injected_inputs = deque()
        # agent 间消息队列（P2 合作）：其他 agent 经 send_message 发来的消息，
        # turn 边界注入本 agent 上下文（与用户补充指令同一合法注入点）
        self.agent_inbox: deque = deque()

    def request_stop(self) -> None:
        if self.abort_event:
            self.abort_event.set()
        self.state.status = AgentStatus.STOPPED

    def _check_abort(self) -> bool:
        return bool(self.abort_event and self.abort_event.is_set())

    def _build_header_context(self) -> Dict[str, str]:
        """构造 custom_headers 模板展开所需上下文（每任务开始时调用一次）。

        conversation_id 仅在供应商 custom_headers 配置了 {conversation_id} 模板时
        惰性生成（经 session_store 按 (会话 × 供应商) 落盘复用），
        避免给所有任务额外写会话元数据。
        """
        provider = getattr(self.adapter, "provider_id", "") or ""
        ctx: Dict[str, str] = {
            "session_id": self.kernel.session_id,
            "workspace": self.workspace or "",
            "model": getattr(self.adapter, "model", "") or "",
            "provider": provider,
        }
        wants_conversation = any(
            "{conversation_id}" in (v or "")
            for v in getattr(self.adapter, "custom_headers", {}).values()
        )
        if wants_conversation and self.session_store is not None:
            ctx["conversation_id"] = self.session_store.get_or_create_conversation_id(
                self.kernel.session_id, provider
            )
        return ctx

    # ------------------------------------------------------------------ 主循环

    class _LLMCallFailure(Exception):
        """LLM 调用终局失败（重试耗尽或不可重试），由 run_task 捕获后收尾。"""

        def __init__(self, message: str) -> None:
            super().__init__(message)
            self.message = message

    async def run_task(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        tools: Optional[List[ToolDefinition]] = None,
        store_snapshot: bool = True,
    ) -> Tuple[str, Dict[str, Any]]:
        """Think-Act-Observe 主循环。"""
        messages: List[Message] = self.kernel.ctx.messages
        tools = tools if tools is not None else self.registry.get_tools()
        # 工具处理器（todo_write 等）经 ContextVar 知道当前会话
        current_session_id.set(self.kernel.session_id)
        # 子 Agent 的 root_session_id 指向主会话（sub_agent 装配），todo_write
        # 以此为目标合并进主会话看板；主 Agent 时回退自身会话 id。
        current_root_session_id.set(
            getattr(self.kernel, "root_session_id", None) or self.kernel.session_id
        )
        # custom_headers 模板展开上下文（适配器 _headers() 读取，见 llm/base.py）
        header_context.set(self._build_header_context())
        self.state = AgentStateTracker()
        self.state.status = AgentStatus.RUNNING

        stats: Dict[str, Any] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "tool_calls": 0,
            "turns": 0,
            "blocked": 0,
            "cache_hit_tokens": 0,
            "cache_miss_tokens": 0,
        }

        # 阶段〇：初始化（system prompt、用户消息入链、首次落盘、task:start）
        await self._initialize_task(messages, prompt, system_prompt, tools, store_snapshot)

        current_step = 0
        empty_reply_retries = 0
        try:
            while current_step < self.max_steps:
                current_step += 1
                stats["turns"] = current_step
                await self.kernel.events.emit("llm:turn_start", {"turn": current_step})

                if self._check_abort():
                    return await self._finish("[Stopped]: 已由用户手动停止。", messages, stats, store_snapshot)

                # A-. 注入任务运行期间用户补充的指令（排队输入在下一回合进入对话）
                await self._inject_queued(messages)

                # A-.2 子 Agent 完成通知（完成即通知：turn 边界投递，本轮 LLM 即可见）
                await self._inject_agent_notifications(messages)

                # A. 上下文裁剪（保护 system 与 assistant/tool 原子对）
                payload = await self._trim_context(messages, stats)

                # B. beforeLLM 管道（插件可修改消息）
                processed = await self.kernel.before_llm.run(self.kernel.ctx, payload)
                # B2. 兜底修复：确保发给 LLM 的消息链满足原子对约束（压缩/裁剪兜底）
                processed = repair_tool_call_pairs(processed)
                # 每轮估算本轮 prompt 规模，供「本次调用」在无 usage 时兜底展示/计费。
                # 注意：这里不再把估算值累进 stats——输入是否计入由 _record_usage 按
                # 「该轮最终有无 usage」决定，否则首轮会「估算 + 真实 usage」双计
                # （实测 3 轮 × 10k 的任务被报成 71k），而「第 1 轮有 usage、后续没有」
                # 时又会整轮漏计。
                self._last_prompt_estimate = TokenCounter.count_messages_tokens(processed)

                # 阶段一：LLM 调用（流式，内部 emit llm:stream；瞬时故障指数退避重试）
                try:
                    content, tool_calls, usage = await self._call_llm_with_retry(processed, tools)
                except AgentLoop._LLMCallFailure as failure:
                    logger.warning("[AgentLoop] LLM 调用失败: %s", failure.message)
                    messages.append(Message(role="assistant", content=f"[LLM Error]: {failure.message}"))
                    return await self._finish(f"[LLM Error]: {failure.message}", messages, stats, store_snapshot)

                # 阶段三：状态更新（usage 累加 + 上下文水位推送）
                self._record_usage(usage, content, stats)
                await self._emit_context_stats(stats)

                if not content and not tool_calls:
                    empty_reply_retries += 1
                    if empty_reply_retries <= 2:
                        messages.append(Message(
                            role="user",
                            content="模型返回了空响应。请继续当前任务，并明确给出下一步工具调用或最终结果。",
                        ))
                        continue
                    self.state.status = AgentStatus.FAILED_MAX_TURNS
                    return await self._finish(
                        "[LLM Error]: 模型连续返回空响应，任务已终止。",
                        messages, stats, store_snapshot,
                    )
                empty_reply_retries = 0

                # E. Assistant 消息入链
                assistant_message = Message(
                    role="assistant",
                    content=content or None,
                    tool_calls=tool_calls if tool_calls else None,
                )
                messages.append(assistant_message)
                await self.kernel.events.emit("message:added", {"message": assistant_message.to_dict()})

                # F. 无工具调用 → 任务收敛，输出最终文本
                if not tool_calls:
                    self.state.status = AgentStatus.SUCCESS
                    return await self._finish(content or "(空回复)", messages, stats, store_snapshot)

                # 阶段二：工具批次执行（精细并行化：写类串行、只读并行；结果按原序回填）。
                #    批次执行期间到达的排队输入：中断剩余工具（占位结果保持
                #    assistant tool_calls ↔ tool 结果的原子对），全部结果回填后
                #    再统一注入——user 消息不能插在工具调用与结果之间。
                results = await self._execute_tool_batch(tool_calls, stats)
                if results is None:  # 批次中途被停止
                    return await self._finish("[Stopped]: 已由用户手动停止。", messages, stats, store_snapshot)
                if self._check_abort():
                    return await self._finish("[Stopped]: 已由用户手动停止。", messages, stats, store_snapshot)

                await self._append_tool_results(tool_calls, results, messages)

                # G2. 工具结果回填完成后注入排队输入（消息链合法位置），
                #     下一轮 LLM 调用立即可见（OpenCode system-reminder 模式）
                await self._inject_queued(messages)

                # H. 每轮批量执行完成后落盘，防中途崩溃丢状态
                if store_snapshot:
                    self._save_session()

                await self.kernel.events.emit("stats:update", self._stats_payload(stats))

            self.state.status = AgentStatus.FAILED_MAX_TURNS
            return await self._finish(
                "[Loop Terminated]: 超出最大步骤限制仍未得出最终结论。",
                messages, stats, store_snapshot,
            )
        except asyncio.CancelledError:
            self.state.status = AgentStatus.STOPPED
            self._save_session()
            raise
        except Exception:
            logger.exception("[AgentLoop] 未捕获异常")
            self._save_session()
            raise

    # ------------------------------------------------------------------ 阶段实现

    async def _initialize_task(
        self, messages: List[Message], prompt: str, system_prompt: Optional[str],
        tools: List[ToolDefinition], store_snapshot: bool,
    ) -> None:
        """任务初始化：System Prompt 装配、历史修复、用户消息入链、首次落盘。"""
        # 1. System Prompt 初始化（每任务一次：静态骨架，保证缓存前缀稳定）
        if system_prompt is None:
            system_prompt = SystemPromptBuilder.build(
                self.workspace, tools, skill_index=self._filtered_skill_index())
        if not messages or messages[0].role != "system":
            messages.insert(0, Message(role="system", content=system_prompt))
        else:
            messages[0].content = system_prompt

        # 1.5 修复历史中可能不完整的工具调用链（任务被停止时落盘的不完整历史）
        messages[:] = repair_tool_call_pairs(messages)

        # 2. 用户消息入链
        user_message = Message(role="user", content=prompt)
        messages.append(user_message)
        await self.kernel.events.emit("message:added", {"message": user_message.to_dict()})

        # 2.5 上一任务遗留的子 Agent 完成通知先行注入（父已结束场景，
        #     通知滞留 manager 队列，本任务首轮 LLM 调用前投递）
        await self._inject_agent_notifications(messages)

        # 首条消息立即落盘，避免 session 创建后、首轮 LLM 完成前刷新列表时消失。
        if store_snapshot:
            self._save_session()

        await self.kernel.events.emit("task:start", {"session_id": self.kernel.session_id})

    async def _trim_context(self, messages: List[Message], stats: Dict[str, Any]) -> List[Message]:
        """上下文裁剪：有效上限 = max(预算下限, 90% × 模型窗口)，到阈值先尝试 LLM 摘要压缩。"""
        cap = self._effective_cap()
        if TokenCounter.count_messages_tokens(messages) <= cap:
            return messages
        compacted = await self._try_compact(messages, cap)
        if compacted is not None:
            messages[:] = compacted
            return messages
        payload = self.context_manager.prune_messages(messages, hard_cap=cap)
        if self.context_manager.last_prune.get("compressed"):
            self._compression_count += 1
            self._compressed_tokens += int(
                self.context_manager.last_prune.get("removed_tokens", 0)
            )
        return payload

    async def _call_llm_with_retry(
        self, processed: List[Message], tools: List[ToolDefinition],
    ) -> Tuple[str, List[ToolCall], Optional[Dict[str, int]]]:
        """阶段一：调用 LLM（流式，内部 emit llm:stream）。

        瞬时故障（单次超时 / 网络抖动 / 限流 / 5xx）自动指数退避重试，
        避免长思考模型或网络波动直接杀死整个任务；重试过程 emit
        "llm:retry" 事件供 UI 展示。不可重试错误（鉴权/参数等）立即失败，
        抛出 _LLMCallFailure 由 run_task 统一收尾。
        """
        attempt = 0
        while True:
            try:
                return await asyncio.wait_for(
                    self.adapter.chat_stream(processed, tools, self.kernel.events),
                    timeout=self.llm_timeout,
                )
            except asyncio.TimeoutError:
                last_err: BaseException = TimeoutError(
                    f"LLM 请求超过 {self.llm_timeout}s（模型可能正在长时间思考或网络拥堵）"
                )
                retryable = True
            except Exception as exc:
                last_err = exc
                # LLMError 携带 retryable 标记（超时/网络/限流/5xx 为 True）
                retryable = bool(getattr(exc, "retryable", False))
            if not retryable or attempt >= self.llm_retries:
                raise AgentLoop._LLMCallFailure(str(last_err))
            attempt += 1
            wait_s = min(2 ** attempt, 8)
            logger.warning(
                "[AgentLoop] LLM 调用失败（%s），%ds 后进行第 %d/%d 次重试",
                last_err, wait_s, attempt, self.llm_retries,
            )
            await self.kernel.events.emit("llm:retry", {
                "attempt": attempt,
                "max_retries": self.llm_retries,
                "reason": str(last_err)[:200],
                "wait": wait_s,
            })
            await asyncio.sleep(wait_s)

    def _record_usage(
        self, usage: Optional[Dict[str, int]], content: Optional[str], stats: Dict[str, Any],
    ) -> None:
        """阶段三（a）：用模型返回的 usage 累加（准确值），无 usage 时回退估算。

        stats 是「本任务累计」（每轮把整段上下文重发，逐轮线性累加），
        而 self._last_call 记录「最近一次调用」的用量——两者口径不同，
        面板必须分开显示，否则单轮 1 万 token 的任务在几十轮后看起来像上百万。
        """
        self._last_usage = usage or self._last_usage
        if usage:
            prompt = usage.get("prompt_tokens", 0)
            output = usage.get("completion_tokens", 0)
            hit = usage.get("prompt_cache_hit_tokens", 0)
            stats["input_tokens"] += prompt
            stats["output_tokens"] += output
            stats["cache_hit_tokens"] += hit
            if getattr(self.adapter, "name", "") == "anthropic":
                # Anthropic: input_tokens 不含 cache_read，miss = input_tokens
                miss = prompt
            else:
                # OpenAI 兼容（DeepSeek 等）: prompt_tokens 已含命中部分
                miss = max(0, prompt - hit)
            stats["cache_miss_tokens"] += miss
            self._last_call = {
                "prompt_tokens": prompt, "output_tokens": output,
                "cache_hit_tokens": hit, "cache_miss_tokens": miss,
            }
        else:
            output = TokenCounter.count_text_tokens(content or "")
            stats["output_tokens"] += output
            # 无 usage：本轮的 prompt 规模用调用前的估算值（而非任务累计值），
            # 且只有在这条分支里才把输入计入统计（有 usage 时由上面按真实值计）
            estimate = self._last_prompt_estimate
            stats["input_tokens"] += estimate
            self._last_call = {
                "prompt_tokens": estimate, "output_tokens": output,
                "cache_hit_tokens": 0, "cache_miss_tokens": estimate,
            }

    async def _execute_tool_batch(
        self, tool_calls: List[ToolCall], stats: Dict[str, Any],
    ) -> Optional[List[str]]:
        """阶段二：派发执行一批工具调用，返回与 tool_calls 同序的结果文本。

        - 写类工具串行执行（每步可中止、可被排队输入中断）；
        - 只读工具按 parallel_tool_calls 模式并行；
        - 中断/跳过的未执行项填 [Interrupted] 占位（保持消息链原子对）；
        - 批次中途检测到停止信号返回 None（由 run_task 统一收尾）。
        """
        INTERRUPTED = "[Interrupted]: 用户插入了新指令，本批剩余工具未执行。"
        interrupted_by_input = False
        if self.parallel_tool_calls == "never" or len(tool_calls) <= 1:
            # 全串行：逐个执行，每步可中止；每步后窥探排队输入
            results = []
            for call in tool_calls:
                if self._check_abort():
                    return None
                if interrupted_by_input:
                    results.append(INTERRUPTED)
                    continue
                results.append(await self._execute_tool_call(call, stats))
                if self.injected_inputs:
                    interrupted_by_input = True
        elif self.parallel_tool_calls == "always":
            # 全并行（无法中断，完成后注入）
            results = list(await asyncio.gather(
                *[self._execute_tool_call(c, stats) for c in tool_calls]
            ))
        else:
            # auto 精细并行：写类工具串行（可被输入中断）+ 只读工具并行
            write_indices = [i for i, c in enumerate(tool_calls) if c.name in WRITE_TOOLS]
            read_indices = [i for i, c in enumerate(tool_calls) if c.name not in WRITE_TOOLS]
            results: List[Optional[str]] = [None] * len(tool_calls)
            for i in write_indices:
                if self._check_abort():
                    return None
                if interrupted_by_input:
                    results[i] = INTERRUPTED
                    continue
                results[i] = await self._execute_tool_call(tool_calls[i], stats)
                if self.injected_inputs:
                    interrupted_by_input = True
            if read_indices and not interrupted_by_input:
                read_results = await asyncio.gather(
                    *[self._execute_tool_call(tool_calls[i], stats) for i in read_indices]
                )
                for i, r in zip(read_indices, read_results):
                    results[i] = r
            # 兜底：中断/跳过的未执行项填占位（防 None 进入消息链）
            for i in range(len(results)):
                if results[i] is None:
                    results[i] = INTERRUPTED
        return results

    async def _append_tool_results(
        self, tool_calls: List[ToolCall], results: List[str], messages: List[Message],
    ) -> None:
        """阶段三（b）：工具结果按调用序回填消息链（assistant tool_calls ↔ tool 原子对）。"""
        for call, result_text in zip(tool_calls, results):
            tool_result = Message(
                role="tool",
                name=call.name,
                tool_call_id=call.id,
                content=result_text,
            )
            messages.append(tool_result)
            await self.kernel.events.emit("message:added", {"message": tool_result.to_dict()})

    # ------------------------------------------------------------------ 工具执行

    def _should_parallelize(self, tool_calls: List[ToolCall]) -> bool:
        """并行判定：never 串行；always 并行；auto 仅本轮全只读时并行。
        单个工具调用无需并行；写类工具存在时保持顺序依赖语义。"""
        if self.parallel_tool_calls == "never" or len(tool_calls) <= 1:
            return False
        if self.parallel_tool_calls == "always":
            return True
        return all(c.name not in WRITE_TOOLS for c in tool_calls)

    async def _execute_tool_call(self, call: ToolCall, stats: Dict[str, Any]) -> str:
        tool_name = call.name
        token = current_tool_call.set(call.id)
        try:
            return await self._execute_tool_call_inner(call, stats)
        finally:
            current_tool_call.reset(token)

    async def _execute_tool_call_inner(self, call: ToolCall, stats: Dict[str, Any]) -> str:
        tool_name = call.name

        # 1. 死循环检测（连续 N 次相同工具+相同参数）
        if self.state.register_and_check_loop(tool_name, call.arguments):
            return (
                f"[Harness Defense]: 检测到死循环！你已用完全相同参数连续调用 {tool_name} "
                f"{self.state.loop_threshold} 次。请停止并换一种策略。"
            )

        # 2. JSON 容错解析（失败回填给 LLM 自愈）
        ok, args, error = safe_json_parse(call.arguments)
        if not ok:
            return f"[Harness Defense]: {error}"

        # 3. beforeTool 安全管道（SecurityPlugin 等）
        hook_data = {"toolName": tool_name, "args": args, "cancel": False, "reason": ""}
        verified = await self.kernel.before_tool.run(self.kernel.ctx, hook_data)

        start_time = time.time()
        if verified.get("cancel"):
            stats["blocked"] += 1
            result_text = f"[Tool Execution Cancelled]: {verified.get('reason') or '被安全策略拒绝。'}"
        else:
            await self.kernel.events.emit(
                "tool:before_execute", {"toolName": tool_name, "args": args, "callId": call.id}
            )
            try:
                # execute_command 支持自定义 timeout（覆盖默认 tool_timeout）
                effective_timeout = self.tool_timeout
                if tool_name == "execute_command" and args.get("timeout"):
                    effective_timeout = max(float(args["timeout"]), self.tool_timeout)
                raw = await asyncio.wait_for(
                    self.registry.execute(tool_name, args), timeout=effective_timeout
                )
            except asyncio.TimeoutError:
                raw = f"[Tool Timeout]: 工具 {tool_name} 执行超过 {effective_timeout}s 被终止。"
            except Exception as exc:  # 注册表内已捕获，这里兜底
                raw = f"[Execution Exception]: {exc}"
            try:
                result_text = truncate_tool_output(raw, output_dir=self.truncation_dir).content
            except Exception:  # 落盘失败等极端情况：不阻断工具链，降级为原样输出
                logger.exception("[AgentLoop] 工具输出截断失败，原样返回")
                result_text = raw[:50_000]
            stats["tool_calls"] += 1

        duration_ms = int((time.time() - start_time) * 1000)

        # 4. afterTool 管道（结果修饰）
        try:
            post = await self.kernel.after_tool.run(
                self.kernel.ctx,
                {"toolName": tool_name, "result": result_text, "args": args},
            )
            result_text = post.get("result", result_text)
        except Exception:
            pass

        await self.kernel.events.emit(
            "tool:after_execute",
            {"toolName": tool_name, "durationMs": duration_ms, "callId": call.id,
             "status": "cancelled" if verified.get("cancel") else "success",
             "result": result_text},
        )
        return result_text

    def _filtered_skill_index(self) -> Optional[str]:
        """技能索引（过滤 deny 的技能）。kernel 无 app 服务时返回 None 走默认。"""
        try:
            app = self.kernel.get_service("app")
            lines = []
            for s in app.skills_list():
                if s.get("permission") == "deny":
                    continue
                desc = s.get("description") or "使用该技能目录中的 SKILL.md"
                lines.append(f"- {s['name']}: {desc}")
            return "\n".join(lines) or "（当前没有发现可用技能）"
        except Exception:
            return None

    async def _inject_queued(self, messages: List[Message]) -> bool:
        """注入任务运行期间用户补充的指令，返回是否有新注入。

        两个调用点：A-（回合开始）与 G2（工具结果回填完成）。批次执行
        期间到达的输入只做非破坏性窥探（interrupted_by_input），真正的
        注入统一发生在消息链合法位置——下一轮 LLM 调用立即可见。
        """
        injected_any = False
        if self.injected_inputs:
            while self.injected_inputs:
                text = str(self.injected_inputs.popleft()).strip()
                if not text:
                    continue
                injected = Message(role="user", content=f"[用户补充指令] {text}")
                messages.append(injected)
                await self.kernel.events.emit("message:added", {"message": injected.to_dict()})
                logger.info("[AgentLoop] 已注入用户补充指令（%d 字符）", len(text))
            injected_any = True
        # agent 间消息（P2 合作）：同一注入点；payload 由 manager 构造
        # （含来源与"非用户指令、不构成授权"声明，防伪安全语义）
        if self.agent_inbox:
            while self.agent_inbox:
                text = str(self.agent_inbox.popleft()).strip()
                if not text:
                    continue
                injected = Message(role="user", content=f"[来自其他 Agent 的消息] {text}")
                messages.append(injected)
                await self.kernel.events.emit("message:added", {"message": injected.to_dict()})
                logger.info("[AgentLoop] 已注入 agent 间消息（%d 字符）", len(text))
            injected_any = True
        return injected_any

    async def _inject_agent_notifications(self, messages: List[Message]) -> bool:
        """注入已完成的子 Agent 通知（完成即通知模型，多智能体 P1）。

        数据源：loop.agent_manager_factory（按 session_id 查询 SessionAgentManager，
        create_loop 挂载）。父运行中每轮 turn 边界投递；跨任务的遗留通知
        在 run_task 开头注入；任务结束时 _finish 注入后随落盘持久化。
        """
        factory = getattr(self, "agent_manager_factory", None)
        if factory is None:
            return False
        manager = factory(self.kernel.session_id)
        if manager is None:
            return False
        notes = manager.drain_notifications()
        for text in notes:
            injected = Message(role="user", content=text)
            messages.append(injected)
            await self.kernel.events.emit("message:added", {"message": injected.to_dict()})
            logger.info("[AgentLoop] 已注入子 Agent 完成通知（%d 字符）", len(text))
        return bool(notes)

    # ------------------------------------------------------------------ 上下文压缩

    async def _try_compact(
        self, messages: List[Message], cap: int
    ) -> Optional[List[Message]]:
        """opencode 风格压缩：旧轮次 LLM 摘要化，最近轮次原样保留。

        摘要替换只发生一次（前缀失效一次），此后前缀逐字节稳定 → 缓存命中延续；
        摘要失败时回退旧裁剪策略（prune_messages）。
        """
        plan = self.context_manager.split_for_compaction(messages, hard_cap=cap)
        if plan is None:
            return None
        head, tail, head_tokens = plan
        summary = await self._summarize_history(head)
        if not summary:
            return None
        system = messages[0] if messages and messages[0].role == "system" else None
        compacted = ([system] if system else []) + [
            Message(role="user", content=f"[历史摘要] {summary}")
        ] + tail
        self._compression_count += 1
        self._compressed_tokens += max(0, head_tokens - TokenCounter.count_text_tokens(summary))
        logger.info("[AgentLoop] 上下文摘要压缩: 压缩 %s 条旧消息, 释放 %s tokens",
                    len(head), head_tokens)
        return compacted

    async def _summarize_history(self, head: List[Message]) -> Optional[str]:
        """前缀对齐的摘要调用（deepseek-harness 模式）。

        逐字复用主对话的 system + tools + head 消息作为请求前缀，把压缩指令
        作为最后一条 user 消息追加——辅助调用成为上次请求的真前缀，KV cache
        被复用而不是失效。旧实现（独立"压缩器"system + 拼接文本）的前缀全新，
        数万 token 的历史全部按未命中计费。
        """
        try:
            system = next((m for m in head if m.role == "system"), None)
            body = [m for m in head if m.role != "system"]
            if not body:
                return None
            # 超长保护：head 是已发送过的缓存内容，逐字转发通常更便宜；
            # 但极端长会话仍需截断，避免单次请求超限
            total_chars = sum(len(m.content or "") for m in body)
            if total_chars > 200_000:
                return None
            instruction = (
                "请将以上全部对话历史压缩为一段精炼的中文摘要，作为后续工作的背景说明：\n"
                "保留已完成的决策与结论、修改过的文件清单、关键发现与未完成的任务，"
                "丢弃过程性细节。直接输出摘要正文，不要任何前缀，不要调用任何工具。"
            )
            messages = ([system] if system else []) + body + [
                Message(role="user", content=instruction),
            ]
            content, _, _ = await self.adapter.chat_stream(
                messages,
                self.registry.get_tools(),   # 工具 schema 也逐字复用（前缀对齐）
                None,
            )
            # 模型若误发工具调用则无正文 → 回退裁剪策略
            return (content or "").strip() or None
        except Exception:
            logger.exception("[AgentLoop] 历史摘要失败，回退旧裁剪策略")
            return None

    # ------------------------------------------------------------------ 收尾

    async def _finish(self, content: str, messages: List[Message], stats: Dict[str, Any],
                      store_snapshot: bool) -> Tuple[str, Dict[str, Any]]:
        # 任务收尾前投递剩余子 Agent 通知：随消息链落盘持久化，
        # 下个任务（或本会话重新打开）自然在上下文中看到交付结果
        try:
            await self._inject_agent_notifications(messages)
        except Exception:
            logger.debug("[AgentLoop] 收尾通知注入失败", exc_info=True)
        if store_snapshot:
            self._save_session()
        payload = self._stats_payload(stats)
        await self.kernel.events.emit("task:done", {"content": content, "stats": payload})
        return content, payload

    def _save_session(self) -> None:
        if self.session_store is not None:
            try:
                existing = self.session_store.load(self.kernel.session_id)
                meta = dict(existing.metadata) if existing is not None else {}
                meta["updated_by"] = "agent_loop"
                self.session_store.save(
                    self.kernel.session_id, self.kernel.ctx.messages, meta,
                )
            except Exception:
                logger.exception("[AgentLoop] 会话落盘失败")

    def _cache_price(self) -> float:
        return cache_price_of(self.pricing)

    def _cost_of(self, miss: int, hit: int, output_tokens: int) -> float:
        """按「未命中输入 / 命中输入 / 输出」三段计价（每 M token 单价）。"""
        return (miss / 1_000_000 * float(self.pricing.get("input_per_mtok", 0) or 0)
                + hit / 1_000_000 * self._cache_price()
                + output_tokens / 1_000_000 * float(self.pricing.get("output_per_mtok", 0) or 0))

    def _estimate_cost(self, stats: Dict[str, Any]) -> float:
        """缓存感知的成本估算（本任务累计口径）。

        输入按命中/未命中分开计价：未命中按 input_per_mtok 全价，命中按
        cache_hit_per_mtok（默认 input 的 10%，对齐 Anthropic 0.1x /
        DeepSeek 折扣的行业惯例）。stats 里的 hit/miss 已在 D2 阶段按
        供应商口径拆好（OpenAI 兼容: prompt 已含命中，miss = prompt-hit；
        Anthropic: input 不含 cache_read，miss 即全量非命中输入），因此
        miss + hit 就是真实总输入。仅当估算兜底（无 usage）时 hit/miss
        均为 0，回退按 input_tokens 全价。
        """
        hit = int(stats.get("cache_hit_tokens", 0) or 0)
        miss = int(stats.get("cache_miss_tokens", 0) or 0)
        if hit + miss <= 0:
            miss = int(stats.get("input_tokens", 0) or 0)
        return self._cost_of(miss, hit, int(stats.get("output_tokens", 0) or 0))

    def _last_call_cost(self) -> float:
        """最近一次调用的成本（「本次调用」口径）。"""
        call = self._last_call or {}
        return self._cost_of(
            int(call.get("cache_miss_tokens", 0) or 0),
            int(call.get("cache_hit_tokens", 0) or 0),
            int(call.get("output_tokens", 0) or 0),
        )

    def _stats_payload(self, stats: Dict[str, Any]) -> Dict[str, Any]:
        return {
            **stats,
            "cost_estimate": round(self._estimate_cost(stats), 4),
            "status": self.state.status.value,
        }

    def _effective_cap(self) -> int:
        """上下文有效上限 = max(预算下限, 90% × 模型上下文窗口)。

        opencode 风格：只在接近模型上限时才压缩——预算只作为小窗口模型的
        兜底下限，避免任务中途频繁裁剪旧消息破坏缓存前缀（裁剪=前缀打洞=miss）。
        """
        budget = self.context_manager.max_allowed_tokens
        window_cap = int(0.9 * self.context_window)
        if self.context_window >= int(budget / 0.9):
            return window_cap
        return min(budget, window_cap)

    async def _emit_context_stats(self, stats: Dict[str, Any]) -> None:
        """推送「上下文情况」统计（任务内实时，会话累计由 TaskHandle 合并）。"""
        hit = stats.get("cache_hit_tokens", 0)
        miss = stats.get("cache_miss_tokens", 0)
        hit_rate = round(hit / (hit + miss), 4) if (hit + miss) > 0 else None
        last_call = dict(self._last_call or {})
        # 当前上下文水位 = 最近一次调用真正发出去的 prompt（不是任务累计值）
        prompt_tokens = (last_call.get("prompt_tokens")
                         or self._last_prompt_estimate
                         or stats.get("input_tokens", 0))
        usage_ratio = round(prompt_tokens / self.context_window, 4) if self.context_window else None
        model = getattr(self.adapter, "model", None) or ""
        # 成本：缓存感知（命中按折扣价，见 _estimate_cost），随任务内累计实时更新
        cost = self._estimate_cost(stats)
        last_call["cost_estimate"] = round(self._last_call_cost(), 4)
        await self.kernel.events.emit("context:stats", {
            "model": model,
            "context_window": self.context_window,
            # 实际计费单价（每 M token，美元）：面板直接展示，避免"钱不对"无从对账
            "pricing": pricing_payload(self.pricing),
            "task": {
                # 注意：以下 *_tokens / cost_estimate 均为「本任务累计」；
                # 「本次调用」请看 last 段（每轮都会把整段上下文重发，累计值 ≈ 轮数 × 上下文）
                "prompt_tokens": stats.get("input_tokens", 0),
                "output_tokens": stats.get("output_tokens", 0),
                "cache_hit_tokens": hit,
                "cache_miss_tokens": miss,
                "cache_hit_rate": hit_rate,
                "compression_count": self._compression_count,
                "compressed_tokens": self._compressed_tokens,
                "usage_ratio": usage_ratio,
                "last_prompt_tokens": prompt_tokens,
                "turns": stats.get("turns", 0),
                "tool_calls": stats.get("tool_calls", 0),
                "blocked": stats.get("blocked", 0),
                "cost_estimate": round(cost, 4),
                "last": last_call,
            },
        })
