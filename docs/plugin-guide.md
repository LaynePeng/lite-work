# lite-work 插件开发指南

> 本文面向想给 lite-work 写插件（新工具、新协作模式）的开发者。
> 所有机制对应仓库当前代码：`litework/tools/plugin.py`（插件基类）、
> `litework/tools/plugin_loader.py`（加载器）、`litework/orchestration/collab_policy.py`（协作模式）。

## 1. 插件体系总览

lite-work 采用 **Cordis 风格插件架构**（空间解耦）：内核 `Kernel` 只保留
事件总线、中间件管道和服务容器，一切具体能力都由插件在 `install()` 时挂进来。
内置的 36 个工具、安全审批、目录隔离，与用户插件走**完全相同的通道**。

| 插件类别 | 基类 | 作用 |
|---|---|---|
| 工具插件 | `litework.tools.plugin.ToolPlugin` | 给 Agent 注册可调用的工具 |
| 协作模式插件 | `litework.orchestration.collab_policy.CollabModePlugin` | 多 Agent 协作玩法（进模式选择器） |

两者共用同一套安装 / 发现 / 治理机制（安装、版本、覆盖回退、删除）。

## 2. 安装位置与加载机制

插件放到用户插件目录即可，**无需修改应用代码**：

```text
~/.lite-work/plugins/
├── my_tool.py                 # 单文件插件
└── my_pack/                   # 目录插件（推荐）
    ├── plugin.py              # 入口（或 __init__.py）
    ├── recipe.md              # 协作模式插件的配方（可选）
    ├── icon.svg               # 插件图标（svg/png/jpg/webp/gif，可选）
    ├── requirements.txt       # 开发态依赖（可选）
    └── wheels/*.whl           # 打包态依赖（可选，推荐）
```

加载规则（`plugin_loader.load_plugins`）：

1. 扫描 `~/.lite-work/plugins/`，单文件 `.py` 或目录内 `plugin.py` / `__init__.py`；
2. 动态 import 模块，找出所有 **`Plugin` 子类**（排除基类自身）并实例化；
3. 目录插件先装依赖：
   - `wheels/*.whl`：解压到插件 `libs/` 并加入 `sys.path`（**打包态唯一可用方式**，
     pip download 到 wheels 目录即可离线分发）；
   - `requirements.txt`：仅开发态 pip 安装（打包态 backend.exe 无 pip，会告警跳过）；
4. 错误隔离：单个插件加载失败只记日志，不影响其他插件和内核启动；
5. 每个任务装配 kernel 时重新安装插件，安装/删除后无需重启 Core，
   下一条消息即可生效（内部通过插件实例缓存失效实现）。

优先级：**用户插件 > 内置插件**。同名 `name` 的用户插件会覆盖内置版，
卸载用户版后自动回退内置版。

> 注意：插件名安全校验只允许 `[A-Za-z0-9._-]`，目录/文件名不要用中文或空格。

## 3. 写一个工具插件

### 3.1 最小示例

```python
# ~/.lite-work/plugins/dice.py
from __future__ import annotations

from typing import Any, Dict, List

from litework.core.types import ToolDefinition
from litework.tools.plugin import ToolPlugin


class DicePlugin(ToolPlugin):
    name = "dice-plugin"          # 全局唯一；与内置同名即可覆盖内置
    version = "1.0.0"             # semver，空串表示跟随主应用版本
    description = "掷骰子：roll_dice 生成随机数"

    def get_tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="roll_dice",
                description="掷一次骰子，返回 1-6 的随机整数",
                parameters={
                    "type": "object",
                    "properties": {
                        "sides": {"type": "number", "description": "面数，默认 6"},
                    },
                    "required": [],
                },
            ),
        ]

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        import random
        sides = int(args.get("sides") or 6)
        return f"🎲 {random.randint(1, sides)}"
```

把文件放进 `~/.lite-work/plugins/` 即完成安装。要点：

- **`name`**：插件唯一标识（覆盖内置靠它）；**`version` / `description`**：展示在
  设置 → 插件页签；
- **`get_tools()`**：返回工具定义列表，`parameters` 是标准 JSON Schema，
  会直接进 LLM 的 tools 字段——description 写得越清楚，模型调用越准；
- **`execute(name, args)`**：工具执行入口，返回**字符串**给模型消费；
  长结果会被内核的截断器 + 观察打包自动归档，无需自己处理；
- **`removed_tools: List[str]`**（可选）：声明要移除的内置/其他插件工具名，
  先删后注册（不受 Agent 工具裁剪策略影响）。

### 3.2 工具执行的安全边界

工具执行前后会经过内核的三条洋葱管道（`Kernel.before_tool` /
`after_tool` / `before_llm`），安全插件（SecurityPlugin）在 `before_tool`
做三级风险判决：

- **SAFE**（读文件、搜索等）直接放行；
- **MEDIUM** 记录审计；
- **HIGH**（写文件、执行命令、删除等）走 Web 审批卡（HITL）。

你的工具如果操作文件/执行命令，建议：

1. 路径一律相对 workspace 解析并校验不越界（参考 `tools/filesystem.py`
   的 `resolve()`：`os.path.abspath` + 前缀检查）；
2. 长耗时操作支持超时与可中止；
3. 遵循工具描述约定（明确的 name、简洁准确的 description、
   完整的 JSON Schema），模型才能正确调用。

### 3.3 在插件里访问内核能力

