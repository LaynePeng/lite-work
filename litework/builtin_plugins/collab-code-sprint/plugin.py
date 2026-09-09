# lite-work 协作模式插件：测试驱动接力（开发冲刺）
# 安装后在 设置 → 多智能体 → 协作模式 中选择启用；
# recipe.md 为派生指引（注入 spawn_agent 描述），可选实现
# on_agent_spawned / on_agent_complete / on_task_done 行为钩子。
from __future__ import annotations

from litework.orchestration.collab_policy import CollabModePlugin


class CodeSprintCollabMode(CollabModePlugin):
    name = "collab-code-sprint"
    version = "1.0.0"
    description = "协作模式：实现与测试 agent 配对循环、按模块领地并行（allowed_dirs 硬隔离）"
    mode_name = "code-sprint"
    display_name = "测试驱动接力（开发冲刺）"
