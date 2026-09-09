# lite-work 协作模式插件：头脑风暴（多视角提案）
# 安装后在 设置 → 多智能体 → 协作模式 中选择启用；
# recipe.md 为派生指引（注入 spawn_agent 描述），可选实现
# on_agent_spawned / on_agent_complete / on_task_done 行为钩子。
from __future__ import annotations

from litework.orchestration.collab_policy import CollabModePlugin


class BrainstormCollabMode(CollabModePlugin):
    name = "collab-brainstorm"
    version = "1.0.0"
    description = "协作模式：多 Agent 不同视角并行提案、交叉批判后综合出最优方案"
    mode_name = "brainstorm"
    display_name = "头脑风暴（多视角提案）"
