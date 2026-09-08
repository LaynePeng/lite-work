---
name: code-sprint
description: 软件开发冲刺：多 Agent 测试驱动接力——实现与测试配对循环、按模块领地并行
version: 1.0.0
triggers: 开发冲刺,多人开发,并行开发,测试驱动,code sprint,实现并测试
---

# 软件开发冲刺技能（多 Agent 特化）

软件工程的两种特化协作：**测试驱动接力**（实现 ⇄ 测试对抗循环）与
**模块领地并行**（按 write set 划分互不重叠的开发区）。

## 模式 A：测试驱动接力（单个功能，质量优先）

1. spawn 实现者（implementer，allowed_dirs 限定目标模块）：
   「实现 X 功能，写完后列出改动文件与自测情况」
2. 实现 agent 完成后（[agent:completed] 通知送达），spawn 测试者（tester）：
   「对 {改动文件清单} 运行测试并审查实现，输出：通过/失败用例、
   发现的问题（按严重度排序）」
3. 有问题 → followup_task 唤醒 implementer：「按以下问题清单修复：
   {问题}」→ 再 followup_task 唤醒 tester 复审
4. tester 输出 [全部通过] → close 两者，你整合交付

## 模式 B：模块领地并行（多模块，速度优先）

1. 把需求拆成 write set 互不重叠的模块（如：api/ + tests/、web/src/、docs/）
2. 每个 spawn 一个 implementer（mode="orchestrate"）：
   - **allowed_dirs 必须显式声明且互不相交**（硬隔离，物理上防冲突）
   - task 写明：模块边界、公共接口约定（先行确定！）、验收标准
3. 公共接口先由你（或单个 agent）定义并写入文件，各 implementer 读取遵守
4. 全部完成后 spawn 一个 tester 统一集成测试（followup 修复回归）

## 模式选择

- 单功能 / 核心逻辑 / 质量敏感 → A（接力）
- 多模块 / 互相独立 / 时间压力 → B（并行）
- 混合：核心模块走 A，周边模块走 B

## 纪律

- 公共接口/数据结构先定义再并行（最大的返工来源是接口漂移）
- implementer 的 allowed_dirs 是硬边界；要改边界外文件必须报告而非越界
- 测试 agent 永远只读代码 + 跑命令，不修实现（角色分离）
- close_agent 释放完成的 agent；进度卡住用 list_agents 检查 + send_message 督促
