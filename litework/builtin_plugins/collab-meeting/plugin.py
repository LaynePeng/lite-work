# lite-work 协作模式插件：会议（群聊共识）
# 安装后在 设置 → 多智能体 → 协作模式 中选择启用；
# recipe.md 为派生指引（注入 spawn_agent 描述），可选实现
# on_agent_spawned / on_agent_complete / on_task_done 行为钩子。
from __future__ import annotations

from litework.orchestration.collab_policy import CollabModePlugin


class MeetingCollabMode(CollabModePlugin):
    name = "collab-meeting"
    version = "1.0.0"
    description = "协作模式：多 Agent 围绕议题轮流发言、互相看到彼此观点，多轮后收敛共识"
    mode_name = "meeting"
    display_name = "会议（群聊共识）"
