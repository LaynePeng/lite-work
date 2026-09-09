# lite-work 协作模式插件：互批（批判审查）
# 安装后在 设置 → 多智能体 → 协作模式 中选择启用；
# recipe.md 为派生指引（注入 spawn_agent 描述），可选实现
# on_agent_spawned / on_agent_complete / on_task_done 行为钩子。
from __future__ import annotations

from litework.orchestration.collab_policy import CollabModePlugin


class CriticCollabMode(CollabModePlugin):
    name = "collab-critic"
    version = "1.0.0"
    description = "协作模式：spawn role=critic 批判者审查方案或代码，产出按严重度排序的问题清单"
    mode_name = "critic"
    display_name = "互批（批判审查）"
