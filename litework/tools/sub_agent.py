"""spawn_sub_agent 工具处理器（第9课（MCP协议）/ 第11课（多Agent协作）编排能力封装为可调用 Tool）。"""
from __future__ import annotations

from typing import Any, Dict


def make_sub_agent_handler(app, parent_events=None):
    async def _handler(args: Dict[str, Any]) -> str:
        task = args.get("taskDescription", "").strip()
        if not task:
            return "[Error]: taskDescription 不能为空。"
        role = args.get("roleType") or "general"
        if role == "explore":
            role = "explorer"
        if role not in ("explorer", "tester", "refactor", "general"):
            role = "general"

        result = await app.sub_agent_runner.run_task(task, role=role, parent_events=parent_events)
        status = "SUCCESS" if result["completed"] else "FAILED"
        return (
            f"[Sub-Agent 执行报告]\n"
            f"状态: {status}\n"
            f"角色: {result['role']}\n"
            f"轮数: {result['turns']}\n"
            f"Token 消耗: {result['total_tokens_used']}\n"
            f"报告:\n{result['summary']}"
        )

    return _handler