注意：事件总线与中间件管道是 **Kernel 的属性**，不是可注入服务；
`get_service()` 真正能拿到的服务只有 `tools` / `tool_filter` / `app` /
`security_guard` / `question_gate` 等。拦截工具执行 + 监听事件的正确写法：

```python
from litework.core.types import Plugin

class MyGuardPlugin(Plugin):
    name = "my-guard"

    def install(self, kernel) -> None:
        # ① 事件总线：kernel.events 属性（TypedEventBus）
        kernel.events.on("tool:after_execute",
                         lambda p: print("tool done:", p["toolName"]))

        # ② 洋葱中间件：挂在 kernel.before_tool / after_tool / before_llm 管道上
        @kernel.before_tool.use
        async def block_rm(ctx, data, next):
            # before_tool 的 data 是 {"toolName", "args", "cancel", "reason"}
            if data.get("toolName") == "execute_command" and "rm -rf" in str(data.get("args", {})):
                data["cancel"] = True
                data["reason"] = "[MyGuardPlugin]: 危险命令已拦截"
            return await next(data)
```

取消执行用 `data["cancel"] = True` + `reason`（不要抛异常），与 SecurityPlugin
同一约定。事件负载字段以 `core/events.py` 的 TypedDict 为准（如
`tool:after_execute` 是 `toolName` / `durationMs` / `callId` / `status`）。

`kernel.get_service("app")` 可拿到 `AgentApp` 装配层（工作区、配置、会话存储等）。

## 4. 写一个协作模式插件

协作模式决定多 Agent 派生的「队形」与生命周期钩子，装好即出现在
**设置 → 多智能体 → 协作模式** 选择器中。参考实现见
`litework/builtin_plugins/collab-*`（头脑风暴 / 辩论 / 会议 / 流水线 /
互批 / 编排 / 测试接力，全部是同一形态的插件）。

```python
# ~/.lite-work/plugins/collab-pair-review/plugin.py
from litework.orchestration.collab_policy import CollabModePlugin


class PairReviewMode(CollabModePlugin):
    name = "collab-pair-review"       # 插件包标识
    version = "1.0.0"
    description = "结对审查：实现者与审查者配对，改完代码必须互审"
    mode_name = "pair-review"         # 模式唯一标识（config.collab_policy 取值）
    display_name = "结对审查"          # 选择器显示名

    async def on_agent_complete(self, ctx) -> None:
        """可选钩子：子 Agent 完成后注入提醒（fire-and-forget，异常自动隔离）。"""
        record = ctx.record
        changed = list(getattr(record, "changed_files", None) or [])
        if changed and getattr(record, "status", "") == "completed":
            manager = ctx.app.agent_manager(ctx.session_id, create=False)
            if manager is not None:
                manager.notifications.append({
                    "agent_id": getattr(record, "agent_id", ""),
                    "summary": f"[结对审查] 改动了 {len(changed)} 个文件，请用 review_code 审查",
                    "changed_files": changed,
                })
```

目录内附 `recipe.md` 作为**模式配方**（不写则回退内置指引）——
它会注入 `spawn_agent` 工具的描述，告诉主 Agent 这种模式下该按什么队形派子 Agent：

```markdown
# ~/.lite-work/plugins/collab-pair-review/recipe.md
结对审查模式：
① 每个实现类任务 spawn 一个实现 agent；
② 实现完成后必须再 spawn 一个 role=critic 的审查 agent，把改动文件列表传给它；
③ 审查通过才算完成；审查发现的问题回传给实现者修订（followup_task 唤醒）。
```

可覆盖的三个生命周期钩子（签名同 `CollabPolicy`）：

| 钩子 | 时机 |
|---|---|
| `on_agent_spawned(ctx)` | 子 Agent 派生成功后（返回前） |
| `on_agent_complete(ctx)` | 子 Agent 进入终态（completed / errored / timeout） |
| `on_task_done(ctx)` | 主任务收尾（task:done / task:error 之后） |

`ctx` 是 `CollabContext`（只读快照）：`app` / `session_id` / `record`
（AgentRecord：agent_id、role、status、changed_files、summary…）。

类继承判定决定分流：`CollabModePlugin` 子类自动归入协作模式（不注册工具），
`ToolPlugin` 子类归入工具插件，与安装入口无关。

## 5. 配套生态扩展点（简表）

| 扩展点 | 机制 |
|---|---|
| MCP 工具 | `mcp_servers` 配置 stdio server，工具以 `mcp_<服务>_<工具>` 注册，默认需审批 |
| 技能 | `skills/` 目录放 SKILL.md，索引常驻 System Prompt，`load_skill` 按需加载 |
| 自定义 Agent | 描述 + 系统提示词 + 工具白名单（allowed/exclude/deny），设置页可视化配置 |

不需要写 Python 的小能力优先用技能（SKILL.md）或 MCP，工具插件适合
需要注册新工具供模型直接调用的场景。

## 6. 调试与发布

1. **看日志**：`~/.lite-work/logs/lite-work.log`（后端）。加载失败会记
   `[PluginLoader] 加载插件模块 xxx 失败: ...`；
2. **验证注册**：`GET /api/tools` 返回当前工具集，确认你的工具在列；
3. **测试**：参考 `tests/test_office_tools.py` 中对 `load_plugins(tmp_path)`
   的测法——把插件写进临时目录再调用加载器断言实例与工具名；
4. **社区发布**：目录插件打成 zip / 推到插件仓库（manifest.json 根格式），
   用户经「检查社区更新」安装；打包分发请自带 `wheels/*.whl`
   （`pip download <pkg> -d wheels/`），打包态无法 pip install。
