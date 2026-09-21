# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""任务运行器：管理并发任务、SSE 事件队列、审批挂起。"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from ..app import AgentApp
from ..core.agent_loop import AgentLoop
from ..core.context_manager import patch_dangling_tool_calls
from ..core.system_prompt import SystemPromptBuilder

logger = logging.getLogger("litework.tasks")

EVENT_FORWARD = {
    "llm:stream", "llm:turn_start", "llm:retry", "message:added", "tool:before_execute",
    "tool:after_execute", "approval:request", "approval:resolved", "task:start",
    "task:done", "task:error", "stats:update", "subagent:completed",
    "context:stats", "subagent:started", "subagent:progress", "skill:loaded",
    "todo:updated", "question:request", "question:resolved",
    "agent:closed", "worktree:merge",
}


def _merge_worktree_prompt(name: str, user_prompt: str) -> str:
    """AI 合并任务的提示词：让 Agent 在主工作区把隔离工作树分支合并回来。

    用户不必手动解决冲突——这正是「让 AI 合并」的意义（与真实开发中让 Claude
    合并分支的做法一致）。约定：分支名 worktree-<name>，用 --no-ff 保留来源。
    """
    branch = f"worktree-{name}"
    lines = [
        f"[隔离工作树合并] 请把分支 `{branch}` 合并回当前分支，并处理可能出现的冲突。",
        "",
        f"1. 先确认当前分支与工作区状态：`git status`、`git branch --show-current`；",
        f"2. 执行合并：`git merge --no-ff -m \"lite-work: 合并隔离工作树（{name}）\" {branch}`；",
        "3. 若产生冲突：逐个打开冲突文件，理解双方意图后正确解决（保留双方有效改动、"
        "删掉冲突标记），再 `git add <文件>`，最后用上面的信息 `git commit` 完成合并提交；",
        "   —— 不要用 `git merge --abort` 一弃了之；确实无法解决时，保留冲突现场并在总结里说明原因；",
        "4. 合并后用 `git status` 与 `git log --oneline -3` 自检：不得残留未合并路径或冲突标记；",
        "5. 只做合并与冲突解决，不要顺手改动无关文件，也不要 push。",
    ]
    if user_prompt:
        lines += ["", "（用户附加说明：" + user_prompt + "）"]
    return "\n".join(lines)


