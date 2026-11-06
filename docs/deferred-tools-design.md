# 工具渐进式披露（P1）设计稿

> 背景：System Prompt 与 tool schema 的重复投喂已在 P0 消除（省 ~7.9k tokens/请求）。
> 剩下的最大开销是 **tool schema 本身常驻**：93 个工具 ≈ 28.2k tokens，其中
> `parameters` 占 75%（40.6k 字符），`description` 只占 25%。压缩描述收益有限，
> 必须从「少发工具」入手。
>
> 参考 pi 的 `prepareLoadout`：工具可以声明「我的声明不进这次请求」，模型通过
> 另一个工具（或提示）触达它。pi 的 `hiddenTools` 还会把这类工具从 System Prompt
> 的工具清单里一并省掉（`system-prompt.ts:150`）。

## 1. 为什么不直接 unregister

`ToolRegistry` 是多处共用的事实源：

- `agent_loop.py:1333` 压缩上下文时用 `registry.get_tools()` 与主循环对齐**缓存前缀**，
  两边工具集不一致会白丢 prompt cache；
- 约 30 处测试直接断言 `registry.get_tools()` 的成员（Plan 模式只读、子 Agent 白名单等）；
- 执行路径 `registry.execute()` 必须能找到工具，否则模型一轮调用就报「未注册」。

所以 **注册表保持完整**，只在「构成本次请求的 tools 列表」这一步做裁剪。

## 2. 分层

```
ToolRegistry          注册表：全量，语义不变（执行/存在性都靠它）
  └─ ToolLoadout      本次请求的可装载视图：deferred 组默认不发 schema
       ├─ 常驻组（core）：read/edit/execute/git/shell/todo/ask/... 全量发
       └─ 按需组（deferred）：office/email/astock/watch/multi-agent/ppt/...
                             只发组名 + 一行说明 + 组内工具名清单
```

模型需要某个按需工具时，调 `load_tools(group)` 装载该组；装载后**后续轮次**的
请求里出现完整 schema。已装载的组在本任务内保持（避免反复装卸导致缓存前缀抖动）。

## 3. 为什么按「组」而不是按「单工具」

- 单工具粒度会让元工具调用次数爆炸（一次任务可能要装 10 个）；
- 同组工具常一起用（office 写作流程 = docx_create + docx_read + ...）；
- 组的边界现成：`ToolPlugin.name`（`office-plugin` / `email-plugin` / …）。

## 4. 分组表

组由插件边界推导，不硬编码工具名（新增工具自动归组）：

| 组 | 来源插件 | 工具数 | schema 字符 | 历史调用 |
|---|---|---|---|---|
| `office` | office-plugin | 19 | 12733 | 14 |
| `multi-agent` | multi-agent-plugin | 10 | 4689 | 1 |
| `email` | email-plugin | 8 | 5816 | 44 |
| `stock` | astock-plugin | 19 | 20405 | 1191 |
| `watch` | watch-plugin | 6 | 3505 | 77 |
| `ppt` | ppt-edit-plugin | 1 | 1560 | 8 |
| `ocr` | ocr-plugin | 3 | 913 | 38 |
| … | … | | | |

**默认常驻（不进按需）**：filesystem / editor / shell / codebase / ast / git /
review / webfetch / todo / question / skills / wakeup —— 这些是任何任务都可能
用到的骨干预设。

**默认按需**：office / multi-agent / email / stock / watch / ppt / ocr。
理由：这些是「特定任务才用」的领域能力，且正是 schema 的大头（合计 ~49k 字符）。

`stock` 组特殊处理：它是当前使用频率最高的域（1191 次调用）。默认按需会
增加一次 `load_tools` 往返——但换来 20k 字符的常驻节省，且 `load_tools` 可以在
同一轮里和其它工具并行发（模型一次发多个 tool_call）。仍保留设置开关。

## 5. 缓存前缀稳定性

按需装载会改变 tools 列表 → 破坏 prompt cache。控制方式：

1. **单调装载**：本任务内只增不减，装载点平均 1~2 次，不是每轮抖动；
2. **System Prompt 里的组清单是静态的**（不随装载变化）——变化只发生在 tools
   数组，Anthropic 的 `cache_control` 打在最后一个 tool 上，装载后整体失效一次，
   可接受；
3. 装载事件 emit `tools:loaded`，前端工具面板可见。

## 6. 元工具

只加 **1 个**工具，避免用两个工具换十个工具的笑话：

```
load_tools(group: str) -> str
  装载一个按需工具组；返回该组工具清单（名称 + 一行摘要）
```

`group` 用 enum 限定取值，模型不会瞎猜。组清单与说明写在 System Prompt 的
「按需工具组」段（静态，几十字符/组）。

## 7. 落地位置

| 文件 | 改动 |
|---|---|
| `litework/tools/registry.py` | 新增 `ToolLoadout`（组定义 + 装载状态 + `select()`） |
| `litework/core/agent_loop.py` | 每轮请求前用 loadout 选工具；注册 `load_tools` |
| `litework/core/system_prompt.py` | 新增「按需工具组」段（组名 + 说明 + 工具名） |
| `litework/app.py` | 装配时把 plugin → group 映射传给 registry |

## 8. 风险与回退

- **风险**：模型不知道要装工具就完不成任务。缓解：System Prompt 明确写「需要
  office/email/stock 等能力时先 load_tools」，且组清单里列出组内工具名——模型
  看到工具名就知道该装哪个组。
- **回退开关**：设置项 `deferred_tools: false` 即全体常驻（当前行为），
  不改代码就能退回。

## 9. 本期不做

- 按单工具粒度装卸；
- 自动预测该装哪组（需要额外的意图分类，收益不抵复杂度）；
- 压缩路径（`agent_loop.py:1333`）——它仍用全量 schema，只影响压缩那一次调用，
  且保持一致反而更利于复用前缀。
