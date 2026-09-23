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
from typing import Any, Callable, Dict, List, Optional, Tuple

from .compaction_economics import decide_compaction
from .context_manager import ContextManager, patch_dangling_tool_calls, repair_tool_call_pairs
from .json_repair import safe_json_parse
from .observation_pack import (
    observations_dir, project_observations, read_recall_chunk,
)
from .kernel import Kernel
from .session_store import SessionStore
from .state_tracker import AgentStateTracker, AgentStatus
from .system_prompt import SystemPromptBuilder
from .token_counter import TokenCounter
from .truncator import truncate_tool_output
from .types import Message, ToolCall, ToolDefinition, header_context
from ..llm.pricing_provider import is_off_peak_now
from ..llm.stream_meter import StreamMeter, use_stream_meter
from ..tools.todos import current_session_id, current_root_session_id

logger = logging.getLogger("litework.agentloop")

# 当前执行的工具调用 ID（跨层传递给 spawn_agent 等需要关联事件的工具；
# asyncio 同一 task 内 ContextVar 可靠传播，并行协程各自独立 context）
current_tool_call: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_tool_call", default=None
)

# 写类工具：auto 模式下写类工具串行执行、只读工具并行执行（顺序依赖风险，如 write A + read A）
WRITE_TOOLS = frozenset({
    "write_file", "apply_search_replace", "apply_unified_diff",
    "execute_command", "git_commit", "git_push",
})


def _count_result_bytes(text: str) -> int:
    """工具结果字节数（与 truncator/observation_pack 的口径一致）。"""
    return len(text.encode("utf-8", errors="replace"))


def cache_price_of(pricing: Dict[str, float]) -> float:
    """缓存命中单价：显式配置优先，否则按 input 的 10% 折算（0.1x 惯例）。"""
    configured = pricing.get("cache_hit_per_mtok")
    if configured is None:
        configured = float(pricing.get("input_per_mtok", 0) or 0) * 0.1
    return float(configured or 0)


