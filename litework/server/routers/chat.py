# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""聊天任务与交互审批：/api/chat、/api/tasks/*、/api/approve、/api/question。"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .context import ServerContext


class ChatRequest(BaseModel):
    session_id: str
    prompt: str
    task_id: Optional[str] = None
    agent_id: Optional[str] = None
    reasoning_effort: Optional[str] = None


class StopRequest(BaseModel):
    task_id: str


class ApproveRequest(BaseModel):
    approval_id: str
    approved: bool


class QuestionAnswerRequest(BaseModel):
    question_id: str
    answer: str


def create_router(ctx: ServerContext) -> APIRouter:
    router = APIRouter()
    app, tasks = ctx.app, ctx.tasks

    @router.post("/api/chat")
    async def chat(payload: ChatRequest, request: Request):
        ctx.check_auth(request)
        ctx.require_workspace()
        session_id = payload.session_id.strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id 不能为空")
        prompt = payload.prompt.strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt 不能为空")

        handle = tasks.active_for_session(session_id)
        if handle is not None:
            # 会话已有任务在跑：本次输入作为补充指令排队，下一回合注入对话
            handle.queue_input(prompt)
            return {"task_id": handle.task_id, "queued": True}
        handle = tasks.start(session_id, prompt, agent_id=payload.agent_id,
                               reasoning_effort=payload.reasoning_effort)
        return {"task_id": handle.task_id}

    @router.get("/api/tasks/{task_id}/events")
    async def task_events(task_id: str, request: Request):
        ctx.check_auth(request)
        handle = tasks.get(task_id)
        if handle is None:
            raise HTTPException(status_code=404, detail="任务不存在")

        # 每个连接独立订阅队列，避免断线重连窗口期新旧 reader 竞争
        queue = handle.subscribe()

        async def _stream():
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if item is None:
                        yield "data: [DONE]\n\n"
                        break
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            finally:
                handle.unsubscribe(queue)
                if handle.done and handle.subscribers_drained:
                    tasks.cleanup(task_id)
                handle.unsubscribe(queue)
                if handle.done and handle.subscribers_drained:
                    tasks.cleanup(task_id)

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @router.post("/api/tasks/{task_id}/stop")
    async def stop_task(task_id: str, request: Request):
        ctx.check_auth(request)
        ok = tasks.stop(task_id)
        if not ok:
            raise HTTPException(status_code=404, detail="任务不存在")
        return {"ok": True}

    @router.get("/api/tasks/background")
    async def list_background_tasks(request: Request):
        ctx.check_auth(request)
        return {"tasks": app.background_tasks()}

    @router.post("/api/tasks/background/{task_id}/kill")
    async def kill_background_task(task_id: str, request: Request):
        ctx.check_auth(request)
        return {"ok": app.kill_background_task(task_id)}

    @router.post("/api/approve")
    async def approve(payload: ApproveRequest, request: Request):
        ctx.check_auth(request)
        ok = app.approval_gate.resolve(payload.approval_id, payload.approved, by="user")
        if not ok:
            raise HTTPException(status_code=404, detail="审批请求不存在或已处理")
        return {"ok": True, "approved": payload.approved}

    @router.post("/api/question")
    async def answer_question(payload: QuestionAnswerRequest, request: Request):
        ctx.check_auth(request)
        ok = app.question_gate.resolve(payload.question_id, payload.answer, by="user")
        if not ok:
            raise HTTPException(status_code=404, detail="问题不存在或已回答")
        return {"ok": True, "answer": payload.answer}

    return router
