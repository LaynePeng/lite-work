# lite-work 协作模式插件：编排-工人（并行派发）
# 安装后在 设置 → 多智能体 → 协作模式 中选择启用；
# recipe.md 为派生指引（注入 spawn_agent 描述），可选实现
# on_agent_spawned / on_agent_complete / on_task_done 行为钩子。
from __future__ import annotations

from litework.orchestration.collab_policy import CollabModePlugin


class OrchestrateCollabMode(CollabModePlugin):
    name = "collab-orchestrate"
    version = "1.0.0"
    description = "协作模式：并行派发互不依赖的子任务，主 Agent 继续关键路径工作，结果完成后自动送达"
    mode_name = "orchestrate"
    display_name = "编排-工人（并行派发）"