class TaskHandle:
    def __init__(self, task_id: str, kernel, registry: Any, loop: AgentLoop, app: AgentApp) -> None:
        self.task_id = task_id
        self.kernel = kernel
        self.registry = registry
        self.loop = loop
        self.app = app
        # 每个 SSE 连接独立队列；首个订阅者前的事件保留，重连不回放
        self._subscribers: List[asyncio.Queue] = []
        self._retained: List[Any] = []
        self._first_subscriber_seen = False
        # 「需要用户响应」事件的保留区（审批/提问）：这类事件丢失的代价是
        # 审批卡永不出现 → 600s 静默超时拒绝 → 任务停滞。resolved 后移除；
        # 任意订阅者（含重连者）连接时回放，兜底断线窗口丢失。
        self.retained_requests: Dict[str, Any] = {}
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
        # 由 TaskManager 在构造后回填的动态属性（此处先声明类型，供类型检查
        # 与静态分析可见；默认值与回填前的读取语义一致）
        self.agent_id: str = ""
        self.skill_extra: Optional[str] = None
        self.skill_names: List[str] = []
        self.skill_ask_names: List[str] = []
        # 隔离工作树模式：任务实际工作区（None = 主工作区）
        self.workspace: Optional[str] = None
        self.worktree_name: Optional[str] = None
        # AI 合并任务：待合并的 worktree 名（非 None 时任务结束后校验并清理）
        self.merge_worktree: Optional[str] = None
        # 已广播过的冲突文件列表（去重，避免同一集合反复推送）
        self._merge_conflicts_seen: List[str] = []

    # ------------------------------------------------------------ SSE 订阅

    def subscribe(self) -> asyncio.Queue:
        """注册一个 SSE 订阅者，返回其专属事件队列。

        - 首个订阅者：回放连接前保留的全部事件（任务先于 SSE 启动的竞态）；
        - 重连订阅者：只接收订阅后的实时事件（回放会让 UI 重复渲染）；
        - 任务已结束时立即投递结束哨兵（迟到的订阅者直接收到 [DONE]，
          不会挂死在 keepalive 上）。
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=512)
        if not self._first_subscriber_seen:
            self._first_subscriber_seen = True
            for ev in self._retained:
                self._put(q, ev)
            self._retained.clear()
        # 回放尚未 resolved 的审批/提问（SSE 断线窗口里发出的事件，
        # 重连后补送达——否则审批卡永不出现，任务静默超时停滞）
        for ev in list(self.retained_requests.values()):
            self._put(q, ev)
        if self.done:
            q.put_nowait(None)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    @property
    def subscribers_drained(self) -> bool:
        """全部订阅者队列已排空（任务收尾清理的判定条件）。"""
        return all(q.empty() for q in self._subscribers)

    @staticmethod
    def _put(q: asyncio.Queue, data: Any) -> None:
        """单订阅者投递：溢出时高频中间事件丢旧，终止事件必须送达。"""
        terminal = isinstance(data, dict) and data.get("type") in {"task:done", "task:error"}
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            try:
                if terminal:
                    while True:
                        q.get_nowait()
                        try:
                            q.put_nowait(data)
                            break
                        except asyncio.QueueFull:
                            continue
                else:
                    q.get_nowait()
                    q.put_nowait(data)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                logger.warning("[Task %s] SSE 队列溢出，丢弃事件", data)

    def queue_input(self, text: str) -> int:
        """任务运行中追加用户补充指令，Agent 在下一回合开始前注入对话。"""
        self.pending_inputs.append(text)
        count = len(self.pending_inputs)
        self._forward_event({"type": "chat:queued",
                             "data": {"text": text, "count": count}})
        return count

    def _forward_event(self, data: Any) -> None:
        # 「需要用户响应」的事件进保留区（resolved 时移除），重连回放兜底；
        # 不进 _retained（subscribe 时两者都回放会重复投递同一审批）
        try:
            if isinstance(data, dict):
                etype, edata = data.get("type"), data.get("data") or {}
                rid = edata.get("id") if isinstance(edata, dict) else None
                if etype in ("approval:request", "question:request") and rid:
                    self.retained_requests[rid] = data
                    for q in list(self._subscribers):
                        self._put(q, data)
                    return
                elif etype in ("approval:resolved", "question:resolved") and rid:
                    self.retained_requests.pop(rid, None)
        except Exception:
            pass
        # 首个订阅者未连接：保留事件（终止事件不因截断丢失——保留列表足够长）
        if not self._first_subscriber_seen:
            self._retained.append(data)
            if len(self._retained) > 512:
                self._retained = self._retained[-512:]
        for q in list(self._subscribers):
            self._put(q, data)

    def _subscribe_events(self) -> None:
        async def _listener(event_name: str, payload: Any) -> None:
            if event_name == "context:stats":
                # 合并会话级累计（任务内实时 + 会话累计一并推给前端）
                session_stats = self.app.accumulate_context_stats(
                    self.kernel.session_id, (payload or {}).get("task") or {}
                )
                payload = {**(payload or {}), "session": session_stats}
            # AI 合并任务：每次命令执行后探测是否出现冲突 → 实时广播（前端显示解决进度）
            if (self.merge_worktree and event_name == "tool:after_execute"
                    and (payload or {}).get("toolName") == "execute_command"):
                try:
                    mgr = self.app.worktree_manager()
                    # git 探测是同步 subprocess：丢线程池，别冻事件循环
                    if await asyncio.to_thread(mgr.has_merge_in_progress):
                        conflicts = await asyncio.to_thread(mgr.merge_conflicts)
                        if conflicts and conflicts != self._merge_conflicts_seen:
                            self._merge_conflicts_seen = conflicts
                            self._emit_merge_phase("conflicts", conflicts=conflicts,
                                                   message="AI 正在解决冲突")
                except Exception:  # noqa: BLE001 - 探测失败不影响任务
                    pass
            if event_name in EVENT_FORWARD:
                self._forward_event({"type": event_name, "data": payload})

        # 子 Agent 完成归档：写入会话 metadata（跨页面刷新/重启恢复）。
        # followup 唤醒同一 agent 会再次完成 → 按 subagentId 去重（更新而非追加，
        # 否则恢复时看板出现同一 agent 的多张卡）
        async def _persist_subagent_completed(payload: Any) -> None:
            try:
                snapshot = self.app.session_store.load(self.kernel.session_id)
                if snapshot is None:
                    return
                records = list((snapshot.metadata or {}).get("subagent_records") or [])
                new_id = payload.get("subagentId") or ""
                records = [r for r in records if r.get("subagentId") != new_id]
                records.append({
                    "subagentId": payload.get("subagentId") or "",
                    "role": payload.get("role") or "general",
                    "task": payload.get("task") or "",
                    "nickname": payload.get("nickname") or "",
                    "mode": payload.get("mode") or "orchestrate",
                    "changed_files": payload.get("changed_files") or [],
                    "tokens": payload.get("tokens_used") or 0,
                    "turns": payload.get("turns") or 0,
                    "summary": payload.get("summary") or "",
                    "status": "completed",
                })
                # 保留条数取配置（默认 20）；update_metadata 会合并到现有 metadata
                keep = int(self.app.config.get("agent_persist_max", 20))
                self.app.session_store.update_metadata(
                    self.kernel.session_id, {"subagent_records": records[-keep:]})
            except Exception:
                logger.debug("[Task %s] 子 Agent 归档落盘失败", self.task_id, exc_info=True)

        self.kernel.events.on("subagent:completed", _persist_subagent_completed)
        self.kernel.events.on("llm:stream", lambda p: _listener("llm:stream", p))
        for name in EVENT_FORWARD - {"llm:stream"}:
            self.kernel.events.on(name, lambda p, n=name: _listener(n, p))

    async def run(self, prompt: str) -> None:
        self._subscribe_events()
        self.running = True
        # AI 合并任务：开场广播 started（前端显示合并进度条）
        if self.merge_worktree:
            self._emit_merge_phase("started", message="正在合并隔离工作树分支…")
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
            # build 含 git 子进程与技能索引读取（同步重活）：丢线程池防冻事件循环
            system_prompt = await asyncio.to_thread(
                SystemPromptBuilder.build,
                self.workspace or self.app.workspace or "", self.registry.get_tools(),
                agent_prompt=agent_prompt,
                skill_extra=getattr(self, "skill_extra", None),
                skill_index=self._filtered_skill_index(),
            )
            # 隔离工作树模式：明确告知 Agent 它在隔离副本里干活（改动不会被主工作区看到）
            if self.worktree_name:
                wt_path = self.workspace or ""
                system_prompt += (
                    "\n\n## 隔离工作树模式\n"
                    f"你正在一个**隔离工作树**中工作（路径：{wt_path}，分支："
                    f"worktree-{self.worktree_name}）。这是主工作区的一个 git worktree 副本：\n"
                    "- 你的所有读写、命令执行都发生在该目录内，**不会影响用户的主工作区**；\n"
                    "- 不要试图操作主工作区的路径，也不要切换分支或 checkout；\n"
                    "- 完成后正常结束任务即可——用户会在界面上审查你的变更，自行决定合并或丢弃。"
                )
            # 会话目标（/goal）：注入每个任务的系统提示——长程目标跨任务持续生效
            goal = self._session_goal()
            if goal:
                system_prompt += (
                    "\n\n## 当前会话目标\n"
                    f"{goal}\n\n"
                    "本会话的所有工作都应服务于该目标的推进；每轮汇报进展时先简述"
                    "距离目标的剩余工作。目标完成时在最终回复中明确说明。"
                )
            # 会话协作模式（对话框选择器）：覆盖全局模式，注入该模式的编排配方
            if self._session_collab_mode():
                from ..orchestration.collab_policy import get_collab_policy, list_collab_modes

                mode_name = self._session_collab_mode()
                display = next(
                    (m["display_name"] for m in list_collab_modes(self.app)
                     if m["name"] == mode_name), mode_name)
                recipe = get_collab_policy(self.app, session_id=self.kernel.session_id).recipe()
                system_prompt += (
                    "\n\n## 本会话协作模式（用户显式选定）\n"
                    f"用户已为本会话选定「{display}」模式，必须按以下编排流程执行本任务：\n\n"
                    f"{recipe}\n\n"
                    "重要：这是用户的显式指令，优先于工具描述中的默认治理策略——"
                    "不要因任务看似简单或与配方开头描述的典型场景不完全一致而改用其他方式；"
                    "除任务物理上无法拆分（纯问答、单步查询）外，"
                    "必须按上述流程派生子 Agent 协作完成。"
                )
            # Agent 角色切换检测：上一轮历史属于别的 Agent（build/plan/office/
            # research/自定义）时注入交接提示——新 Agent 必须知道历史操作是谁做的、
            # 自己的能力边界（对齐 OpenCode SessionReminders，泛化到全部 Agent）
            handover = self._role_handover_note()
            if handover:
                system_prompt += handover
            # 计划文件指引：plan 任务每次注入（对齐 OpenCode plan reminder 每轮注入）——
            # 切换场景已含于交接提示，这里覆盖「plan 连续多任务」的场景
            elif self.agent_id == "plan":
                try:
                    profile = self.app.get_agent("plan")
                    system_prompt += self._plan_file_handover("plan", profile)
                except KeyError:
                    pass
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
            # AI 合并任务收尾：校验合并是否真正完成（分支已并入主分支、无进行中的合并），
            # 成功则自动清理 worktree 与分支；仍有冲突则保留现场并提示用户
            if self.merge_worktree:
                self._finalize_merge_worktree(self.merge_worktree)
            if todo_plugin is not None:
                todo_plugin.unbind(self.kernel.session_id)
            # 任务结束：清差分基线，下个任务的统计从 0 重新起算
            self.app._last_task_snapshot.pop(self.kernel.session_id, None)
            # 结束哨兵必须送达所有订阅者，否则客户端会一直处于运行状态。
            for q in list(self._subscribers):
                while True:
                    try:
                        q.put_nowait(None)
                        break
                    except asyncio.QueueFull:
                        try:
                            q.get_nowait()
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
        action = f'加载技能 "{name}"'
        reason = "该技能的权限规则为 ask，使用前需要确认。"
        future = self.app.approval_gate.request_approval(
            action, reason, session_id=getattr(self.kernel, "session_id", None))
        approval_id = self.app.approval_gate.current_id(future)
        # rememberable 是 ApprovalRequestPayload 的必填字段（本路径无 rule →
        # False）；缺字段在 strict 事件模式下会直接抛错中断任务
        await self.kernel.events.emit("approval:request", {
            "id": approval_id, "action": action, "reason": reason,
            "rememberable": False,
        })
        approved = await future
        # 正常路径（server）：approval:resolved 由 gate.on_resolve（server/app.py
        # 注入）统一广播，含 by=timeout，本会话流内不重复发（避免 payload 缺字段
        # 与 by 取错值）。仅在 on_resolve 未注入时兜底补一条，防审批卡悬挂。
        if getattr(self.app.approval_gate, "on_resolve", None) is None:
            await self.kernel.events.emit("approval:resolved", {
                "id": approval_id, "approved": approved, "by": "user", "action": action,
            })
        return approved

    def _read_skill_content(self, name: str) -> Optional[str]:
        try:
            from ..tools.skills import SkillsTools
            return SkillsTools(self.app.workspace).read_skill(name)
        except Exception:
            return None

    def _emit_merge_phase(self, phase: str, *, conflicts: Optional[List[str]] = None,
                          files: int = 0, commits: int = 0, message: str = "") -> None:
        """广播合并阶段事件（前端显示合并进度条）。"""
        name = self.merge_worktree or ""
        self._forward_event({"type": "worktree:merge", "data": {
            "name": name,
            "branch": f"worktree-{name}" if name else "",
            "phase": phase,
            "conflicts": conflicts or [],
            "files": files,
            "commits": commits,
            "message": message,
        }})

    def _finalize_merge_worktree(self, name: str) -> None:
        """AI 合并任务结束后的校验与收尾（在主工作区执行）。"""
        try:
            mgr = self.app.worktree_manager()
            if mgr.has_merge_in_progress():
                conflicts = mgr.merge_conflicts()
                self._emit_merge_phase("conflicts", conflicts=conflicts,
                                       message="主工作区仍有冲突待解决")
                logger.info("[Task] AI 合并未完成，仍有 %d 个冲突文件", len(conflicts))
                return
            if mgr.is_branch_merged(name):
                # 合并成功：读一次变更摘要 → 清理 worktree + 分支 → 关闭会话隔离开关
                # （否则 metadata 仍为 worktree=true，下个任务会再建一个工作树）
                st = mgr.status(name)
                files = len(st.get("files") or [])
                commits = int(st.get("commits") or 0)
                mgr.cleanup(name)
                self.app.set_session_worktree(self.kernel.session_id, False)
                self._emit_merge_phase("merged", files=files, commits=commits,
                                       message="已合并并清理工作树")
                logger.info("[Task] AI 合并完成，已清理 worktree %s", name)
            else:
                self._emit_merge_phase("failed",
                                       message=f"分支 worktree-{name} 尚未并入当前分支")
        except Exception:  # noqa: BLE001 - 收尾失败不应影响任务结束
            logger.exception("[Task] AI 合并收尾失败")
            self._emit_merge_phase("failed", message="合并收尾时发生异常，请检查 git 状态")

    def _session_goal(self) -> Optional[str]:
        """会话目标（/goal 设置，存于 session metadata）。"""
        try:
            snapshot = self.app.session_store.load(self.kernel.session_id)
            goal = (snapshot.metadata or {}).get("goal") if snapshot else None
            return str(goal).strip() or None if goal else None
        except Exception:
            return None

    def _session_collab_mode(self) -> Optional[str]:
        """会话协作模式（对话框选择器写入，存于 session metadata）。"""
        try:
            snapshot = self.app.session_store.load(self.kernel.session_id)
            mode = (snapshot.metadata or {}).get("collab_mode") if snapshot else None
            return str(mode).strip() or None if mode else None
        except Exception:
            return None

    # ------------------------------------------------------------ Agent 角色切换检测

    # 具备写能力的工具集合：接手 Agent 的白名单与之有交集（且未被 deny）
    # 即视为「可写型」，切换文案据此选择方向指引（对齐 sub_agent.WRITE_SCOPE_TOOLS）
    _WRITE_TOOLS = frozenset({
        "write_file", "apply_search_replace", "apply_unified_diff",
        "delete_file", "execute_command",
    })

    def _last_history_agent(self) -> Optional[str]:
        """历史中最后一条带 agent 标记的 assistant 消息的 agent id。

        消息级 agent 标记由 AgentLoop 写入（Message.agent）；旧快照无标记
        时回退 metadata.last_agent_id；两者都无 → None（不触发切换提示）。
        """
        for msg in reversed(self.kernel.ctx.messages):
            if msg.role == "assistant" and getattr(msg, "agent", None):
                return str(msg.agent)
        try:
            snapshot = self.app.session_store.load(self.kernel.session_id)
            last = (snapshot.metadata or {}).get("last_agent_id") if snapshot else None
            return str(last).strip() or None if last else None
        except Exception:
            return None

    def _can_write(self, profile) -> bool:
        """从 profile 推导该 Agent 是否具备写能力（职责域模型优先）。

        domains：edit / execute / git_write 任一域 allow 即视为可写型
        （execute=ask 或 office 等受限可写归为可写——重点是区分「纯只读」）。
        旧模型（tools+permissions）仍兼容：tools=None=全量，deny 排除后判断。
        """
        if profile is None:
            return False
        if profile.domains:
            from ..core.permissions import resolved_domains

            resolved = resolved_domains(profile.domains)
            return any(resolved.get(d) == "allow" for d in ("edit", "execute", "git_write"))
        tools = profile.tools  # None = 全量
        denied = {k for k, v in (profile.permissions or {}).items() if v == "deny"}
        if tools is None:
            return not (self._WRITE_TOOLS & denied)
        allowed = set(tools) - denied
        return bool(self._WRITE_TOOLS & allowed)

    def _profile_capability(self, profile) -> str:
        """从 profile.tools + permissions 推导能力措辞（可写型 / 只读型）。"""
        if self._can_write(profile):
            return ("你具备文件修改与命令执行能力——此前 Agent 产出的分析结论、"
                    "计划或未完成的工作，经你判断后可以直接落地执行")
        return ("你仅具备只读能力（读文件 / 搜索 / git 查看），"
                "不得执行、继续或撤销此前的任何修改操作——只做观察、分析与规划")

    def _role_handover_note(self) -> Optional[str]:
        """会话内 Agent 切换检测：上一轮历史属于别的 Agent 时生成交接提示。

        对齐 OpenCode 的 SessionReminders（plan.txt / build-switch.txt）机制，
        但泛化到全部主 Agent 与自定义 Agent：身份/能力边界从 AgentProfile
        动态推导，不写死任何 id 组合。
        """
        current = self.agent_id
        try:
            profile = self.app.get_agent(current)
        except KeyError:
            return None
        prev = self._last_history_agent()
        if not prev or prev == current:
            return None
        try:
            prev_profile = self.app.get_agent(prev)
            prev_display = (prev_profile.description or prev_profile.id).strip() or prev
        except KeyError:
            prev_display = prev
        cur_display = (profile.description or profile.id).strip() or current
        note = (
            "\n\n## 角色切换提示\n"
            f"本会话此前的任务由「{prev_display}」（agent_id={prev}）执行，"
            "历史消息中的工具调用、文件改动与提交均为该 Agent 所为，"
            "**不是你的操作**——不要把历史当成自己的工作，也不要试图“继续”或"
            "“撤销”它们，请基于当前仓库 / 文件的实际状态开展工作。\n"
            f"你现在是「{cur_display}」（agent_id={current}）。"
            f"{self._profile_capability(profile)}。"
        )
        # 计划文件交接（对齐 OpenCode：plan 会话留下的计划文件，接手方续用）
        note += self._plan_file_handover(prev, profile)
        return note

    def _plan_file_path(self) -> Optional[str]:
        """本会话的计划文件路径（.lite-work/plans/<session>.md，gitignore 内不污染仓库）。

        仅当当前工作区就绪时返回；路径跨平台安全（session_id 已由 _safe_filename 同源
        规则约束，这里再做一层字符清理）。
        """
        workspace = self.app.workspace
        if not workspace:
            return None
        safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in self.kernel.session_id)[:80]
        if not safe or safe in (".", ".."):
            return None
        return os.path.join(workspace, ".lite-work", "plans", f"{safe}.md")

    def _plan_file_handover(self, prev_agent_id: str, current_profile) -> str:
        """计划文件交接提示（对齐 OpenCode：plan 留下的计划文件，接手方续用）。

        - 接手方可写 且 上一轮是 plan 且计划文件存在 →「执行其中定义的计划」；
        - 接手方是 plan（含首次进入该会话的 plan）：告知计划文件路径——
          已存在则提示可增量编辑，不存在则提示把规划产出写入该文件。
        """
        try:
            plan_path = self._plan_file_path()
            if not plan_path:
                return ""
            exists = os.path.isfile(plan_path)
            prev_is_plan = prev_agent_id == "plan"
            cur_is_plan = getattr(current_profile, "id", "") == "plan"
            if self._can_write(current_profile) and prev_is_plan and exists:
                return (f"\n\n计划文件：此前 Plan Agent 已将计划写入 `{plan_path}`，"
                        "你应当先读取该文件，并执行其中定义的计划。")
            if cur_is_plan:
                if exists:
                    return (f"\n\n计划文件：本会话已有计划文件 `{plan_path}`，"
                            "你可以用 plan_save 工具在其基础上更新"
                            "（注意：plan_save 是整体重写，增量请传入含原内容的完整版本）")
                return (f"\n\n计划文件：请用 plan_save 工具把本会话的规划产出写入计划文件 "
                        f"`{plan_path}`（Markdown 格式，含可执行步骤），"
                        "供后续切换到执行型 Agent 时直接落地。")
            return ""
        except Exception:
            logger.debug("[Task %s] 计划文件交接提示生成失败", self.task_id, exc_info=True)
            return ""

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
              reasoning_effort: Optional[str] = None,
              merge_worktree: Optional[str] = None) -> TaskHandle:
        task_id = uuid.uuid4().hex[:12]
        # 隔离工作树模式：会话开启时，任务在独立 worktree 中执行（复用已存在的）。
        # 例外：merge_worktree（AI 合并任务）必须落在**主工作区**——它要把分支
        # 合并回主分支并解决冲突，隔离执行会自我矛盾。
        task_workspace: Optional[str] = None
        worktree_name: Optional[str] = None
        # 会话快照只读一次（C2）：worktree 开关、历史消息、模型覆盖都取自同一份，
        # 避免同一任务里把 session 文件读 2~3 遍（大历史下是纯浪费的 I/O）
        snapshot = self.app.session_store.load(session_id)
        metadata: Dict[str, Any] = (snapshot.metadata if snapshot else None) or {}
        if merge_worktree:
            pass
        elif metadata.get("worktree"):
            mgr = self.app.worktree_manager()
            created = mgr.create(session_id)
            if created.get("ok"):
                task_workspace = created["path"]
                worktree_name = session_id
                if created.get("restored"):
                    logger.info("[Task] 已恢复会话 %s 的隔离工作树（分支 %s）",
                                session_id, created.get("branch"))
            else:
                # 既不能建也不能恢复（git 不可用/分支被删）→ 隔离模式失效：
                # 清除标记，避免"界面说隔离、任务其实在主工作区"的静默隔离丢失
                logger.warning("[Task] worktree 不可用，已关闭该会话的隔离模式: %s",
                               created.get("reason"))
                self.app.set_session_worktree(session_id, False)
        # 按 Agent 配置裁剪工具集（build 全量 / plan 只读 / 自定义）
        # workspace：隔离时所有工具插件以 worktree 为工作区构建
        registry = self.app.create_agent_registry(agent_id or "build", workspace=task_workspace)
        kernel = self.app.create_kernel(session_id, registry=registry,
                                        security_workspace=task_workspace)
        # 权限收敛（P3）：编排者身份挂 kernel——spawn_agent handler 读取其 profile，
        # 编排者 deny 的工具对子 Agent 强制 deny（子权限永不超过父）
        kernel.orchestrator_agent_id = agent_id or "build"
        # 多轮对话：加载该 session 已落盘的历史消息到上下文，
        # 避免每轮新建 kernel 时从空上下文开始、落盘覆盖上一轮对话
        if snapshot and snapshot.messages:
            # 顺手治愈历史中的悬空 tool_calls（旧版本/中断遗留）：补占位结果，
            # 否则每轮 repair 都丢弃该消息（丢上下文 + 反复击穿 prompt cache）
            kernel.ctx.messages = patch_dangling_tool_calls(list(snapshot.messages))
        model_override = metadata.get("model") or None
        if not isinstance(model_override, dict):
            model_override = None
        loop = self.app.create_loop(kernel, registry, agent_id=agent_id,
                                    model_override=model_override,
                                    reasoning_effort_override=reasoning_effort,
                                    workspace=task_workspace)
        loop.parallel_tool_calls = str(self.app.config.get("parallel_tool_calls", "auto")).lower()

        handle = TaskHandle(task_id, kernel, registry, loop, self.app)
        handle.agent_id = agent_id or "build"
        handle.workspace = task_workspace
        handle.worktree_name = worktree_name
        handle.merge_worktree = merge_worktree
        if merge_worktree:
            prompt = _merge_worktree_prompt(merge_worktree, prompt)
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
