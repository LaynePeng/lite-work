"""任务运行器：管理并发任务、SSE 事件队列、审批挂起。"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from ..app import AgentApp
from ..core.agent_loop import AgentLoop
from ..core.system_prompt import SystemPromptBuilder
from ..core.types import Message

logger = logging.getLogger("litework.tasks")

EVENT_FORWARD = {
    "llm:stream", "llm:turn_start", "message:added", "tool:before_execute",
    "tool:after_execute", "approval:request", "approval:resolved", "task:start",
    "task:done", "task:error", "stats:update", "subagent:completed",
    "context:stats", "subagent:started", "subagent:progress", "skill:loaded",
    "todo:updated", "question:request", "question:resolved",
}


class TaskHandle:
    def __init__(self, task_id: str, kernel, registry: Any, loop: AgentLoop, app: AgentApp) -> None:
        self.task_id = task_id
        self.kernel = kernel
        self.registry = registry
        self.loop = loop
        self.app = app
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self.abort_event = asyncio.Event()
        self.loop.abort_event = self.abort_event
        # 用户补充指令队列：与 loop 共享同一 deque，回合开始时注入对话
        self.pending_inputs: deque = deque(maxlen=32)
        self.loop.injected_inputs = self.pending_inputs
        self.task: Optional[asyncio.Task] = None
        self.stopping = False
        self.running = False
        self.done = False
        self.subscription = None

    def queue_input(self, text: str) -> int:
        """任务运行中追加用户补充指令，Agent 在下一回合开始前注入对话。"""
        self.pending_inputs.append(text)
        count = len(self.pending_inputs)
        self._forward_event({"type": "chat:queued",
                             "data": {"text": text, "count": count}})
        return count

    def _forward_event(self, data: Any) -> None:
        event_type = data.get("type") if isinstance(data, dict) else ""
        terminal = event_type in {"task:done", "task:error"}
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            # 高频中间事件可以丢弃旧事件，但终止事件必须进入队列。
            try:
                if terminal:
                    while True:
                        self.queue.get_nowait()
                        try:
                            self.queue.put_nowait(data)
                            break
                        except asyncio.QueueFull:
                            continue
                else:
                    self.queue.get_nowait()
                    self.queue.put_nowait(data)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                logger.warning("[Task %s] SSE 队列溢出，丢弃事件", self.task_id)

    def _subscribe_events(self) -> None:
        async def _listener(event_name: str, payload: Any) -> None:
            if event_name == "context:stats":
                # 合并会话级累计（任务内实时 + 会话累计一并推给前端）
                session_stats = self.app.accumulate_context_stats(
                    self.kernel.session_id, (payload or {}).get("task") or {}
                )
                payload = {**(payload or {}), "session": session_stats}
            if event_name in EVENT_FORWARD:
                self._forward_event({"type": event_name, "data": payload})

        # 子 Agent 完成归档：写入会话 metadata（跨页面刷新/重启恢复）
        async def _persist_subagent_completed(payload: Any) -> None:
            try:
                snapshot = self.app.session_store.load(self.kernel.session_id)
                if snapshot is None:
                    return
                records = list((snapshot.metadata or {}).get("subagent_records") or [])
                records.append({
                    "subagentId": payload.get("subagentId") or "",
                    "role": payload.get("role") or "general",
                    "task": payload.get("task") or "",
                    "tokens": payload.get("tokens_used") or 0,
                    "turns": payload.get("turns") or 0,
                    "summary": payload.get("summary") or "",
                    "status": "completed",
                })
                # 最多保留 20 条；update_metadata 会合并到现有 metadata
                self.app.session_store.update_metadata(self.kernel.session_id, {"subagent_records": records[-20:]})
            except Exception:
                logger.debug("[Task %s] 子 Agent 归档落盘失败", self.task_id, exc_info=True)

        self.kernel.events.on("subagent:completed", _persist_subagent_completed)
        self.kernel.events.on("llm:stream", lambda p: _listener("llm:stream", p))
        for name in EVENT_FORWARD - {"llm:stream"}:
            self.kernel.events.on(name, lambda p, n=name: _listener(n, p))

    async def run(self, prompt: str) -> None:
        self._subscribe_events()
        self.running = True
        todo_plugin = getattr(self.app, "todo_plugin", None)
        if todo_plugin is not None:
            todo_plugin.bind(self.kernel.session_id, self.kernel.events)
        try:
            # Agent 专属角色提示（Plan 的规划人格 / 自定义 Agent）注入 System Prompt；
            # build 无专属提示时输出与通用版逐字节一致，缓存前缀不受影响
            agent_prompt = None
            try:
                profile = self.app.get_agent(self.agent_id)
                agent_prompt = profile.system_prompt
            except KeyError:
                pass
            # ask 权限的技能：任务启动前逐个审批，拒绝的不注入
            ask_names = list(getattr(self, "skill_ask_names", []) or [])
            if ask_names:
                for name in ask_names:
                    ok = await self._approve_skill(name)
                    extra = self.skill_extra or ""
                    if ok:
                        content = self._read_skill_content(name)
                        if content:
                            self.skill_extra = (extra + ("\n\n" if extra else "") + content)
                    else:
                        # 审批拒绝：从已加载名单剔除，给出可见提示
                        self.skill_names = [n for n in (self.skill_names or []) if n != name]
                        note = f"[技能 {name!r} 需要确认，已被操作员拒绝]"
                        self.skill_extra = (extra + ("\n\n" if extra else "") + note) if extra else note
            system_prompt = SystemPromptBuilder.build(
                self.app.workspace, self.registry.get_tools(), agent_prompt=agent_prompt,
                skill_extra=getattr(self, "skill_extra", None),
                skill_index=self._filtered_skill_index(),
            )
            skill_extra_used = getattr(self, "skill_extra", None)
            if skill_extra_used:
                # 轻量提示：告知用户本任务注入了哪些技能（不进会话历史）
                self._forward_event({"type": "skill:loaded",
                                     "data": {"names": getattr(self, "skill_names", []) or []}})
            await self.loop.run_task(prompt, system_prompt=system_prompt, store_snapshot=True)
        except asyncio.CancelledError:
            self._forward_event({"type": "task:error",
                                 "data": {"message": "[Stopped]: 任务被取消。"}})
        except Exception as exc:
            logger.exception("[Task %s] 运行异常", self.task_id)
            self._forward_event({"type": "task:error", "data": {"message": str(exc)}})
        finally:
            self.running = False
            self.done = True
            if todo_plugin is not None:
                todo_plugin.unbind(self.kernel.session_id)
            # 任务结束：清差分基线，下个任务的统计从 0 重新起算
            self.app._last_task_snapshot.pop(self.kernel.session_id, None)
            # 结束哨兵必须送达，否则客户端会一直处于运行状态。
            while True:
                try:
                    self.queue.put_nowait(None)
                    break
                except asyncio.QueueFull:
                    try:
                        self.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

    def stop(self) -> None:
        """先置协作式中止信号，再强杀挂起的 asyncio 任务（LLM 流卡住时靠它解套）。"""
        self.stopping = True
        self.abort_event.set()
        if self.task is not None and not self.task.done():
            self.task.cancel()

    async def _approve_skill(self, name: str) -> bool:
        """ask 权限技能的启动前审批（复用全局审批门 + approval:request 事件）。"""
        future = self.app.approval_gate.request_approval(
            f'加载技能 "{name}"', "该技能的权限规则为 ask，使用前需要确认。")
        approval_id = self.app.approval_gate.current_id(future)
        await self.kernel.events.emit("approval:request", {
            "id": approval_id, "action": f'加载技能 "{name}"',
            "reason": "该技能的权限规则为 ask，使用前需要确认。",
        })
        approved = await future
        await self.kernel.events.emit("approval:resolved", {"id": approval_id, "approved": approved})
        return approved

    def _read_skill_content(self, name: str) -> Optional[str]:
        try:
            from ..tools.skills import SkillsTools
            return SkillsTools(self.app.workspace).read_skill(name)
        except Exception:
            return None

    def _filtered_skill_index(self) -> Optional[str]:
        """技能索引（过滤 deny 的技能），供 System Prompt 技能段使用。"""
        try:
            lines = []
            for s in self.app.skills_list():
                if s.get("permission") == "deny":
                    continue
                desc = s.get("description") or "使用该技能目录中的 SKILL.md"
                lines.append(f"- {s['name']}: {desc}")
            return "\n".join(lines) or "（当前没有发现可用技能）"
        except Exception:
            return None


class TaskManager:
    def __init__(self, app: AgentApp) -> None:
        self.app = app
        self.tasks: Dict[str, TaskHandle] = {}

    def start(self, session_id: str, prompt: str, agent_id: Optional[str] = None,
              reasoning_effort: Optional[str] = None) -> TaskHandle:
        task_id = uuid.uuid4().hex[:12]
        # 按 Agent 配置裁剪工具集（build 全量 / plan 只读 / 自定义）
        registry = self.app.create_agent_registry(agent_id or "build")
        kernel = self.app.create_kernel(session_id, registry=registry)
        # 多轮对话：加载该 session 已落盘的历史消息到上下文，
        # 避免每轮新建 kernel 时从空上下文开始、落盘覆盖上一轮对话
        snapshot = self.app.session_store.load(session_id)
        if snapshot and snapshot.messages:
            kernel.ctx.messages = list(snapshot.messages)
        model_override = (snapshot.metadata.get("model") if snapshot else None) or None
        if not isinstance(model_override, dict):
            model_override = None
        loop = self.app.create_loop(kernel, registry, agent_id=agent_id,
                                    model_override=model_override,
                                    reasoning_effort_override=reasoning_effort)
        loop.parallel_tool_calls = str(self.app.config.get("parallel_tool_calls", "auto")).lower()

        handle = TaskHandle(task_id, kernel, registry, loop, self.app)
        handle.agent_id = agent_id or "build"
        handle.skill_extra, handle.skill_names, handle.skill_ask_names = self._resolve_skill_extra(prompt)
        self.tasks[task_id] = handle
        handle.task = asyncio.get_event_loop().create_task(handle.run(prompt))
        return handle

    def _resolve_skill_extra(self, prompt: str) -> Tuple[Optional[str], List[str], List[str]]:
        """技能权限感知的解析：/skill 显式命令 + triggers 自动匹配。

        返回 (skill_content, loaded_names, ask_names)：
        - allow 的技能直接注入内容；
        - deny 的技能对 Agent 隐藏——/skill 显式加载给出禁用提示，
          triggers 自动匹配静默跳过；
        - ask 的技能延迟到任务启动时（TaskHandle.run）逐个审批后注入。
        """
        from ..core.commands import parse_skill_command
        try:
            from ..tools.skills import SkillsTools
        except Exception:
            return None, [], []
        try:
            skills_tools = SkillsTools(self.app.workspace)
            skill_content = ""
            loaded_names: List[str] = []
            ask_names: List[str] = []
            cmd = parse_skill_command(prompt)
            if cmd and cmd.get("name"):
                name = cmd["name"]
                action = self.app.skill_permission(name)
                if action == "deny":
                    skill_content += f"[技能 {name!r} 已被权限规则禁用，请检查 skill_permissions 配置]"
                    loaded_names.append(name)
                elif action == "ask":
                    ask_names.append(name)
                    loaded_names.append(name)
                else:
                    content = skills_tools.read_skill(name)
                    if content:
                        # 附带技能目录绝对路径：SKILL.md 中的相对路径（脚本等）
                        # 以技能目录为基准，而非用户工作区——缺路径 Agent 只能瞎找
                        skill_path = skills_tools._skills().get(name)
                        skill_dir = str(skill_path.parent) if skill_path else "（未知）"
                        skill_content += f"[技能 {name} 目录：{skill_dir}，文档中的相对路径以该目录为基准]\n\n{content}"
                    else:
                        # 未命中时给出可用技能清单，Agent 不需要自己去文件系统瞎找
                        available = [s["name"] for s in skills_tools.list_skills()]
                        avail_hint = ("；可用技能：" + ", ".join(available)) if available else "（当前没有发现任何技能）"
                        skill_content += f"[未找到技能 {name!r}，请检查名称{avail_hint}]"
                    loaded_names.append(name)
            # triggers 自动匹配（/skill 命令时跳过，避免重复注入）
            if not cmd:
                trigger_mode = getattr(self.app, "config", {}).get("skill_trigger_mode", "substring")
                for skill in skills_tools.match_skills(prompt, trigger_mode):
                    name = skill["name"]
                    action = self.app.skill_permission(name)
                    if action == "deny":
                        continue  # deny 对 Agent 隐藏
                    if action == "ask":
                        if name not in ask_names:
                            ask_names.append(name)
                            loaded_names.append(name)
                        continue
                    content = skills_tools.read_skill(name)
                    if content:
                        skill_path = skills_tools._skills().get(name)
                        skill_dir = str(skill_path.parent) if skill_path else "（未知）"
                        header = f"[技能 {name} 目录：{skill_dir}，文档中的相对路径以该目录为基准]\n\n"
                        skill_content += ("\n\n" if skill_content else "") + header + content
                        loaded_names.append(name)
            return (skill_content or None), loaded_names, ask_names
        except Exception:
            logger.exception("[TaskManager] 技能解析失败（忽略，不影响任务）")
            return None, [], []

    def get(self, task_id: str) -> Optional[TaskHandle]:
        return self.tasks.get(task_id)

    def active_for_session(self, session_id: str) -> Optional[TaskHandle]:
        """该会话当前正在运行的任务（供 /api/chat 排队补充指令）。"""
        for handle in self.tasks.values():
            if handle.running and not handle.stopping and handle.kernel.session_id == session_id:
                return handle
        return None

    def stop(self, task_id: str) -> bool:
        handle = self.tasks.get(task_id)
        if handle is None:
            return False
        handle.stop()
        return True

    def cleanup(self, task_id: str) -> None:
        self.tasks.pop(task_id, None)

    def active_count(self) -> int:
        return sum(1 for t in self.tasks.values() if t.running)
