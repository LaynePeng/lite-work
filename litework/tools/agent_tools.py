"""多 Agent 协作工具（Phase 1）：spawn_agent / list_agents / close_agent / wait_agents。

设计对齐 docs/multi-agent-design.md §3。协作模式（编排-工人 / 流水线 /
头脑风暴 / 互批）是这组原语之上的提示词配方——见工具描述中的使用指引。
spawn_sub_agent（同步语义）保留为兼容包装，逐步迁移到 spawn_agent。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from ..core.agent_loop import current_session_id
from ..core.types import ToolDefinition
from ..orchestration.agent_manager import current_agent_depth, extract_fork_messages
from ..tools.plugin import ToolPlugin

# 模式选择指引（写入工具描述：模型按任务特征自动路由，借鉴 Codex 委派策略 +
# Magentic-One 进度账本 + AutoGen 群聊会议）
DELEGATION_GUIDE = (
    "模式选择（按任务特征路由）：\n"
    "① 并行调研/互不依赖的子任务 → 编排-工人：spawn_agent 并行 + 继续自己的工作，结果自动送达；\n"
    "② 顺序依赖的分工（设计→实现→审查） → 流水线：按序 spawn，把前序 agent 的产出写进后续任务描述；\n"
    "③ 需要多样的方案/创意 → 头脑风暴：对同一问题 spawn 3+ 个不同视角/立场的 agent 并行提案，综合取舍；\n"
    "④ 方案或代码需要把关 → 互批：spawn role=critic 的批判者审查，产出问题清单；\n"
    "⑤ 高风险/争议决策 → 辩论：提案者与 critic 多轮对抗（send_message 传意见 + followup_task 唤醒修订）；\n"
    "⑥ 需要集体讨论达成共识 → 会议（meeting）：多 agent 围绕议题轮流发言、互相看到彼此观点后收敛；\n"
    "⑦ 软件开发任务 → 测试驱动接力：实现 agent 与测试 agent 配对（实现→测试→修复循环）。\n"
    "进度账本（长程任务纪律）：派发后每隔几步用 list_agents 检查各 agent 状态——"
    "卡住的 send_message 督促或 close 换人重派；全部完成后综合。\n"
    "通用纪律：先区分关键路径（自己做）与 sidecar（可并行）；任务具体、有界、自包含；"
    "并行写任务用 allowed_dirs 声明互不相交范围；wait_agents 仅在下一步被阻塞时使用；"
    "子任务需要当前上下文才能理解时用 fork_turns 继承（'all' 或最近 N 条）。"
)


def _manager(app, kernel=None):
    """当前会话的 SessionAgentManager。

    优先级：kernel.root_session_id（子 Agent 执行时指向主会话——孙 agent
    必须挂主会话 manager，否则脱离看板与通知注入）> current_session_id
    （主会话 loop 中执行时的 ContextVar）。
    """
    sid = getattr(kernel, "root_session_id", None) if kernel is not None else None
    if not sid:
        try:
            sid = current_session_id.get()
        except LookupError:
            return None, None
    if not sid:
        return None, None
    return app.agent_manager(str(sid)), sid


def _collab_gate(app) -> str:
    """治理档位（P3，对齐 Codex MultiAgentMode）：explicit=默认（用户明确要求才派生），
    proactive=主动委派。注入 spawn_agent 描述头部。"""
    mode = str(app.config.get("agent_collab_mode") or "explicit")
    if mode == "proactive":
        return ("主动委派已开启：当并行能节省时间或提升质量时，应主动派生子 Agent"
                "（用户可随时纠正，重活/急活优先自己做）。")
    return ("默认策略：仅当用户明确要求子 Agent、委派或并行工作时才派生；"
            "要求「深入/全面/多角度」不构成派生许可。")


def _role_registry_hint(app) -> str:
    """已注册的自定义 subagent 角色清单（P3：角色 description 进 spawn 提示）。"""
    try:
        rows = []
        for p in app.agent_registry.all().values():
            if p.mode in ("subagent", "all") and not p.hidden:
                desc = (p.description or "").strip()[:60]
                rows.append(f"{p.id}: {desc}" if desc else p.id)
        if not rows:
            return ""
        return "\n已注册的自定义角色（role 参数可直接使用）：" + "；".join(rows)
    except Exception:
        return ""


def make_agent_tool_handlers(app, parent_events=None, kernel=None):
    """绑定到具体 kernel 的六个工具 handler（create_kernel 重绑用）。

    kernel：fork_context 的消息源（spawn 时从当前会话消息链裁剪继承上下文）。
    """

    async def _spawn(args: Dict[str, Any]) -> str:
        task = str(args.get("task", "")).strip()
        if not task:
            return "[Error]: task 不能为空。"
        allowed_dirs = args.get("allowed_dirs")
        if allowed_dirs is not None and not isinstance(allowed_dirs, list):
            return "[Error]: allowed_dirs 必须是目录数组。"
        manager, _sid = _manager(app, kernel)
        if manager is None:
            return "[Error]: 无活动会话，无法派生 agent。"
        # 嵌套深度：主会话=0 → 派生 depth=1；子 Agent（handler 在子 loop 中重绑）
        # 经 ContextVar 读自身深度 → 派生 depth+1（manager 校验配置上限）
        depth = current_agent_depth.get() + 1
        # fork_context：从当前会话消息链裁剪继承上下文（none/all/N）
        fork_spec = str(args.get("fork_turns") or "none")
        fork_messages = None
        if kernel is not None and fork_spec != "none":
            fork_messages = extract_fork_messages(kernel.ctx.messages, fork_spec)
        # 物理隔离（P3）：worktree=独立 git worktree，完成后自动合并回主工作区
        isolation = str(args.get("isolation") or "shared")
        if isolation not in ("shared", "worktree"):
            isolation = "shared"
        # 权限收敛（P3）：编排者 deny 的工具对子 Agent 强制 deny
        parent_denies: List[str] = []
        if kernel is not None:
            oid = getattr(kernel, "orchestrator_agent_id", None)
            if oid:
                try:
                    prof = app.get_agent(oid)
                    parent_denies = [t for t, act in (prof.permissions or {}).items()
                                     if act == "deny"]
                except Exception:
                    pass
        result = await manager.spawn(
            task,
            role=str(args.get("role") or "general"),
            agent_name=args.get("agent_name"),
            allowed_dirs=[str(d) for d in allowed_dirs] if allowed_dirs else None,
            max_steps=int(args.get("max_steps") or 0),
            parent_events=parent_events,
            mode=str(args.get("mode") or "orchestrate"),
            model=str(args.get("model")) if args.get("model") else None,
            depth=depth,
            initial_messages=fork_messages,
            isolation=isolation,
            parent_denies=parent_denies,
        )
        if not result.get("ok"):
            return f"[Error]: {result.get('error')}"
        hint = (
            "。agent 在后台执行，你应继续推进自己的工作；"
            "结果完成后会以 [agent:completed] 通知自动送达，无需轮询。"
        ) if not allowed_dirs else (
            f"。写入范围限定：{', '.join(allowed_dirs)}；"
            "继续你的工作，完成通知会自动送达。"
        )
        fork_hint = f"；已继承当前上下文（{fork_spec}）" if fork_messages else ""
        return (
            f"[Agent 已派生] id={result['agent_id']} name={result['nickname']}"
            f" role={args.get('role') or 'general'}{fork_hint}{hint}"
        )

    async def _list(args: Dict[str, Any]) -> str:
        manager, _sid = _manager(app, kernel)
        if manager is None:
            return "[]"
        agents = manager.list_agents()
        if not agents:
            return "当前没有已派生的 agent。"
        return json.dumps(agents, ensure_ascii=False, indent=1)

    async def _close(args: Dict[str, Any]) -> str:
        manager, _sid = _manager(app, kernel)
        if manager is None:
            return "[Error]: 无活动会话。"
        result = await manager.close(str(args.get("agent_id", "")))
        if not result.get("ok"):
            return f"[Error]: {result.get('error')}"
        # 通知前端：agent 已关闭（agent:closed 事件 → Agents 看板移除卡片）
        bus = parent_events or (kernel.events if kernel is not None else None)
        if bus is not None:
            try:
                await bus.emit("agent:closed", {"agentId": result["agent_id"]})
            except Exception:
                pass
        return f"[Agent 已关闭] {result['agent_id']}（关闭前状态：{result['previous_status']}）"

    async def _wait(args: Dict[str, Any]) -> str:
        manager, _sid = _manager(app, kernel)
        if manager is None:
            return "[Error]: 无活动会话。"
        ids = args.get("agent_ids") or []
        if not isinstance(ids, list) or not ids:
            return "[Error]: agent_ids 不能为空。"
        result = await manager.wait(
            [str(i) for i in ids],
            timeout_ms=int(args.get("timeout_ms") or 120000),
        )
        if not result.get("ok"):
            return f"[Error]: {result.get('error')}"
        lines = []
        for aid, s in result["statuses"].items():
            lines.append(
                f"- {s['nickname']}（{s['role']}）[{s['status']}] {s['summary']}"
                + (f"（改动：{', '.join(s['changed_files'][:8])}）" if s.get("changed_files") else "")
            )
        head = "[Agent 等待结果]\n" + ("\n[注意] 部分agent仍在运行（超时）\n" if result.get("timed_out") else "\n")
        return head + "\n".join(lines)

    async def _send_message(args: Dict[str, Any]) -> str:
        manager, _sid = _manager(app, kernel)
        if manager is None:
            return "[Error]: 无活动会话。"
        result = await manager.send_message(
            str(args.get("agent_id", "")),
            str(args.get("message", "")),
            sender=str(args.get("sender") or "main-agent"),
        )
        if not result.get("ok"):
            return f"[Error]: {result.get('error')}"
        return f"[消息已发送] → {result['nickname']}：{result['delivered']}"

    async def _followup(args: Dict[str, Any]) -> str:
        manager, _sid = _manager(app, kernel)
        if manager is None:
            return "[Error]: 无活动会话。"
        task = str(args.get("task", "")).strip()
        if not task:
            return "[Error]: task 不能为空。"
        result = await manager.followup(
            str(args.get("agent_id", "")), task,
            parent_events=parent_events,
            max_steps=int(args.get("max_steps") or 0),
        )
        if not result.get("ok"):
            return f"[Error]: {result.get('error')}"
        extra = f"，送达滞留消息 {result['delivered_backlog']} 条" if result.get("delivered_backlog") else ""
        return (
            f"[Agent 已唤醒] {result['nickname']} 带着完整历史上下文继续执行新任务{extra}。"
            "完成后会自动通知。"
        )

    # -------- 共享任务池（去中心化认领：编排者建池，子 Agent 自领）--------

    async def _create_tasks(args: Dict[str, Any]) -> str:
        manager, _sid = _manager(app, kernel)
        if manager is None:
            return "[Error]: 无活动会话。"
        titles = args.get("titles") or []
        if not isinstance(titles, list) or not titles:
            return "[Error]: titles 不能为空。"
        deps = args.get("depends_on") or {}
        deps_by_title = {str(k): [str(x) for x in v] for k, v in deps.items()} \
            if isinstance(deps, dict) else {}
        created = manager.create_shared_tasks([str(t) for t in titles],
                                              deps_by_title=deps_by_title)
        dep_note = "；含依赖关系（前置未完成时认领被阻塞）" if any(
            t.get("depends_on") for t in manager.list_shared_tasks()) else ""
        return (
            f"[共享任务池已建立] {len(created)} 个任务{dep_note}。子 Agent 可通过 "
            "list_shared_tasks 查看并 claim_shared_task 认领（先到先得）；"
            "适合并行分工：spawn 多个 agent 后让它们自领。"
        )

    async def _list_tasks(args: Dict[str, Any]) -> str:
        manager, _sid = _manager(app, kernel)
        if manager is None:
            return "当前没有共享任务。"
        tasks = manager.list_shared_tasks()
        if not tasks:
            return "当前没有共享任务。"
        return json.dumps(tasks, ensure_ascii=False, indent=1)

    async def _claim_task(args: Dict[str, Any]) -> str:
        manager, _sid = _manager(app, kernel)
        if manager is None:
            return "[Error]: 无活动会话。"
        claimer = str(args.get("claimer") or "agent")
        result = manager.claim_shared_task(str(args.get("task_id", "")), claimer)
        if not result.get("ok"):
            return f"[Error]: {result.get('error')}"
        return f"[已认领] {result['id']}：{result['title']}。完成后请 complete_shared_task。"

    async def _finish_task(args: Dict[str, Any]) -> str:
        manager, _sid = _manager(app, kernel)
        if manager is None:
            return "[Error]: 无活动会话。"
        result = manager.finish_shared_task(str(args.get("task_id", "")),
                                            str(args.get("claimer") or "agent"))
        if not result.get("ok"):
            return f"[Error]: {result.get('error')}"
        return f"[任务完成] {result['id']} 已标记 done。"

    return {"spawn_agent": _spawn, "list_agents": _list,
            "close_agent": _close, "wait_agents": _wait,
            "send_message": _send_message, "followup_task": _followup,
            "create_shared_tasks": _create_tasks, "list_shared_tasks": _list_tasks,
            "claim_shared_task": _claim_task, "complete_shared_task": _finish_task}


class MultiAgentPlugin(ToolPlugin):
    """多 Agent 协作工具插件（Phase 1 原语层）。"""

    name = "multi-agent-plugin"
    version = "1.0.0"
    description = "多 Agent 协作：异步派生/查询/等待/关闭子 Agent"

    def __init__(self, app) -> None:
        self._app = app
        self._handlers = make_agent_tool_handlers(app)

    def get_tools(self) -> List[ToolDefinition]:
        gate = _collab_gate(self._app)
        roles = _role_registry_hint(self._app)
        return [
            ToolDefinition(
                name="spawn_agent",
                description=(
                    f"{gate}\n"
                    "异步派生一个子 Agent 并行执行任务，立即返回（不等待完成）。"
                    "结果完成后以 [agent:completed] 通知自动送达，无需轮询。"
                    f"{roles}\n{DELEGATION_GUIDE}"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "task": {"type": "string",
                                 "description": "具体、有界、自包含的子任务描述"},
                        "role": {"type": "string",
                                 "description": "角色：general（默认）/ explorer（只读调研）/"
                                                "tester（测试）/ critic（批判审查）/ refactor；"
                                                "或已注册的自定义 subagent id"},
                        "agent_name": {"type": "string",
                                       "description": "agent 昵称（可选，默认 角色名-序号）"},
                        "allowed_dirs": {"type": "array", "items": {"type": "string"},
                                         "description": "写入目录白名单（相对 workspace）。"
                                                        "声明后获得写文件能力且仅限这些目录；"
                                                        "不声明则按角色默认（explorer/critic 只读）。"
                                                        "并行写任务必须声明互不相交的范围"},
                        "max_steps": {"type": "integer",
                                      "description": "最大执行轮数（默认取配置，有封顶）"},
                        "model": {"type": "string",
                                  "description": "模型路由覆盖（可选）：'provider/model' 或裸 model。"
                                                 "探索/调研类子任务可指定更快的模型；默认跟随全局"},
                        "fork_turns": {"type": "string",
                                       "description": "上下文继承（可选）：'none'（默认）| 'all' | 数字字符串如 '6'"
                                                      "（继承最近 N 条）。子任务需要当前对话背景才能理解时使用；"
                                                      "自包含任务不要继承（省 token）"},
                        "isolation": {"type": "string",
                                      "description": "物理隔离（可选）：'shared'（默认，共享工作区，配合 "
                                                     "allowed_dirs 逻辑隔离）| 'worktree'（独立 git 工作树，"
                                                     "完成自动合并回主工作区；适合大改动/实验性重构；非 git 仓库自动降级）"},
                        "mode": {"type": "string",
                                 "description": "合作模式标记：orchestrate（默认，编排-工人）/"
                                                "pipeline（流水线：按序交接）/brainstorm（头脑风暴："
                                                "并行多视角提案）/debate（辩论：批判对抗）/"
                                                "meeting（会议：群聊共议）——按你选择的模式声明，"
                                                "供界面分组展示"},
                    },
                    "required": ["task"],
                },
            ),
            ToolDefinition(
                name="list_agents",
                description="列出当前会话已派生的 agent（id/昵称/角色/状态/任务摘要）",
                parameters={"type": "object", "properties": {}},
            ),
            ToolDefinition(
                name="close_agent",
                description="关闭一个 agent 释放并发额度（完成后建议关闭；运行中关闭会终止执行）",
                parameters={
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string", "description": "spawn_agent 返回的 id"},
                    },
                    "required": ["agent_id"],
                },
            ),
            ToolDefinition(
                name="wait_agents",
                description=(
                    "阻塞等待指定 agent 到达终态并取回结果。仅当下一步被其结果阻塞时使用；"
                    "完成通知会自动送达，大多数情况无需等待"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "agent_ids": {"type": "array", "items": {"type": "string"},
                                      "description": "要等待的 agent id 列表"},
                        "timeout_ms": {"type": "integer",
                                       "description": "超时毫秒数（默认 120000）"},
                    },
                    "required": ["agent_ids"],
                },
            ),
            ToolDefinition(
                name="send_message",
                description=(
                    "给一个 agent 发消息（agent 间合作）。运行中的 agent 在下一步开始时收到；"
                    "已完成的暂存，followup_task 唤醒时送达。用于：向同伴传达新信息/中间产出、"
                    "批判意见（互批）、补充要求"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string", "description": "目标 agent id"},
                        "message": {"type": "string", "description": "消息内容（可含产出/意见）"},
                        "sender": {"type": "string", "description": "发送者标识（子 Agent 发送时填自己的昵称）"},
                    },
                    "required": ["agent_id", "message"],
                },
            ),
            ToolDefinition(
                name="followup_task",
                description=(
                    "唤醒一个已完成的 agent 继续执行新任务（接力合作）：携带其全部历史上下文"
                    "与滞留消息。运行中的 agent 不可唤醒（改用 send_message）。适用于：流水线交接"
                    "（前序产出交付后续）、红蓝对抗多轮往返、方案迭代修改"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string", "description": "spawn_agent 返回的 id"},
                        "task": {"type": "string", "description": "后续任务描述（说明前序产出如何获取）"},
                        "max_steps": {"type": "integer", "description": "最大执行轮数（默认 12）"},
                    },
                    "required": ["agent_id", "task"],
                },
            ),
            ToolDefinition(
                name="create_shared_tasks",
                description=(
                    "建立共享任务池（批量声明可并行认领的工作单元）：spawn 多个 agent 后让它们"
                    "经 list_shared_tasks + claim_shared_task 自领分工（先到先得，认领即锁定），"
                    "无需你逐一指派。适合一批同构子任务（如分模块迁移）"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "titles": {"type": "array", "items": {"type": "string"},
                                   "description": "任务标题列表（每条应具体、有界、自包含）"},
                        "depends_on": {"type": "object",
                                       "description": "依赖图（可选）：{任务标题: [前置任务标题,...]}，"
                                                      "前置未完成时该任务认领被阻塞"},
                    },
                    "required": ["titles"],
                },
            ),
            ToolDefinition(
                name="list_shared_tasks",
                description="列出共享任务池（id/标题/状态/认领者）",
                parameters={"type": "object", "properties": {}},
            ),
            ToolDefinition(
                name="claim_shared_task",
                description="认领一个 pending 任务（原子锁定，先到先得）；完成后用 complete_shared_task 标记",
                parameters={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "任务 id（t001 形式）"},
                        "claimer": {"type": "string", "description": "你的昵称（默认 agent）"},
                    },
                    "required": ["task_id"],
                },
            ),
            ToolDefinition(
                name="complete_shared_task",
                description="把你认领的任务标记完成",
                parameters={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "任务 id"},
                        "claimer": {"type": "string", "description": "你的昵称（须与认领时一致）"},
                    },
                    "required": ["task_id"],
                },
            ),
        ]
    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        handler = self._handlers.get(name)
        if handler is None:
            return f"[Error]: 未知工具 {name}"
        return await handler(args)
