# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""观察打包（Observation Pack，SoL-Pi C23 机制的 lite-work 适配）。

解决的问题是：大工具结果（长日志 / 大文件读取 / 命令回显）会在后续每一轮
请求里被**整段重发**，而它们只在产生后的头几轮里有决策价值。

机制（三条纪律对齐 SoL-Pi 开源实现）：
1. 投影层改写：只改「发请求用的消息投影」，session 存档逐字节不动 →
   与上下文压缩、会话恢复、prompt 缓存全部兼容；
2. 先全文后占位：同一条结果在前 FULL_SENDS 次请求里全文发送（保证模型
   第一时间看到完整证据），之后替换为稳定占位符；原文在占位前归档落盘，
   Agent 用 obs_recall 工具按字节分页召回；
3. fail-open：任何打包/归档失败都回退原文，绝不因省 token 丢失证据。

无状态设计：某条结果已被发送过几次，用「它后面有多少条 assistant 消息」
推算（SoL-Pi 同款技巧），跨会话恢复/重启都正确，不需要额外持久化状态。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .types import Message

THRESHOLD_BYTES = 24_000       # 超过该字节数的纯文本工具结果才参与打包
FULL_SENDS = 3                 # 前 N 次请求全文发送，之后替换为占位符
EXCERPT_BYTES = 1_200          # 占位符保留的原文头部字节数
RECALL_MAX_BYTES = 16 * 1024   # obs_recall 单页字节上限
RECALL_MAX_LINES = 400         # obs_recall 单页行数上限
REDUCE_THRESHOLD_BYTES = 20_000  # 证据收据：超过该字节数才值得调小模型压缩
PLACEHOLDER_MARK = "[obs:"     # 占位符标记（用于幂等判断）

LEDGER_NAME = "ledger.jsonl"


@dataclass
class Observation:
    obs_id: str
    tool_name: str
    content: str
    bytes: int
    lines: int
    tokens: int


@dataclass
class RecallChunk:
    text: str
    bytes: int
    lines: int
    next_offset: int
    eof: bool


def observations_dir(truncation_dir: Optional[str]) -> Optional[str]:
    """观察归档根目录；truncation_dir 未配置时返回 None（打包整体停用）。"""
    if not truncation_dir:
        return None
    return os.path.join(truncation_dir, "observations")


def _count_bytes(text: str) -> int:
    return len(text.encode("utf-8", errors="replace"))


def estimate_tokens(text: str) -> int:
    """粗估 tokens（与 TokenCounter 同量级的 4 字符/token 启发式，仅供台账）。"""
    return max(1, _count_bytes(text) // 4)


def observation_id(content: str) -> str:
    return "obs_" + hashlib.sha1(content.encode("utf-8", errors="replace")).hexdigest()[:12]


def is_placeholder(content: str) -> bool:
    return isinstance(content, str) and content.lstrip().startswith(PLACEHOLDER_MARK)


def is_pure_text_result(message: Message) -> bool:
    """可打包的候选：工具结果消息、纯文本内容。"""
    return (
        message.role == "tool"
        and isinstance(message.content, str)
        and bool(message.content)
    )


def placeholder_for(obs: Observation) -> str:
    """稳定占位符：保留原文头部摘录 + 召回指引。同一内容每次生成完全一致。"""
    excerpt = obs.content[:EXCERPT_BYTES]
    return (
        f"{PLACEHOLDER_MARK}{obs.obs_id}] 工具 {obs.tool_name} 的大结果已归档"
        f"（{obs.bytes:,} bytes / {obs.lines} 行，全文前 {EXCERPT_BYTES} 字节如下）。\n"
        f"--- 摘录 ---\n{excerpt}\n--- 摘录结束 ---\n"
        f"如需继续阅读原文，请调用 obs_recall(id=\"{obs.obs_id}\", offset=…) 分页读取；"
        f"不需要时请不要召回，节省上下文。"
    )


def archive_observation(root: str, obs: Observation) -> str:
    """原文归档（幂等：同一 id 同一内容，重复写无害）。"""
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, f"{obs.obs_id}.txt")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(obs.content)
    return path


