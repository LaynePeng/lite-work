# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""协作模式策略层：配方（提示词指引）+ 行为钩子（on_agent_* 生命周期）。"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.types import Plugin

logger = logging.getLogger("litework.collab")

# 内置配方：模式选择指引（原 agent_tools.DELEGATION_GUIDE 外置至此）
DEFAULT_RECIPE = (
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


@dataclass
class CollabContext:
    """钩子收到的上下文（只读快照，钩子不得修改编排状态）。"""
    app: Any
    session_id: str
    # on_agent_spawned / on_agent_complete 时的 AgentRecord 快照（鸭子类型）
    record: Optional[Any] = None
    extras: Dict[str, Any] = field(default_factory=dict)


class CollabPolicy:
    """策略基类：钩子异常由内核隔离，不影响任务执行。"""

    name: str = "default"

    def recipe(self) -> str:
        """模式提示词配方（注入 spawn_agent 工具描述）。"""
        return DEFAULT_RECIPE

    async def on_agent_spawned(self, ctx: CollabContext) -> None:
        """子 Agent 派生成功后（返回前）调用。"""

    async def on_agent_complete(self, ctx: CollabContext) -> None:
        """子 Agent 进入终态（completed / errored / timeout）时调用。"""

    async def on_task_done(self, ctx: CollabContext) -> None:
        """主任务收尾（task:done / task:error 之后）调用。"""


class DefaultCollabPolicy(CollabPolicy):
    """默认策略：仅内置配方，无行为钩子。"""

    name = "default"


class ReviewCollabPolicy(CollabPolicy):
    """有文件改动的 agent 交付后注入 review 提醒。"""

    name = "review"

    async def on_agent_complete(self, ctx: CollabContext) -> None:
        record = ctx.record
        if record is None:
            return
        changed = list(getattr(record, "changed_files", None) or [])
        if not changed or getattr(record, "status", "") != "completed":
            return
        manager = ctx.app.agent_manager(ctx.session_id, create=False)
        if manager is None:
            return
        manager.notifications.append({
            "agent_id": getattr(record, "agent_id", ""),
            "nickname": getattr(record, "nickname", ""),
            "role": getattr(record, "role", "general"),
            "status": "review-pending",
            "summary": (f"[策略 review] 该 agent 改动了 {len(changed)} 个文件，"
                        f"收尾前请用 review_code 审查：{', '.join(changed[:8])}"),
            "changed_files": changed,
        })


class UserRecipeCollabPolicy(CollabPolicy):
    """自定义配方：collab_recipe 文本替换内置指引。"""

    name = "user-recipe"

    def __init__(self, recipe_text: str) -> None:
        self.recipe_text = recipe_text or DEFAULT_RECIPE

    def recipe(self) -> str:
        return self.recipe_text


# ---------------------------------------------------------------- 注册与解析

_BUILTIN_POLICIES: Dict[str, type] = {
    "default": DefaultCollabPolicy,
    "review": ReviewCollabPolicy,
}


def register_collab_policy(cls: type) -> type:
    """注册自定义策略类（第三方插件扩展点）。"""
    name = getattr(cls, "name", "")
    if not name:
        raise ValueError("CollabPolicy 子类必须定义非空 name")
    _BUILTIN_POLICIES[name] = cls
    return cls


def _resolve_mode(app, name: str) -> Optional[CollabPolicy]:
    """按模式名解析：已安装模式插件优先，其次内置/注册策略；未命中返回 None。"""
    for plugin in _installed_mode_plugins(app):
        if plugin.mode_name == name:
            return PluginCollabPolicy(plugin)
    cls = _BUILTIN_POLICIES.get(name)
    return cls() if cls is not None else None


def get_collab_policy(app, session_id: Optional[str] = None) -> CollabPolicy:
    """优先级：会话覆盖 > collab_recipe > collab_policy > default。"""
    try:
        # 1. 会话级覆盖（对话框协作模式选择器写入 session metadata）
        if session_id:
            snapshot = getattr(app, "session_store", None)
            if snapshot is not None:
                snap = snapshot.load(session_id)
                mode = (snap.metadata or {}).get("collab_mode") if snap else None
                if mode:
                    policy = _resolve_mode(app, str(mode))
                    if policy is not None:
                        return policy
        config = getattr(app, "config", {}) or {}
        # 2. 自定义配方文本
        recipe_text = str(config.get("collab_recipe") or "").strip()
        if recipe_text:
            return UserRecipeCollabPolicy(recipe_text)
        # 3. 模式名（内置策略 / 已安装模式插件）
        policy_name = str(config.get("collab_policy") or "default").strip() or "default"
        policy = _resolve_mode(app, policy_name)
        if policy is None:
            logger.warning("[Collab] 未知策略 %r，回退 default", policy_name)
            return DefaultCollabPolicy()
        return policy
    except Exception:
        logger.exception("[Collab] 策略解析失败，回退 default")
        return DefaultCollabPolicy()


def _installed_mode_plugins(app) -> List["CollabModePlugin"]:
    """生效中的协作模式插件（本地已安装 > 内置，同名覆盖），错误隔离。"""
    try:
        return app.collab_modes()
    except Exception:
        logger.debug("[Collab] 协作模式插件加载失败", exc_info=True)
        return []


def list_collab_modes(app) -> List[Dict[str, Any]]:
    """模式选择器数据源。source: builtin=内置 / plugin=本地覆盖（社区更新）。"""
    from ..tools.plugin_loader import builtin_plugins_root, find_plugin_icon

    modes: List[Dict[str, Any]] = [
        {"name": "default", "display_name": "默认（自动路由）",
         "description": "按任务特征自动路由（编排/流水线/头脑风暴/互批/辩论/会议/测试驱动接力）",
         "source": "builtin", "version": "", "icon_url": None},
        {"name": "review", "display_name": "审查门",
         "description": "有文件改动的子 Agent 交付后注入 review_code 审查提醒",
         "source": "builtin", "version": "", "icon_url": None},
    ]
    local_root = os.path.join(getattr(app, "config_dir", "") or "", "plugins")
    for plugin in _installed_mode_plugins(app):
        is_local = getattr(plugin, "_is_local_override", False)
        roots = (local_root, builtin_plugins_root()) if is_local else (builtin_plugins_root(),)
        has_icon = find_plugin_icon(plugin.name, *roots) is not None
        modes.append({
            "name": plugin.mode_name,
            "display_name": getattr(plugin, "display_name", "") or plugin.mode_name,
            "description": (getattr(plugin, "description", "") or "")[:200],
            "source": "plugin" if is_local else "builtin",
            "version": getattr(plugin, "version", "") or "",
            "icon_url": f"/api/collab/icon/{plugin.mode_name}" if has_icon else None,
        })
    return modes


# ---------------------------------------------------------------- 可安装协作模式（社区插件形态）

class CollabModePlugin(Plugin):
    """协作模式插件基类：社区仓库 lite-work-plugins 的 collab-* 包实现此类。

    与工具插件同一套安装/发现机制（~/.lite-work/plugins/），装好即在
    设置的协作模式选择器中出现。子类声明元信息 + 可选生命周期钩子；
    recipe 默认读插件目录的 recipe.md。
    """

    # 模式唯一标识（config.collab_policy 取值；与插件 name 解耦）
    mode_name: str = ""
    # 选择器显示名
    display_name: str = ""
    # hover 说明
    description: str = ""

    def recipe(self) -> str:
        """模式配方：默认读插件目录下的 recipe.md。"""
        try:
            plugin_file = inspect.getfile(type(self))
            recipe_path = Path(plugin_file).parent / "recipe.md"
            if recipe_path.is_file():
                return recipe_path.read_text(encoding="utf-8")
        except (OSError, TypeError):
            pass
        return DEFAULT_RECIPE

    # 生命周期钩子（与 CollabPolicy 同签名，可选覆盖）
    async def on_agent_spawned(self, ctx: "CollabContext") -> None:
        """子 Agent 派生成功后（返回前）调用。"""

    async def on_agent_complete(self, ctx: "CollabContext") -> None:
        """子 Agent 进入终态（completed / errored / timeout）时调用。"""

    async def on_task_done(self, ctx: "CollabContext") -> None:
        """主任务收尾（task:done / task:error 之后）调用。"""


class PluginCollabPolicy(CollabPolicy):
    """协作模式插件 → CollabPolicy 适配器（配方与钩子委托给插件实例）。"""

    def __init__(self, plugin: CollabModePlugin) -> None:
        self.plugin = plugin
        self.name = f"plugin:{plugin.mode_name}"

    def recipe(self) -> str:
        return self.plugin.recipe()

    async def on_agent_spawned(self, ctx: CollabContext) -> None:
        await self.plugin.on_agent_spawned(ctx)

    async def on_agent_complete(self, ctx: CollabContext) -> None:
        await self.plugin.on_agent_complete(ctx)

    async def on_task_done(self, ctx: CollabContext) -> None:
        await self.plugin.on_task_done(ctx)


# ---------------------------------------------------------------- 钩子安全调用

def _fire_and_forget(coro, hook_name: str, loop=None) -> None:
    """异步钩子 fire-and-forget：异常兜底记录，不进入内核调用栈。"""

    async def _wrapped():
        try:
            await coro
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[Collab] 策略钩子 %s 执行失败（已隔离）", hook_name)

    try:
        (loop or asyncio.get_running_loop()).create_task(_wrapped())
    except RuntimeError:
        # 无运行循环（极端时序）：丢弃钩子，不影响内核
        logger.debug("[Collab] 策略钩子 %s 无事件循环可调度", hook_name)


def fire_collab_hook(app, hook: str, ctx: CollabContext) -> None:
    """内核侧统一入口：解析策略并异步触发指定钩子。"""
    try:
        policy = get_collab_policy(app)
    except Exception:
        return
    handler = getattr(policy, hook, None)
    if handler is None or hook not in ("on_agent_spawned", "on_agent_complete", "on_task_done"):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 同步上下文无事件循环：不创建协程对象，避免 "coroutine was never
        # awaited" 资源告警（钩子本就无法调度，丢弃即可）
        logger.debug("[Collab] 策略钩子 %s 无事件循环可调度", hook)
        return
    _fire_and_forget(handler(ctx), f"{policy.name}.{hook}", loop)
