# lite-work 协作模式插件：流水线（按序接力）
# 安装后在 设置 → 多智能体 → 协作模式 中选择启用；
# recipe.md 为派生指引（注入 spawn_agent 描述），可选实现
# on_agent_spawned / on_agent_complete / on_task_done 行为钩子。
from __future__ import annotations

from litework.orchestration.collab_policy import CollabModePlugin


class PipelineCollabMode(CollabModePlugin):
    name = "collab-pipeline"
    version = "1.0.0"
    description = "协作模式：顺序依赖的分工协作：每阶段一个 agent，前序产出交接给后续 agent 消费"
    mode_name = "pipeline"
    display_name = "流水线（按序接力）"