def archive_text(root: str, content: str, tool_name: str = "tool") -> Optional[Observation]:
    """便捷归档：任意文本 → 计算 id/规模 → 落盘。root 为 None 时返回 None。"""
    if not root or not content:
        return None
    size = _count_bytes(content)
    obs = Observation(
        obs_id=observation_id(content),
        tool_name=tool_name,
        content=content,
        bytes=size,
        lines=content.count("\n") + 1,
        tokens=estimate_tokens(content),
    )
    archive_observation(root, obs)
    return obs


def read_recall_chunk(root: str, obs_id: str, offset: int = 0) -> RecallChunk:
    """按字节偏移读取归档原文的一页（带硬上限，防止单次召回撑爆上下文）。"""
    path = os.path.join(root, f"{obs_id}.txt")
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(RECALL_MAX_BYTES)
    # 不截断 UTF-8 多字节字符
    text = data.decode("utf-8", errors="ignore")
    if len(text.split("\n")) > RECALL_MAX_LINES:
        text = "\n".join(text.split("\n")[:RECALL_MAX_LINES])
    consumed = _count_bytes(text)
    next_offset = offset + consumed
    eof = next_offset >= os.path.getsize(path)
    return RecallChunk(text=text, bytes=consumed, lines=text.count("\n") + 1 if text else 0,
                       next_offset=next_offset, eof=eof)


def append_ledger(root: str, event: Dict) -> None:
    """节省台账（jsonl 追加）：每次全文/占位决策都可审计。"""
    try:
        os.makedirs(root, exist_ok=True)
        event = {"ts": round(time.time(), 3), **event}
        with open(os.path.join(root, LEDGER_NAME), "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 台账失败不影响主流程


def _observation_of(message: Message) -> Optional[Observation]:
    content = message.content or ""
    size = _count_bytes(content)
    if size < THRESHOLD_BYTES:
        return None
    return Observation(
        obs_id=observation_id(content),
        tool_name=message.name or "tool",
        content=content,
        bytes=size,
        lines=content.count("\n") + 1,
        tokens=estimate_tokens(content),
    )


def project_observations(
    messages: List[Message], root: Optional[str]
) -> Tuple[List[Message], int, int]:
    """投影：返回 (投影后消息, 本次节省的 tokens, 本次替换的结果数)。

    输入消息链不被修改（返回新列表）；无 root / 无候选时原样返回。
    发送计数是无状态的：某条结果后面有多少条 assistant 消息，就代表它已经
    被多少次请求携带过（SoL-Pi 同款技巧，跨会话恢复/重启都正确）。
    """
    if not root or not messages:
        return messages, 0, 0

    projected = list(messages)
    assistant_after = [0] * len(messages)
    assistant_count = 0
    for i in range(len(messages) - 1, -1, -1):
        assistant_after[i] = assistant_count
        if messages[i].role == "assistant":
            assistant_count += 1

    saved = 0
    packed = 0
    for i, message in enumerate(messages):
        if not is_pure_text_result(message) or is_placeholder(message.content or ""):
            continue
        try:
            obs = _observation_of(message)
            if obs is None:
                continue
            sends = assistant_after[i]
            if sends < FULL_SENDS:
                append_ledger(root, {"event": "full", "id": obs.obs_id,
                                     "sends": sends, "tool": obs.tool_name,
                                     "original_tokens": obs.tokens})
                continue
            placeholder = placeholder_for(obs)
            placeholder_tokens = estimate_tokens(placeholder)
            removed = max(0, obs.tokens - placeholder_tokens)
            archive_observation(root, obs)
            append_ledger(root, {"event": "placeholder", "id": obs.obs_id,
                                 "sends": sends, "tool": obs.tool_name,
                                 "original_tokens": obs.tokens,
                                 "placeholder_tokens": placeholder_tokens,
                                 "removed_tokens": removed})
            projected[i] = Message(
                role=message.role, name=message.name,
                tool_call_id=message.tool_call_id, content=placeholder,
            )
            saved += removed
            packed += 1
        except Exception:
            # fail-open：任何打包失败都保留原文
            continue
    return projected, saved, packed
