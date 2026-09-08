---
name: pipeline
description: 流水线协作：多 Agent 按序接力（设计→实现→审查），前序产出交接给后续 Agent
version: 1.0.0
triggers: 流水线,按序执行,接力,分阶段执行,pipeline
---

# 流水线技能（多 Agent 接力）

顺序依赖的分工协作：每个阶段一个 agent，前序产出交给后续 agent 消费。
适合「设计 → 实现 → 审查」「调研 → 撰写 → 校对」这类有明确交接顺序的任务。

## 编排流程

### 第 1 步：拆分阶段

把任务拆成 2-4 个阶段，明确每阶段的输入/输出物。示例：

1. designer：产出方案文档（输出：技术要点清单）
2. coder：按方案实现（输入：方案；输出：代码与改动文件清单）
3. critic：审查实现（输入：改动清单；输出：问题清单）

### 第 2 步：按序派生（不要并行！）

用 spawn_agent 逐个执行，spawn 时声明 mode="pipeline"：

- spawn designer → **wait_agents 等它完成**（顺序依赖，等待是必要的）
- 拿到 designer 的产出（wait 结果或 [agent:completed] 通知里的 summary）
- spawn coder，task 写明「方案要点：{designer 产出}」
- 以此类推

### 第 3 步：分支处理

- 某阶段产物不合格：followup_task 唤醒该阶段 agent 按反馈修订
- 后续阶段需要补充信息：send_message 直达（若在运行中）
- 阶段间有可并行的部分：混合使用（该段用 mode="orchestrate" 并行派发）

## 纪律

- 阶段数 2-4 为宜；超过 4 阶段说明任务该拆成多个主任务
- 每阶段交接物必须明确（方案/清单/代码/报告），不要只说"继续做"
- 纯顺序依赖必须 wait；可并行的部分不要串行浪费
- 完成后 close_agent 释放额度
