# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""``lite-work acp`` 入口：把 lite-work 作为 ACP Agent 跑在 stdio 上。

装配要点（与 server/tasks.py 的同构部分必须一致，否则 ACP 里的行为会和
Web UI 里不一样，用户会觉得「同一个 Agent 怎么换个界面就不对」）：

1. ``AgentApp`` 承载配置/LLM/工具/权限；
2. 每个 ACP 会话一个 lite-work session，**首次 prompt 时**才装配 kernel
   （session/new 只是登记，此时用户可能还没发任何消息，提前装整套插件纯属浪费）；
3. 同一会话后续 turn 复用已加载的消息历史，否则第二轮会从空上下文开始。

关键约束：ACP 的 stdout 只能是合法 JSON-RPC。因此本模块进入服务循环前
必须先把 stdout 改道（``StdioTransport.capture_stdout``），**且要在
AgentApp 构造之前**——插件 import 期就可能 print。
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, Optional, Tuple

from ..acp.agent import AcpAgent
from ..acp.transport import StdioTransport

logger = logging.getLogger("litework.acp")


def run_acp(config_dir: Optional[str] = None, workspace: Optional[str] = None,
            log_level: str = "info") -> int:
    """跑 ACP Agent 主循环，直到 stdin EOF。返回进程退出码。"""
    from .. import cli as cli_mod  # 延迟导入：避免 cli ↔ acp 循环依赖

    transport = StdioTransport()
    # 先把 stdout 让给协议：必须在任何业务对象构造之前。
    # 日志仍走 stderr（_configure_logging 里 StreamHandler 默认就是 stderr），
    # 协议要求 stderr 可自由写。
    with transport.capture_stdout():
        log_path = cli_mod._configure_logging(log_level, config_dir)
        logger.info("ACP 模式启动（日志: %s）", log_path)
        try:
            return asyncio.run(_serve(transport, config_dir, workspace))
        except KeyboardInterrupt:
            return 0


async def _serve(transport: StdioTransport, config_dir: Optional[str],
                 workspace: Optional[str]) -> int:
    # 必须在事件循环**内部**接 stdio：connect_read_pipe 需 running loop
    await transport.use_standard_streams()
    runner = _LoopRunner(config_dir, workspace)
    agent = AcpAgent(
        transport,
        runner.build_loop,
        history_loader=runner.load_history,
        permission_relay=runner.permission_relay,
    )
    agent.register()
    try:
        await transport.pump()
    finally:
        await runner.close()
    return 0


class _LoopRunner:
    """按 ACP 会话装配 AgentLoop，并记住每个会话的历史。"""

    def __init__(self, config_dir: Optional[str], workspace: Optional[str]) -> None:
        self.config_dir = config_dir
        self.workspace = workspace
        self._app: Optional[Any] = None
        #: ACP sessionId → lite-work session_id（复用同一个会话文件）
        self._session_map: Dict[str, str] = {}

    def _app_instance(self) -> Any:
        if self._app is None:
            from ..app import AgentApp
            # 工作区：ACP 客户端在 session/new 里给 cwd，这里先给个占位，
            # 真正的工作区在 build_loop 时按会话 cwd 覆盖。
            self._app = AgentApp(
                workspace=self.workspace,
                config_dir=self.config_dir,
            )
        return self._app

    def build_loop(self, acp_session_id: str, cwd: str):
        app = self._app_instance()
        session_id = self._session_map.get(acp_session_id)
        if session_id is None:
            session_id = acp_session_id
            self._session_map[acp_session_id] = session_id

        # 会话工作区以 ACP 客户端给的 cwd 为准（这就是编辑器里打开的项目）。
        # 不设全局 app.workspace：ACP 可能同时开多个不同项目窗口。
        task_workspace = os.path.abspath(cwd) if cwd else (app.workspace or os.getcwd())

        registry = app.create_agent_registry(
            "build", workspace=task_workspace, session_id=session_id)
        kernel = app.create_kernel(session_id, registry=registry,
                                   security_workspace=task_workspace,
                                   plugins_workspace=task_workspace)
        # 多轮：把已落盘的历史加载进上下文，否则每轮都从空开始、还会覆盖
        # 上一轮对话（与 server/tasks.py 完全一致的坑）
        snapshot = app.session_store.load(session_id)
        if snapshot is not None and snapshot.messages:
            from ..core.context_manager import patch_dangling_tool_calls
            kernel.ctx.messages = patch_dangling_tool_calls(list(snapshot.messages))

        loop = app.create_loop(kernel, registry, agent_id="build",
                               workspace=task_workspace)
        loop.parallel_tool_calls = str(
            app.config.get("parallel_tool_calls", "auto")).lower()
        loop.completion_gate = str(
            app.config.get("completion_gate", "advisory")).lower()
        return loop

    def load_history(self, session_id: str):
        """session/load 用：读已落盘的消息供重放。

        不装配 kernel——重放只需要历史数据，此时用户还没发新消息，
        提前装整套插件纯属浪费（与 session/new 同一原则）。
        """
        app = self._app_instance()
        snapshot = app.session_store.load(session_id)
        if snapshot is None:
            return []
        return list(snapshot.messages)

    def permission_relay(self, approval_id: str, approved: bool) -> None:
        """把编辑器里的用户决定写回审批门。

        必须走 app.approval_gate.resolve —— 工具的 await 挂在那上面的 future，
        不回填就会一直挂到 600s 超时被拒（用户看到的是「Agent 卡住」）。
        """
        app = self._app_instance()
        app.approval_gate.resolve(approval_id, approved, by="acp")

    async def close(self) -> None:
        if self._app is not None:
            try:
                await self._app.close()
            except Exception:  # noqa: BLE001
                logger.debug("[ACP] 关闭 AgentApp 失败", exc_info=True)


__all__ = ["run_acp"]