def pricing_payload(pricing: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """面板/接口统一口径的计费单价（每 M token，美元）+ 来源元信息。

    成本估算完全由这几个单价决定，因此随统计一起下发，供前端直接展示对账：
    - off_peak / off_peak_active：分时供应商（DeepSeek）的空闲档与当前是否生效；
    - source / source_age_seconds / stale：价格来源与新鲜度，过期时前端提示去
      设置页手动同步（不自动联网）。
    """
    p = pricing or {}
    out: Dict[str, Any] = {
        "input_per_mtok": float(p.get("input_per_mtok", 0) or 0),
        "output_per_mtok": float(p.get("output_per_mtok", 0) or 0),
        "cache_hit_per_mtok": cache_price_of(p),
        "source": str(p.get("source") or ""),
        "source_age_seconds": p.get("source_age_seconds"),
        "stale": bool(p.get("stale")),
    }
    off_peak = p.get("off_peak")
    if isinstance(off_peak, dict) and off_peak:
        out["off_peak"] = {
            "input_per_mtok": float(off_peak.get("input_per_mtok", 0) or 0),
            "output_per_mtok": float(off_peak.get("output_per_mtok", 0) or 0),
            "cache_hit_per_mtok": cache_price_of(off_peak),
        }
        out["off_peak_active"] = is_off_peak_now(p)
    return out


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
        pricing: Optional[Dict[str, Any]] = None,
        auto_approve: bool = False,
        context_window: Optional[int] = None,
        truncation_dir: Optional[str] = None,
        reducer_adapter=None,
        enable_observation_pack: bool = True,
        enable_compaction_economics: bool = True,
        header_conversation_id: Optional[str] = None,
        denied_tools: Optional[Dict[str, str]] = None,
    ) -> None:
        self.kernel = kernel
        self.adapter = adapter
        self.registry = registry
        # 越权工具 → 自解释拒绝消息（app.create_loop / sub_agent 按权限域装配；
        # None = 未提供，退回注册表的「未注册的工具」通用报错）
        self.denied_tools: Dict[str, str] = denied_tools or {}
        self.session_store = session_store
        self.context_manager = context_manager or ContextManager(token_budget)
        self.max_steps = max_steps
        self.tool_timeout = tool_timeout
        self.llm_timeout = llm_timeout
        # LLM 瞬时故障（超时/网络/限流/5xx）的自动重试次数
        self.llm_retries = max(0, int(llm_retries))
        self.auto_approve = auto_approve
        # 定价字典可携带 off_peak 档与来源元信息（见 app.resolve_pricing）
        self.pricing: Dict[str, Any] = pricing or {
            "input_per_mtok": 2.0, "output_per_mtok": 8.0}
        self.context_window = context_window or 128_000
        self.state = AgentStateTracker()
        self.abort_event: Optional[asyncio.Event] = None
        self.workspace: str = "."
        # 隔离工作树模式：非 None 时本任务在 worktree 内执行，工具调用受边界检查
        # （isolation_root = worktree 路径；main_workspace = 用户主工作区，仅供提示）
        self.isolation_root: Optional[str] = None
        self.main_workspace: Optional[str] = None
        # 截断落盘目录（第4/5课：超限工具输出保存到磁盘，上下文只放句柄）
        # 观察打包的归档也放在其 observations/ 子目录（须在 _register_obs_recall_tool 之前赋值）
        self.truncation_dir: Optional[str] = truncation_dir
        # 上下文压缩/缓存统计（供「上下文情况」面板）
        self._compression_count = 0
        self._compressed_tokens = 0
        self._last_usage: Optional[Dict[str, int]] = None
        # 最近一次 LLM 调用的用量与速度（「本次调用」口径，与 stats 里的任务累计区分）：
        # tokens 为整数，速度字段可能为 None（窗口过小/未计量）→ 用 Any
        self._last_call: Optional[Dict[str, Any]] = None
        # 无 usage 时每轮估算的 prompt 规模（用于「本次调用」的兜底口径）
        self._last_prompt_estimate: int = 0
        # 并行工具执行模式："auto"（只读轮并行/含写串行）| "always" | "never"
        self.parallel_tool_calls: str = "auto"
        # 任务运行期间用户补充的输入队列（TaskHandle 持有同一个 deque，跨回合注入）
        self.injected_inputs: deque = deque()
        # agent 间消息队列（P2 合作）：其他 agent 经 send_message 发来的消息，
        # turn 边界注入本 agent 上下文（与用户补充指令同一合法注入点）
        self.agent_inbox: deque = deque()
        # ---- 效率机制（v1.6.0，SoL-Pi 存活机制的 lite-work 适配）----
        # 观察打包：大工具结果「先全文后占位符」，原文归档 + obs_recall 分页召回
        self._obs_root = observations_dir(self.truncation_dir) if enable_observation_pack else None
        self._mech_obs_saved_tokens = 0
        self._mech_obs_packed = 0
        self._mech_reducer_saved_tokens = 0
        # 证据收据 reducer（opt-in：传入小模型适配器才启用）
        self.reducer_adapter = reducer_adapter
        # 孙 agent 完成通知注入源（可选装配）：按 session_id 取会话级 AgentManager。
        # 由 AgentApp / SubAgentRunner 装配时注入；None = 未装配（无注入源）
        self.agent_manager_factory: Optional[Callable[[str], Any]] = None
        # 压缩经济学开关（关闭则回到旧「超阈值即摘要」行为）
        self._enable_compaction_economics = enable_compaction_economics
        # custom_headers 模板展开用 conversation_id 的注入值（子 Agent 从父会话继承；
        # None = 走 session_store 惰性生成逻辑，见 _build_header_context）
        self.header_conversation_id: Optional[str] = header_conversation_id
        # 最近一次压缩决策理由（面板可解释性）
        self._compaction_reason: Optional[str] = None
        # ---- token 速度（v1.9.x）：本任务的配对累计 ----
        # 为什么放在实例属性而不是 stats 字典：stats 会被原样作为 stats:update 事件
        # 发出（StatsUpdatePayload 有严格字段声明，strict 模式下多一个键就抛错），
        # 速度是「面板展示口径」而非 stats 契约的一部分。
        self._speed_output_tokens = 0   # Σ 有生成窗口那几轮的输出 tokens
        self._speed_gen_ms = 0          # Σ 生成窗口（毫秒）
        self._speed_turns = 0           # 计入平均的轮数
        self._register_obs_recall_tool()

    def _register_obs_recall_tool(self) -> None:
        """注册 obs_recall：按字节分页召回已归档的大工具结果。

        registry 可能在多个 loop 间共享，幂等注册；无归档目录时整体停用。
        """
        if not self._obs_root or self.registry is None or self.registry.has("obs_recall"):
            return
        root = self._obs_root

        async def _recall(args: Dict[str, Any]) -> str:
            obs_id = str(args.get("id", ""))
            if not obs_id.startswith("obs_") or len(obs_id) > 40 or "/" in obs_id or ".." in obs_id:
                return f"[Error]: 非法的观察 id: {obs_id}"
            offset = max(0, int(args.get("offset") or 0))
            try:
                chunk = read_recall_chunk(root, obs_id, offset)
            except FileNotFoundError:
                return f"[Error]: 未知的观察 id: {obs_id}"
            header = (
                f"[obs_recall id={obs_id} offset={offset} "
                f"next_offset={chunk.next_offset} eof={chunk.eof}]\n"
                f"[本页 {chunk.bytes:,} bytes / {chunk.lines} 行；未读完时用 next_offset 续读]\n"
            )
            return header + chunk.text

        self.registry.register(
            "obs_recall",
            "按 id 与字节偏移分页读取已归档的大工具结果原文（上下文中只保留占位符的大结果）",
            {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "占位符里的观察 id（obs_xxxxxxxx）"},
                    "offset": {"type": "integer", "minimum": 0,
                               "description": "字节偏移，默认 0；按返回的 next_offset 续读"},
                },
                "required": ["id"],
            },
            _recall,
        )
        logger.info("[AgentLoop] 已注册 obs_recall（观察打包归档目录: %s）", root)

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
        if wants_conversation:
            if self.header_conversation_id:
                # 子 Agent：直接继承父会话 conversation_id（子 loop 无 session_store）
                ctx["conversation_id"] = self.header_conversation_id
            elif self.session_store is not None:
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

        # token 速度累计按任务清零（stats 是 stats:update 的严格契约，速度另存）
        self._speed_output_tokens = 0
        self._speed_gen_ms = 0
        self._speed_turns = 0

        # 流式计量器（token 速度）：整任务复用同一实例，每次尝试前 reset。
        # 不传 usage 的调用方（子 Agent / 证据收据）另建实例或不用——不影响主任务口径。
        meter = StreamMeter()

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

                # B3. 观察打包（投影层）：大工具结果「先全文后占位符」，session 存档不动。
                # fail-open：任何失败都保留原文，绝不为省 token 丢证据。
                if self._obs_root:
                    try:
                        processed, obs_saved, obs_packed = project_observations(processed, self._obs_root)
                        if obs_saved > 0:
                            self._mech_obs_saved_tokens += obs_saved
                            self._mech_obs_packed += obs_packed
                    except Exception:
                        logger.debug("[AgentLoop] 观察打包失败，本轮回退原文", exc_info=True)

                # 每轮估算本轮 prompt 规模（按投影后的实际请求计），供「本次调用」在
                # 无 usage 时兜底展示/计费。注意：这里不再把估算值累进 stats——输入是否
                # 计入由 _record_usage 按「该轮最终有无 usage」决定，否则首轮会「估算 +
                # 真实 usage」双计（实测 3 轮 × 10k 的任务被报成 71k），而「第 1 轮有
                # usage、后续没有」时又会整轮漏计。
                self._last_prompt_estimate = TokenCounter.count_messages_tokens(processed)

                # 阶段一：LLM 调用（流式，内部 emit llm:stream；瞬时故障指数退避重试）
                try:
                    content, tool_calls, usage = await self._call_llm_with_retry(
                        processed, tools, meter)
                except AgentLoop._LLMCallFailure as failure:
                    logger.warning("[AgentLoop] LLM 调用失败: %s", failure.message)
                    messages.append(Message(role="assistant", content=f"[LLM Error]: {failure.message}"))
                    return await self._finish(f"[LLM Error]: {failure.message}", messages, stats, store_snapshot)

                # 阶段三：状态更新（usage 累加 + 本轮速度 + 上下文水位推送）
                self._record_usage(usage, content, stats, meter)
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
                    # 打上产生该消息的 Agent 身份（Agent 切换检测的数据基础）
                    agent=getattr(self.kernel, "orchestrator_agent_id", None),
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

                # G-. 动作融合（SoL-Pi T5）：edit/write 的 then 验证命令在同一轮内
                #     执行——一次工具调用完成「改 + 验」，省去下一轮的完整请求。
                #     走与普通工具完全相同的审批/守卫/截断链路，不绕过安全边界。
                results = await self._apply_action_fusion(tool_calls, results, stats)

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
            # 取消时给未执行的 tool_calls 补占位结果再落盘：否则历史永久带着
            # 悬空对，下个任务被 repair 整条丢弃（丢上下文 + 击穿 prompt cache）
            patch_dangling_tool_calls(messages)
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
            # build 含 _git_info 的两次 git 子进程调用（各 3s 超时）与技能索引
            # 读取——纯同步重活丢线程池，防事件循环被冻（SSE/审批全部无响应）
            system_prompt = await asyncio.to_thread(
                SystemPromptBuilder.build,
                self.workspace, tools, skill_index=self._filtered_skill_index(),
            )
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
        """上下文裁剪：有效上限 = max(预算下限, 90% × 模型窗口)。

        超阈值后不再无脑 LLM 摘要：先过压缩经济学决策（摘要写入成本 + 缓存债
        vs 剩余预期轮数的收益，见 compaction_economics），划算才摘要，否则推迟
        并用免费裁剪兜底；决策理由随 context:stats 下发（面板可解释）。
        """
        cap = self._effective_cap()
        count = TokenCounter.count_messages_tokens(messages)
        if count <= cap:
            return messages

        plan = self.context_manager.split_for_compaction(messages, hard_cap=cap)
        decision = None
        if plan is not None and self._enable_compaction_economics:
            head, _tail, head_tokens = plan
            decision = decide_compaction(
                head_tokens=head_tokens,
                summary_tokens=max(1, head_tokens // 10),   # 摘要长度预估：head 的 10%
                context_tokens=count,
                context_window=self.context_window,
                current_turn=int(stats.get("turns", 0) or 0),
                max_turns=self.max_steps,
                pricing=self._pricing_now(),
            )
            self._compaction_reason = decision.reason
            if decision.compact:
                compacted = await self._try_compact(messages, cap, plan=plan)
                if compacted is not None:
                    messages[:] = compacted
                    return messages
                self._compaction_reason = "summary_failed"   # 摘要失败 → 免费裁剪兜底
        elif not self._enable_compaction_economics:
            self._compaction_reason = "economics_disabled"
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
            if decision is None and self._compaction_reason is None:
                self._compaction_reason = "pruned"
        return payload

    async def _call_llm_with_retry(
        self, processed: List[Message], tools: List[ToolDefinition],
        meter: Optional[StreamMeter] = None,
    ) -> Tuple[str, List[ToolCall], Optional[Dict[str, int]]]:
        """阶段一：调用 LLM（流式，内部 emit llm:stream）。

        瞬时故障（单次超时 / 网络抖动 / 限流 / 5xx）自动指数退避重试，
        避免长思考模型或网络波动直接杀死整个任务；重试过程 emit
        "llm:retry" 事件供 UI 展示。不可重试错误（鉴权/参数等）立即失败，
        抛出 _LLMCallFailure 由 run_task 统一收尾。

        meter：每次**尝试**开始前重置——失败尝试的字符/时间戳不能算进速度。
        """
        attempt = 0
        while True:
            try:
                if meter is not None:
                    meter.reset()
                    meter.start()
                # 计量器通过 contextvar 传给适配器（不改进 chat_stream 签名：
                # 社区/测试里的自定义适配器无需改动，不实现计量也不影响功能）
                with use_stream_meter(meter):
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
        meter: Optional[StreamMeter] = None,
    ) -> None:
        """阶段三（a）：用模型返回的 usage 累加（准确值），无 usage 时回退估算。

        stats 是「本任务累计」（每轮把整段上下文重发，逐轮线性累加），
        而 self._last_call 记录「最近一次调用」的用量——两者口径不同，
        面板必须分开显示，否则单轮 1 万 token 的任务在几十轮后看起来像上百万。

        meter（可选）：流式增量计量器。传入时额外算出「本轮 token 速度」——
        生成速度（排除首字等待）、端到端速度、首字延迟，并把本轮计入任务平均。
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
        self._record_speed(usage, stats, meter)

    def _record_speed(
        self, usage: Optional[Dict[str, int]], stats: Dict[str, Any],
        meter: Optional[StreamMeter],
    ) -> None:
        """把本轮生成速度写进 self._last_call，并累加「任务平均」的配对分子/分母。

        口径（见 StreamMeter）：
          - tps_gen：生成速度 = 输出 tokens ÷ 生成窗口（末块−首块），**排除首字等待**
            ——这才是「模型吐字速度」，跨供应商/模型可比；
          - tps_e2e：端到端 = 输出 tokens ÷（末块−请求发出），含首字，贴近体感；
          - ttft_ms：首字延迟，单独展示（推理模型 TTFT 长但吐字不慢，不能混为一谈）。

        任务平均必须用**配对**的分子/分母：只把「既有输出 tokens 又有生成窗口」的
        轮次计入，否则缺测轮次会把平均值抬高（分母缺、分子照加）。
        """
        if meter is None:
            return
        tokens = int((self._last_call or {}).get("output_tokens", 0) or 0)
        # 有 usage 时用精确输出 tokens 校准估算值（estimated=False）
        exact = int(usage.get("completion_tokens", 0)) if usage else None
        snap = meter.snapshot(exact_output_tokens=exact)
        # 复制后再写（而不是原地 update）：_last_call 在本方法外可能为 None，
        # 显式重新赋值既让类型收敛，也避免与其它引用共享可变字典。
        call: Dict[str, Any] = dict(self._last_call or {})
        call.update({
            "ttft_ms": snap["ttft_ms"],
            "gen_ms": snap["gen_ms"],
            "e2e_ms": snap["e2e_ms"],
            "tps_gen": snap["tps_gen"],
            "tps_e2e": snap["tps_e2e"],
            "tps_estimated": snap["estimated"],
        })
        self._last_call = call
        gen_ms = snap["gen_ms"]
        if gen_ms:
            # 配对累计：任务平均 = Σ输出 / Σ生成窗口
            self._speed_output_tokens += tokens
            self._speed_gen_ms += gen_ms
            self._speed_turns += 1

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
            # 默认全填中断占位：未执行 / 被跳过的索引保持占位（等价于原实现
            # 「先填 None 再兜底替换」，同时让列表保持 List[str]，无需再兜底）
            results = [INTERRUPTED] * len(tool_calls)
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
        return results

    async def _append_tool_results(
        self, tool_calls: List[ToolCall], results: List[str], messages: List[Message],
    ) -> None:
        """阶段三（b）：工具结果按调用序回填消息链（assistant tool_calls ↔ tool 原子对）。

        配置了 reducer 模型时（opt-in），大体积诊断类结果会先被压缩成「证据收据」：
        收据里的逐字引用必须能在原文中找到，校验失败或压缩失败一律回退原文
        （SoL-Pi C11/D1 思路：宁可不省，也不能丢证据）。
        """
        for call, result_text in zip(tool_calls, results):
            content = result_text
            if self.reducer_adapter is not None:
                try:
                    content = await self._reduce_to_receipt(call.name, result_text)
                except Exception:
                    logger.debug("[AgentLoop] 证据收据生成失败，回退原文", exc_info=True)
            tool_result = Message(
                role="tool",
                name=call.name,
                tool_call_id=call.id,
                content=content,
            )
            messages.append(tool_result)
            await self.kernel.events.emit("message:added", {"message": tool_result.to_dict()})

    async def _reduce_to_receipt(self, tool_name: str, text: str) -> str:
        """大诊断结果 → 证据收据。校验不过就原样返回（fail-open）。"""
        from .observation_pack import REDUCE_THRESHOLD_BYTES, estimate_tokens

        if _count_result_bytes(text) < REDUCE_THRESHOLD_BYTES:
            return text
        original_tokens = estimate_tokens(text)
        prompt = (
            "你是日志压缩器。把下面的工具输出压缩成一份「证据收据」，要求：\n"
            "1. 保留关键结论、错误信息与重要数字；\n"
            f"2. 用「> 」前缀逐字引用原文中最关键的 1-3 段（每段 20~200 字符，"
            "必须与原文逐字一致，不得改写）；\n"
            f"3. 收据总长度不超过原文的 1/8；\n"
            "4. 不要编造原文没有的信息。\n\n工具输出：\n"
        )
        receipt, _calls, _usage = await asyncio.wait_for(
            self.reducer_adapter.chat_stream(
                [Message(role="user", content=prompt + text)], [], None
            ),
            timeout=max(30.0, self.llm_timeout / 2),
        )
        receipt = (receipt or "").strip()
        if not receipt:
            return text
        # 逐字引用校验：至少 1 段 ≥24 字符的引用能 在原文中找到（忽略空白差异）
        quotes = [ln[2:].strip() for ln in receipt.split("\n") if ln.startswith("> ")]
        import re as _re

        def _norm(s: str) -> str:
            return _re.sub(r"\s+", "", s)

        normalized_original = _norm(text)
        valid = [q for q in quotes if len(_norm(q)) >= 24 and _norm(q) in normalized_original]
        if len(valid) < 1:
            logger.info("[AgentLoop] 收据引用校验失败（%d 条引用），回退原文", len(quotes))
            return text
        receipt_tokens = estimate_tokens(receipt)
        saved = original_tokens - receipt_tokens
        if saved <= 0 or receipt_tokens > original_tokens // 4:
            return text   # 压缩收益不足 → 不如放原文
        # 原文归档，收据里保留召回入口（证据永不丢失）
        from .observation_pack import archive_text, observation_id

        obs_id = observation_id(text)
        archive_text(self._obs_root or "", text, tool_name)
        self._mech_reducer_saved_tokens += saved
        logger.info("[AgentLoop] 证据收据: %s → %d tokens（省 %d）", tool_name,
                    receipt_tokens, saved)
        return (
            f"[receipt id={obs_id}] 以下为工具 {tool_name} 输出的证据收据"
            f"（原文 {original_tokens:,} tokens 已归档，可 obs_recall(id=\"{obs_id}\") 召回）：\n"
            f"{receipt}"
        )

    _FUSION_TOOLS = {"write_file", "apply_search_replace", "apply_unified_diff"}
    _FUSION_MAX_COMMANDS = 4
    _FUSION_OUTPUT_LIMIT = 2_000   # 单条 then 命令输出的截断长度

    async def _apply_action_fusion(
        self, tool_calls: List[ToolCall], results: List[str], stats: Dict[str, Any],
    ) -> List[str]:
        """动作融合：edit/write 调用里的 then 验证命令立即执行（同轮完成「改+验」）。

        then 命令以 execute_command 工具调用走 _execute_tool_call——与普通工具
        完全相同的审批/守卫/截断链路，不绕过任何安全边界。命令失败不影响
        主操作结果（改动已落盘），输出如实附回，由模型决定下一步。
        """
        import json as _json

        for i, call in enumerate(tool_calls):
            if call.name not in self._FUSION_TOOLS or not results[i]:
                continue
            ok, parsed, _err = safe_json_parse(call.arguments or "{}")
            args = parsed if ok and isinstance(parsed, dict) else None
            try:
                then_cmds = args.get("then") if args else None
            except Exception:
                continue
            if not isinstance(then_cmds, list) or not then_cmds:
                continue
            outputs: List[str] = []
            for j, cmd in enumerate(then_cmds[: self._FUSION_MAX_COMMANDS]):
                if not isinstance(cmd, str) or not cmd.strip():
                    continue
                fusion_call = ToolCall(
                    id=f"{call.id}#then{j}",
                    name="execute_command",
                    arguments=_json.dumps({"command": cmd, "timeout": 120}),
                )
                out = await self._execute_tool_call(fusion_call, stats)
                outputs.append(f"--- then[{j}] $ {cmd} ---\n{out[: self._FUSION_OUTPUT_LIMIT]}")
            if outputs:
                results[i] = (
                    f"{results[i]}\n\n[then 验证结果（动作融合，同轮执行）]\n"
                    + "\n".join(outputs)
                )
        return results

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

    def _worktree_isolation_violation(self, tool_name: str, args: Dict[str, Any]) -> Optional[str]:
        """隔离工作树模式的工具边界检查（参考 Claude Code 的四层防护）。

        - 文件类工具：路径参数必须落在 worktree 内；
        - 命令类工具：cwd 必须在 worktree 内；命令文本不得出现主工作区路径，
          不得设置 GIT_DIR / GIT_WORK_TREE / --git-dir / --work-tree 重定向。
        返回违规说明（直接作为工具结果回给 LLM 自愈），合法返回 None。
        """
        root = self.isolation_root
        if not root:
            return None
        try:
            root_real = os.path.realpath(root)
        except OSError:
            return None

        def _inside(path: str) -> bool:
            try:
                p = os.path.realpath(path if os.path.isabs(path) else os.path.join(root_real, path))
            except OSError:
                return False
            return p == root_real or p.startswith(root_real + os.sep)

        def _deny(what: str) -> str:
            return (
                f"[Worktree Isolation]: {what} 不在隔离工作树内。"
                f"你只能在 {root} 内工作——主工作区不允许读写（隔离模式下改动需用户审查后合并）。"
            )

        # 层 1：文件类工具路径参数
        for key in ("filePath", "path", "file", "dir", "directory"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                if not _inside(val):
                    return _deny(f"路径 {val!r}")

        # 层 2：命令 cwd
        cwd = args.get("cwd")
        if isinstance(cwd, str) and cwd.strip() and not _inside(cwd):
            return _deny(f"工作目录 {cwd!r}")

        # 层 3/4：命令文本里的主工作区路径与 git 目录重定向
        cmd = args.get("command")
        if isinstance(cmd, str) and cmd.strip():
            main = self.main_workspace
            if main:
                try:
                    main_real = os.path.realpath(main)
                except OSError:
                    main_real = main
                if main_real and main_real in cmd:
                    return _deny(f"命令引用了主工作区路径 {main_real!r}")
            upper = cmd.upper()
            if "GIT_DIR=" in upper or "GIT_WORK_TREE=" in upper:
                return _deny("命令设置了 GIT_DIR / GIT_WORK_TREE 重定向")
            if "--GIT-DIR" in upper or "--WORK-TREE" in upper:
                return _deny("命令使用了 --git-dir / --work-tree 重定向")
        return None

    async def _execute_tool_call_inner(self, call: ToolCall, stats: Dict[str, Any]) -> str:
        tool_name = call.name

        # 1. 死循环检测（精确重复：连续 N 次相同工具+相同参数；滑窗重读：同一文件
        #    反复读重叠区域但行窗口每次微变——精确哈希拦不住的震荡形态）
        if self.state.register_and_check_loop(tool_name, call.arguments):
            return (
                f"[Harness Defense]: 检测到死循环！{self.state.last_loop_detail}。"
                "请立即停止重复读取：目标内容已在上下文中，直接执行编辑/写入等下一步动作；"
                "若确实无法推进，停止调用工具并向用户说明遇到的困难。"
            )

        # 1.5 越权工具调用：不在当前 Agent 工具面内、且是被权限域 deny 的已知工具
        #     → 回填自解释拒绝（含原因与出路），而不是「未注册的工具」——后者易被
        #     模型误读为工具名笔误而换参数重试（v1.9.x 循环事故的信任污染入口）
        denial = self.denied_tools.get(tool_name)
        if denial is not None and not self.registry.has(tool_name):
            return denial

        # 2. JSON 容错解析（失败回填给 LLM 自愈）
        ok, args, error = safe_json_parse(call.arguments)
        if not ok:
            return f"[Harness Defense]: {error}"

        # 2.5 隔离工作树边界检查：禁止越过 worktree 触碰主工作区
        violation = self._worktree_isolation_violation(tool_name, args)
        if violation:
            stats["blocked"] += 1
            return violation

        # 3. beforeTool 安全管道（SecurityPlugin 等）
        hook_data = {"toolName": tool_name, "args": args, "cancel": False, "reason": ""}
        verified = await self.kernel.before_tool.run(self.kernel.ctx, hook_data)

        start_time = time.time()
        if verified.get("cancel"):
            stats["blocked"] += 1
            result_text = f"[Tool Execution Cancelled]: {verified.get('reason') or '被安全策略拒绝。'}"
        else:
            # execute_command 支持自定义 timeout（覆盖默认 tool_timeout）——
            # 提前计算并随 before_execute 下发（前端卡片看门狗依据）
            effective_timeout = self.tool_timeout
            if tool_name == "execute_command" and args.get("timeout"):
                effective_timeout = max(float(args["timeout"]), self.tool_timeout)
            await self.kernel.events.emit(
                "tool:before_execute",
                {"toolName": tool_name, "args": args, "callId": call.id,
                 "timeoutMs": int(effective_timeout * 1000)},
            )
            timed_out = False
            try:
                raw = await asyncio.wait_for(
                    self.registry.execute(tool_name, args), timeout=effective_timeout
                )
            except asyncio.TimeoutError:
                timed_out = True
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

        # 4. afterTool 管道（结果修饰）：post-processing hook 不能无限等待——
        # 卡住的 hook 会把整条工具链挂死（tool:after_execute 永不发出，前端
        # 工具卡永远「运行中」）。兜底上限跟随该工具自身的 effective_timeout
        # （工具允许跑多久，结果修饰就允许多久），不额外一刀切收紧。
        try:
            post = await asyncio.wait_for(
                self.kernel.after_tool.run(
                    self.kernel.ctx,
                    {"toolName": tool_name, "result": result_text, "args": args},
                ),
                timeout=effective_timeout,
            )
            result_text = post.get("result", result_text)
        except asyncio.TimeoutError:
            logger.warning(
                "[AgentLoop] afterTool hook 超时（%.0fs，同工具上限），跳过结果修饰: %s",
                effective_timeout, tool_name,
            )
        except Exception:
            pass

        await self.kernel.events.emit(
            "tool:after_execute",
            {"toolName": tool_name, "durationMs": duration_ms, "callId": call.id,
             # timeout 显性化：超时不再是 success——前端据此置错误样式，
             # LLM 结果文本里的 [Tool Timeout] 前缀也在前端映射兜底
             "status": ("cancelled" if verified.get("cancel")
                        else "timeout" if timed_out
                        else "success"),
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
        self, messages: List[Message], cap: int, plan=None
    ) -> Optional[List[Message]]:
        """opencode 风格压缩：旧轮次 LLM 摘要化，最近轮次原样保留。

        摘要替换只发生一次（前缀失效一次），此后前缀逐字节稳定 → 缓存命中延续；
        摘要失败时回退旧裁剪策略（prune_messages）。plan 可传入已计算的拆分结果
        （_trim_context 做经济学决策时已经算过，避免重复计算）。
        """
        plan = plan or self.context_manager.split_for_compaction(messages, hard_cap=cap)
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
            # 与主循环 payload 同口径：摘要请求不经 repair_tool_call_pairs，
            # 历史残留的悬空/无主 tool 对会直接 400（整次压缩失败回退裁剪）
            body = repair_tool_call_pairs(body)
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
                # 本任务的 Agent 身份随快照落盘：下个任务据此（消息无 agent
                # 标记的旧快照时）判定会话内是否发生了 Agent 切换
                agent_id = getattr(self.kernel, "orchestrator_agent_id", None)
                if agent_id:
                    meta["last_agent_id"] = agent_id
                self.session_store.save(
                    self.kernel.session_id, self.kernel.ctx.messages, meta,
                )
            except Exception:
                logger.exception("[AgentLoop] 会话落盘失败")

    def _pricing_now(self) -> Dict[str, Any]:
        """按**计价时刻**的分时窗口选档（高峰 / 空闲半价）。

        任务可能跨越高峰与空闲的边界（一天里两次高峰窗口），因此不能在建 loop
        时定死单价——每次计价都按当前时刻判断。无 off_peak 档时原样返回。
        """
        if is_off_peak_now(self.pricing):
            off_peak = self.pricing.get("off_peak")
            if isinstance(off_peak, dict):
                return {**self.pricing, **off_peak}
        return self.pricing

    def _cache_price(self) -> float:
        return cache_price_of(self._pricing_now())

    def _cost_of(self, miss: int, hit: int, output_tokens: int) -> float:
        """按「未命中输入 / 命中输入 / 输出」三段计价（每 M token 单价）。"""
        p = self._pricing_now()
        return (miss / 1_000_000 * float(p.get("input_per_mtok", 0) or 0)
                + hit / 1_000_000 * cache_price_of(p)
                + output_tokens / 1_000_000 * float(p.get("output_per_mtok", 0) or 0))

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
        last_call: Dict[str, Any] = dict(self._last_call or {})
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
            # 效率机制节省台账（本任务累计）：观察打包 / 证据收据 / 压缩决策理由
            "mechanisms": {
                "obs_saved_tokens": self._mech_obs_saved_tokens,
                "obs_packed": self._mech_obs_packed,
                "reducer_saved_tokens": self._mech_reducer_saved_tokens,
                # reducer 是 opt-in（config.reducer_model 为空即停用）：
                # 前端靠这个字段区分「未启用」和「启用了但暂无节省」
                "reducer_enabled": self.reducer_adapter is not None,
                "compaction_reason": self._compaction_reason,
            },
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
                # 本任务平均生成速度 = Σ输出 tokens ÷ Σ生成窗口（只算有生成窗口的轮次）
                "avg_tps": (round(self._speed_output_tokens / (self._speed_gen_ms / 1000.0), 2)
                            if self._speed_gen_ms else None),
                "speed_turns": self._speed_turns,
            },
        })
