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
from ..tools.plugin import ToolPlugin

# 委派策略提示（借鉴 Codex spawn_agent 描述，写入工具 description 供模型学习）
DELEGATION_GUIDE = (
    "使用指引：先区分关键路径（阻塞的下一步自己做）与 sidecar（可并行委派）任务；"
    "委派任务必须具体、有界、自包含，不与主任务重叠；并行写任务用 allowed_dirs "
    "声明互不相交的写入范围；派生后继续做自己的工作，仅在下一步被阻塞时 wait_agents；"
    "头脑风暴：对同一问题 spawn 多个不同视角的 agent 后综合；"
    "互批：spawn role=critic 的批判者审查方案或代码。"
)


def _manager(app):
    """当前会话的 SessionAgentManager（工具执行期经 ContextVar 定位会话）。"""
    sid = None
    try:
        sid = current_session_id.get()
    except LookupError:
        return None, None
    if not sid:
        return None, None
    return app.agent_manager(str(sid)), sid


def make_agent_tool_handlers(app, parent_events=None):
    """绑定到具体 kernel.events 的四个工具 handler（create_kernel 重绑用）。

    异步 spawn 的 progress 转发依赖 parent bus；callId 经 ContextVar 快照
    传给子任务（spawn_agent 卡片关联）。
    """

    async def _spawn(args: Dict[str, Any]) -> str:
        task = str(args.get("task", "")).strip()
        if not task:
            return "[Error]: task 不能为空。"
        allowed_dirs = args.get("allowed_dirs")
        if allowed_dirs is not None and not isinstance(allowed_dirs, list):
            return "[Error]: allowed_dirs 必须是目录数组。"
        manager, _sid = _manager(app)
        if manager is None:
            return "[Error]: 无活动会话，无法派生 agent。"
        result = await manager.spawn(
            task,
            role=str(args.get("role") or "general"),
            agent_name=args.get("agent_name"),
            allowed_dirs=[str(d) for d in allowed_dirs] if allowed_dirs else None,
            max_steps=int(args.get("max_steps") or 12),
            parent_events=parent_events,
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
        return (
            f"[Agent 已派生] id={result['agent_id']} name={result['nickname']}"
            f" role={args.get('role') or 'general'}{hint}"
        )

    async def _list(args: Dict[str, Any]) -> str:
        manager, _sid = _manager(app)
        if manager is None:
            return "[]"
        agents = manager.list_agents()
        if not agents:
            return "当前没有已派生的 agent。"
        return json.dumps(agents, ensure_ascii=False, indent=1)

    async def _close(args: Dict[str, Any]) -> str:
        manager, _sid = _manager(app)
        if manager is None:
            return "[Error]: 无活动会话。"
        result = await manager.close(str(args.get("agent_id", "")))
        if not result.get("ok"):
            return f"[Error]: {result.get('error')}"
        return f"[Agent 已关闭] {result['agent_id']}（关闭前状态：{result['previous_status']}）"

    async def _wait(args: Dict[str, Any]) -> str:
        manager, _sid = _manager(app)
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
        manager, _sid = _manager(app)
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
        manager, _sid = _manager(app)
        if manager is None:
            return "[Error]: 无活动会话。"
        task = str(args.get("task", "")).strip()
        if not task:
            return "[Error]: task 不能为空。"
        result = await manager.followup(
            str(args.get("agent_id", "")), task,
            parent_events=parent_events,
            max_steps=int(args.get("max_steps") or 12),
        )
        if not result.get("ok"):
            return f"[Error]: {result.get('error')}"
        extra = f"，送达滞留消息 {result['delivered_backlog']} 条" if result.get("delivered_backlog") else ""
        return (
            f"[Agent 已唤醒] {result['nickname']} 带着完整历史上下文继续执行新任务{extra}。"
            "完成后会自动通知。"
        )

    return {"spawn_agent": _spawn, "list_agents": _list,
            "close_agent": _close, "wait_agents": _wait,
            "send_message": _send_message, "followup_task": _followup}


class MultiAgentPlugin(ToolPlugin):
    """多 Agent 协作工具插件（Phase 1 原语层）。"""

    name = "multi-agent-plugin"
    version = "1.0.0"
    description = "多 Agent 协作：异步派生/查询/等待/关闭子 Agent"

    def __init__(self, app) -> None:
        self._app = app
        self._handlers = make_agent_tool_handlers(app)

    def get_tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="spawn_agent",
                description=(
                    "异步派生一个子 Agent 并行执行任务，立即返回（不等待完成）。"
                    "结果完成后以 [agent:completed] 通知自动送达，无需轮询。"
                    f"{DELEGATION_GUIDE}"
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
                                      "description": "最大执行轮数（默认 12）"},
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
        ]
    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        handler = self._handlers.get(name)
        if handler is None:
            return f"[Error]: 未知工具 {name}"
        return await handler(args)
