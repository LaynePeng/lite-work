# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""任务后反思 → 技能草稿（路线图 W10）。

W7 轨迹是原始数据，反思是提炼结论：任务完成后，基于轨迹（或会话消息尾部
退化）让 LLM 提炼「可复用的执行流程」，生成 SKILL.md 草稿落到个人区
`~/lite-work/personal/skill-drafts/`，由用户审阅后手动采纳（复制/移动到技能
目录）——**不自动生效**（反思是旁路增强，草稿永远只是建议）。

设计约束：
- 反思失败**静默**（记日志）：绝不影响任务收尾主流程；
- 默认关闭（reflection_enabled=false）：反思花 LLM 费用，用户显式开启；
- 无轨迹时退化用会话消息尾部（轨迹未开启的场景也能反思）；
- 草稿文件带任务标识与会话溯源，避免互相覆盖。
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("litework.reflection")

#: 反思开关的配置键（app.config）
REFLECTION_CONFIG_KEY = "reflection_enabled"

#: 草稿目录：个人区下的 skill-drafts
_DRAFTS_DIRNAME = "skill-drafts"

#: 退化的消息尾部条数（无轨迹时）
_FALLBACK_MESSAGES = 40

_REFLECT_SYSTEM = (
    "你是一个任务复盘专家。根据下面的任务执行轨迹，提炼这个任务的「可复用流程」。\n"
    "输出一份 Markdown 技能草稿（SKILL.md 风格），包含：\n"
    "1. 一句话描述（这个技能适合什么任务）；\n"
    "2. 触发条件（用户说什么话时适合用）；\n"
    "3. 分步流程（编号步骤，每步一个可验证的动作）；\n"
    "4. 注意事项（本次踩过的坑）。\n"
    "要求：只输出 Markdown 正文，不要输出多余解释；如果这个任务不值得沉淀为技能"
    "（过于琐碎/一次性），输出 NO_SKILL 一词即可。"
)


def drafts_dir(personal_workspace: str) -> str:
    """草稿目录（不创建；写入时 makedirs）。"""
    return os.path.join(personal_workspace, _DRAFTS_DIRNAME)


def _slugify(text: str, limit: int = 40) -> str:
    """任务目标 → 文件名安全 slug（中文保留、空白折叠、去掉路径分隔）。"""
    s = re.sub(r"[\r\n\t/\\:：?*\"<>|]+", " ", text or "").strip()
    s = re.sub(r"\s+", "-", s)
    return (s[:limit].strip("-") or "task")


def _trajectory_digest(app: Any, session_id: str, task_id_hint: str) -> Optional[str]:
    """轨迹 → 反思素材摘要；无轨迹返回 None。"""
    try:
        from .trajectory import list_session_trajectories, read_trajectory

        traj = list_session_trajectories(app.config_dir, session_id)
        if not traj:
            return None
        # 优先匹配 hint（本次任务的 task_id 前缀），否则取最新一条
        picked = None
        for t in traj:
            if task_id_hint and str(t.get("task_id", "")).startswith(task_id_hint):
                picked = t
                break
        if picked is None:
            picked = traj[-1]
        events = read_trajectory(app.config_dir, session_id,
                                 str(picked.get("task_id", "")), 0, 200)
        lines = []
        for ev in events:
            et = ev.get("type", "")
            if et == "tool_call":
                lines.append(f"- 工具 {ev.get('tool', '')}（{ev.get('outcome', '')}）"
                             + (f"：{ev.get('summary', '')}" if ev.get("summary") else ""))
            elif et == "outcome":
                lines.append(f"- 结果：{ev.get('verdict', '')}")
            elif et == "skill_exec":
                lines.append(f"- 技能 {ev.get('skill', '')}")
        return "\n".join(lines[:120]) or None
    except Exception:
        logger.debug("[Reflection] 轨迹读取失败，退化用消息尾部", exc_info=True)
        return None


def _messages_digest(app: Any, session_id: str) -> Optional[str]:
    """会话消息尾部 → 反思素材（轨迹未开启时的退化路径）。

    Message 为 dataclass（无 .get），role/content 用 getattr 取——快照里
    历史消息可能是任意形态，统一按属性读、异常跳过。
    """
    snap = app.session_store.load(session_id)
    if snap is None:
        return None
    lines = []
    for m in snap.messages[-_FALLBACK_MESSAGES:]:
        role = str(getattr(m, "role", "") or "")
        content = str(getattr(m, "content", "") or "").strip()
        if not content:
            continue
        if role == "user":
            lines.append(f"[用户] {content[:300]}")
        elif role == "assistant":
            lines.append(f"[助手] {content[:300]}")
    return "\n".join(lines) or None


async def run_reflection(app: Any, session_id: str, goal: str = "",
                         task_id_hint: str = "") -> Optional[str]:
    """任务完成后运行反思；成功返回草稿文件路径，否则 None。

    旁路纪律：任何异常都吞掉（记 debug 日志）——反思绝不阻塞任务收尾。
    """
    if not bool(app.config.get(REFLECTION_CONFIG_KEY, False)):
        return None
    try:
        digest = _trajectory_digest(app, session_id, task_id_hint)
        source = "trajectory"
        if not digest:
            digest = _messages_digest(app, session_id)
            source = "messages"
        if not digest:
            return None

        goal_text = goal or "（未设目标）"
        user_prompt = (
            f"任务目标：{goal_text}\n\n执行轨迹摘要（来源：{source}）：\n{digest}\n\n"
            "请提炼可复用流程，输出 SKILL.md 草稿；不值得沉淀则输出 NO_SKILL。"
        )
        from .types import Message
        content, _, _ = await app.adapter.chat_stream(
            [Message(role="system", content=_REFLECT_SYSTEM),
             Message(role="user", content=user_prompt)],
            [], None,
        )
        content = (content or "").strip()
        if not content or "NO_SKILL" in content[:40]:
            return None

        # 落盘到个人区草稿目录
        from .personal import PERSONAL_WORKSPACE
        out_dir = drafts_dir(PERSONAL_WORKSPACE)
        os.makedirs(out_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        header = (
            f"<!-- 反思草稿（W10）：{stamp} · 会话 {session_id} · 素材 {source} -->\n"
            f"<!-- 审阅后可将本文件移动到技能目录（~/.agents/skills/<名称>/SKILL.md）采纳 -->\n\n"
        )
        path = os.path.join(out_dir, f"{_slugify(goal)}-{stamp}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(header + content + "\n")
        logger.info("[Reflection] 技能草稿已落盘: %s", path)
        return path
    except Exception:
        logger.debug("[Reflection] 反思失败（旁路，不影响任务）", exc_info=True)
        return None
