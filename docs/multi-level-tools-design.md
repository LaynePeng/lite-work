# 多级工具（Multi-Level Tools）设计稿

> 状态：设计 + 第一期实现（edit_files 复合工具 + 工具面板分级展示）。
> 背景：2026-10 性能诊断（`~/.lite-work/metrics/tool_events.jsonl` + 轨迹）显示
> 最重任务 4 个连续打满 100 轮，其中 59~72 次 apply_search_replace 基本一轮一个
> patch；每轮 5-15s 网络 + LLM 往返。根因是**工具粒度太细**——模型一轮只干一个
> 微操作。参考 Muse（Meta, 2026-09）的多级工具思路：低阶原语收进高阶复合工具，
> 模型一轮干完一个语义单元。

## 1. 分层模型

```
L1 复合工具（语义单元，一轮一次调用）
  edit_files     —— 多文件多补丁批量精确编辑（收编 apply_search_replace /
                    apply_unified_diff 的单文件单补丁用法）
  read_manual    —— 已有（富围栏手册按需查阅）
  verify         —— 待做：build+test+lint 编排（一个调用跑完验证三件套）
  search         —— 待做：AST/全文/文件树统一入口

L0 底层原语（保留注册，但 System Prompt 里不优先推荐）
  read_file / write_file / apply_search_replace / apply_unified_diff /
  execute_command / search_code / ...
```

**L0 不删除**：兼容既有提示词习惯、子 Agent 白名单（`ROLE_TOOLS`）、
权限域（`TOOL_DOMAIN`）。L1 是「更快的前门」，L0 是「万能的后门」。

## 2. edit_files 规格

```
edit_files({
  edits: [
    { filePath, mode: "search_replace"|"unified_diff"|"write",
      search?, replace?, diff?,          # 按 mode 取参
      content?,                          # mode=write 时
      then?: [cmd, ...]                  # 可选：全部成功后验证命令
    }, ...
  ],
  stop_on_error: true   # 默认 true：某条失败即停止（原子性弱化版）
})
```

- 返回：每条编辑的结果列表（成功/失败 + 错误信息），失败的带原因；
- 语义：**先全部校验（search 块存在性检查）再依次应用**——校验阶段失败的
  条目直接标记，不落盘半截；
- 权限：归入 `TOOL_DOMAIN` 的 `edit` 域（与 apply_search_replace 同域，
  deny edit 的 Agent 同样拿不到它）；
- WRITE_TOOLS：加入 `agent_loop.WRITE_TOOLS`（写类串行语义不变）。

## 3. 工具面板分级展示（前端）

`/api/tools` 响应增加 `domain` 字段（后端从 `TOOL_DOMAIN` 查）；ToolsPanel
按域分组渲染：

```
需要关注（失败工具）         ← 不变
───
编辑（4）                    ← 域分组，组头带说明
  edit_files  23·100%
  apply_search_replace 401·98%
执行（2）
  execute_command 1029·99%
阅读（10）▸（折叠）
…
```

- 域顺序：编辑 → 执行 → 阅读 → Git → 联网 → Office → 协作 → 交互 → 其他；
- 每组默认显示前 4 个（按调用量降序），组头点击折叠/展开全部；
- 复合工具（edit_files）带 `★` 徽标 + tooltip「复合工具：一次调用完成多个操作」。

## 4. 性能预期

| 指标 | 现状 | 预期 |
| --- | --- | --- |
| 重任务轮数 | 100（打满） | ~30-40（编辑轮合并） |
| 每轮延迟 | 5-15s | 不变，但轮数少 → 总时长 ÷3 |
| System Prompt 工具 schema | ~40 工具全量 | L1 精简描述（长描述在 execute 报错时给） |

## 5. 未做（二期）

- verify 复合工具（build/test/lint 编排）；
- L0 从 schema 收编（需评估各模型对工具数减少的敏感度）；
- 子 Agent 角色白名单同步引入 L1。
