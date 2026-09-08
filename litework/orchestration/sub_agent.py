"""子 Agent 编排（对应课程第11课 SubAgentRunner 真实化）。

上下文隔离：子 Agent 拥有独立 Kernel 与消息链；工具集按角色裁剪；
结果压缩：最终产出汇总为精简报告 + Token 消耗归集到父级事件。

第11课增强：角色来源扩展为 AgentRegistry —— 用户自定义的 subagent
（mode="subagent"）也能被 spawn_sub_agent 直接派生使用。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from ..core.agent_loop import AgentLoop
from ..core.system_prompt import FINAL_REPORT_REQUIREMENT, SystemPromptBuilder
from ..core.types import Message, ToolDefinition

logger = logging.getLogger("litework.orchestration")

ROLE_PROMPTS = {
    "explorer": "你是一名只读调研员。只能查看代码与搜索，禁止修改文件或执行破坏性命令。"
                "聚焦任务，给出简洁结论与关键文件/行号证据。",
    "tester": "你是一名测试执行员。负责运行测试并分析结果，可执行命令但禁止修改生产代码。",
    "refactor": "你是一名重构工程师。拥有完整工具集，负责完成指定的重构任务并验证。",
    "critic": "你是一名批判性审查员（Critic）。对给定的方案/代码/结论进行严格审查："
              "找出漏洞、边界情况、风险与未验证的假设；指出具体位置并给出改进建议。"
              "只读不写。输出按「问题清单（按严重度排序）→ 改进建议」组织。",
    "general": "你是一名专注的专家工人，聚焦你的任务并返回简洁总结。",
}

ROLE_TOOLS: Dict[str, List[str]] = {
    "explorer": ["read_file", "list_dir", "file_tree", "search_code", "get_file_outline",
                 "read_focused_symbol", "git_status", "git_diff", "git_log", "git_branch",
                 "review_code", "webfetch", "webfetch_batch"],
    "tester": ["read_file", "list_dir", "file_tree", "search_code", "get_file_outline",
               "read_focused_symbol", "execute_command", "git_status", "git_diff", "git_log",
               "git_branch"],
    # 批判者：只读 + 代码审查（互批/红蓝对抗模式的角色基础）
    "critic": ["read_file", "list_dir", "file_tree", "search_code", "get_file_outline",
               "read_focused_symbol", "review_code", "git_status", "git_diff",
               "webfetch", "webfetch_batch"],
    "refactor": None,  # 全部工具
    "general": None,
}

# 子 Agent 禁用的工具：禁止嵌套派生（P1；P2 放开到限深）+ 交互工具（通道未转发）。
# send_message / list_agents 开放给子 Agent——协作模式（互批/接力）里
# 子 Agent 需要与同伴通信；followup_task（唤醒续跑）保留给编排者。
SUB_AGENT_EXCLUDE = ["spawn_sub_agent", "spawn_agent",
                     "close_agent", "wait_agents", "followup_task", "ask_user"]

# 声明 allowed_dirs 的写域 agent：读全开 + 三个写文件工具（写受 IsolationPlugin 约束；
# shell 命令无法静态判定写目标，P1 不授予，由父 Agent 执行）
WRITE_SCOPE_TOOLS = ROLE_TOOLS["explorer"] + [
    "write_file", "apply_search_replace", "apply_unified_diff",
]


class SubAgentRunner:
    def __init__(self, app) -> None:
        self.app = app

    def _resolve_role(self, role: str):
        """从 AgentRegistry 查找角色（支持用户自定义 subagent），否则回退内置 ROLE_*。"""
        try:
            profile = self.app.agent_registry.get(role)
            if profile.mode in ("subagent", "all"):
                return profile
        except KeyError:
            pass
        return None

    async def run_task(
        self,
        task_description: str,
        role: str = "general",
        system_prompt: Optional[str] = None,
        max_steps: int = 12,
        parent_events=None,
        agent_id: Optional[str] = None,
        nickname: Optional[str] = None,
        allowed_dirs: Optional[List[str]] = None,
        record=None,
        initial_messages: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        if role == "explore":
            role = "explorer"
        profile = self._resolve_role(role)
        if profile is not None:
            allowed = profile.tools
            permissions = profile.permissions
            base_prompt = profile.system_prompt or ROLE_PROMPTS.get("general", "")
        else:
            allowed = ROLE_TOOLS.get(role)
            permissions = None
            base_prompt = system_prompt or ROLE_PROMPTS.get(role, ROLE_PROMPTS["general"])

        # 目录硬隔离模式：写域工具集（写操作由 IsolationPlugin 白名单约束）
        if allowed_dirs:
            allowed = WRITE_SCOPE_TOOLS

        registry = self.app.build_registry(
            allowed=allowed,
            exclude=SUB_AGENT_EXCLUDE,
            permissions=permissions,
        )
        sub_id = agent_id or f"sub_{uuid.uuid4().hex[:8]}"
        sub_kernel = self.app.create_kernel(sub_id, registry=registry)
        if allowed_dirs and record is not None:
            from .agent_manager import IsolationPlugin
            sub_kernel.use(IsolationPlugin(self.app.workspace, allowed_dirs, record))
        tools: List[ToolDefinition] = registry.get_tools()

        # 唤醒续跑（followup）：携带历史消息链，跳过 system 重插
        if initial_messages:
            sub_kernel.ctx.messages = list(initial_messages)

        system = (
            f"{FINAL_REPORT_REQUIREMENT}\n\n{base_prompt}\n\n[你的具体任务]\n{task_description}\n\n"
            f"工作目录: {self.app.workspace}\n"
            f"{SystemPromptBuilder._git_info(self.app.workspace)}"
        )
        if allowed_dirs:
            system += (
                f"\n[写入范围] 你只能修改以下目录内的文件：{', '.join(allowed_dirs)}。"
                "范围外的改动需求请写入最终报告，由主 Agent 决定。"
            )

        loop = AgentLoop(
            kernel=sub_kernel,
            adapter=self.app.adapter,
            registry=registry,
            session_store=None,  # 子 Agent 不落盘
            max_steps=max_steps,
            tool_timeout=float(self.app.config.get("tool_timeout", 120)),
            llm_timeout=float(self.app.config.get("llm_timeout", 300)),
            llm_retries=int(self.app.config.get("llm_retries", 2)),
            token_budget=int(self.app.config.get("token_budget", 48000)) // 2,
            auto_approve=bool(self.app.config.get("auto_approve", False)),
        )
        loop.workspace = self.app.workspace
        loop.truncation_dir = self.app.create_loop(sub_kernel, registry).truncation_dir
        # 挂载 loop 引用（send_message 直达运行中的 agent 的 agent_inbox）
        if record is not None:
            record.loop = loop

        logger.info('[SubAgent] 派生子 Agent role=%s task="%s..."',
                    role, task_description[:60])

        # 内部活动转发：子 kernel 事件命名空间化为 subagent:progress 发到父事件总线，
        # 前端按 callId 关联到 spawn_sub_agent 卡片，实现实时可见（隔离上下文不丢失）
        parent_bus = parent_events
        call_id = None
        try:
            from ..core.agent_loop import current_tool_call
            call_id = current_tool_call.get()
        except Exception:
            call_id = None

        def _brief(data: Any) -> str:
            try:
                s = json.dumps(data, ensure_ascii=False)
            except Exception:
                s = str(data)
            return s[:100] + ("…" if len(s) > 100 else "")

        async def _forward_progress(event_name: str, payload: Any) -> None:
            if parent_bus is None:
                return
            data = payload or {}
            item: Dict[str, Any] = {
                "subagentId": sub_id, "role": role, "kind": event_name, "callId": call_id,
            }
            if event_name == "llm:turn_start":
                item.update({"turn": data.get("turn")})
            elif event_name == "tool:before_execute":
                item.update({
                    "tool": data.get("toolName"), "brief": _brief(data.get("args")),
                    "status": "running",
                })
            elif event_name == "tool:after_execute":
                item.update({
                    "tool": data.get("toolName"), "status": data.get("status") or "done",
                    "durationMs": data.get("durationMs"),
                })
            else:
                return
            try:
                await parent_bus.emit("subagent:progress", item)
            except Exception:
                logger.debug("[SubAgent] progress 转发失败", exc_info=True)

        sub_kernel.events.on("llm:turn_start", lambda p: _forward_progress("llm:turn_start", p))
        sub_kernel.events.on("tool:before_execute", lambda p: _forward_progress("tool:before_execute", p))
        sub_kernel.events.on("tool:after_execute", lambda p: _forward_progress("tool:after_execute", p))

        # 3a. 审批透传：子 Agent 的安全审批事件转发到父事件总线，用户可在主界面审批
        if parent_events is not None:
            sub_kernel.events.on("approval:request", lambda p: _forward_progress("approval:request", p))
            sub_kernel.events.on("approval:resolved", lambda p: _forward_progress("approval:resolved", p))

        # 3b. 流式文本实时显示：转发 llm:stream 事件（带节流，避免高频刷屏）
        _streaming_buf: List[str] = []
        _last_stream_flush: float = 0

        async def _forward_llm_stream(payload: Any) -> None:
            nonlocal _last_stream_flush
            if parent_bus is None:
                return
            chunk = (payload or {}).get("chunk", "")
            if not chunk:
                return
            _streaming_buf.append(chunk)
            now = time.time()
            # 每 200ms 或缓冲区超过 500 字符时刷新一次
            if now - _last_stream_flush > 0.2 or sum(len(s) for s in _streaming_buf) > 500:
                text = "".join(_streaming_buf)
                _streaming_buf.clear()
                _last_stream_flush = now
                try:
                    await parent_bus.emit("subagent:progress", {
                        "subagentId": sub_id, "role": role, "kind": "llm:stream",
                        "callId": call_id, "text": text,
                    })
                except Exception:
                    pass

        sub_kernel.events.on("llm:stream", _forward_llm_stream)

        if parent_events is not None:
            await parent_events.emit("subagent:started", {
                "task": task_description,
                "role": role,
                "subagentId": sub_id,
                "nickname": nickname or sub_id,
                "callId": call_id,
            })

        timeout = float(self.app.config.get("subagent_timeout", 600))
        try:
            summary, stats = await asyncio.wait_for(
                loop.run_task(
                    f"请完成以下子任务并输出精炼总结（不要向用户提问，直接执行）：\n{task_description}",
                    system_prompt=system, tools=tools, store_snapshot=False,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            summary = f"[SubAgent Timeout]: 子任务超过 {timeout}s 被终止，已完成部分: {getattr(loop, 'last_summary', '无')}"
            stats = {"input_tokens": 0, "output_tokens": 0, "turns": 0, "status": "TIMEOUT"}
        completed_payload = {
            "task": task_description,
            "role": role,
            "subagentId": sub_id,
            "nickname": nickname or sub_id,
            "callId": call_id,
            "tokens_used": stats["input_tokens"] + stats["output_tokens"],
            "turns": stats["turns"],
            "summary": summary,
        }
        await sub_kernel.events.emit("subagent:completed", completed_payload)
        if parent_events is not None:
            await parent_events.emit("subagent:completed", completed_payload)

        logger.info('[SubAgent] 完成 role=%s turns=%s tokens=%s',
                    role, stats["turns"], stats["input_tokens"] + stats["output_tokens"])
        # 保存消息链历史（followup_task 唤醒续跑的上下文基础）
        if record is not None:
            record.messages = list(sub_kernel.ctx.messages)
        return {
            "summary": summary,
            "total_tokens_used": stats["input_tokens"] + stats["output_tokens"],
            "turns": stats["turns"],
            "completed": stats["status"] == "SUCCESS",
            "role": role,
        }
