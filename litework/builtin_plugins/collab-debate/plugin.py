# lite-work 协作模式插件：辩论（对抗收敛）
# 安装后在 设置 → 多智能体 → 协作模式 中选择启用；
# recipe.md 为派生指引（注入 spawn_agent 描述），可选实现
# on_agent_spawned / on_agent_complete / on_task_done 行为钩子。
from __future__ import annotations

from litework.orchestration.collab_policy import CollabModePlugin


class DebateCollabMode(CollabModePlugin):
    name = "collab-debate"
    version = "1.0.0"
    description = "协作模式：提案者与批判者多轮对抗审查，迭代修订至方案收敛（红蓝对抗）"
    mode_name = "debate"
    display_name = "辩论（对抗收敛）"
