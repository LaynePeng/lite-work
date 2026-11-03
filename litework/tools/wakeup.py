# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""schedule_wakeup：会话给自己设定时唤醒（一次性「复活」）。

## 为什么需要它

长程任务经常不是「一步做完」，而是**做完这一段落、等外部条件成熟再继续**：
等 CI 跑完、等行情开盘、等对方回邮件、等部署生效。此前模型只能：

- 让用户「过 10 分钟再叫我」——人必须回来；
- 或占着一个 Agent 轮次空转等待——烧 token 且阻塞会话；
- 或建一条 interval 计划——但它会**反复跑**，而需求只是一次。

结果就是「时间触发的任务根本没法做」。本工具让会话自己给自己排一次唤醒：
到点后以**原会话**为目标起一个新任务（prompt 由模型自己写好），会话带着
完整历史接着干活。

## 与盯梢计划的区别（刻意复用同一套存储与调度）

- 复用 `ScheduleStore` + `WatchScheduler`：跨 Core 触发租约、catch_up 补跑、
  运行历史、通知总线全部白拿，不必另造一套定时器；
- `schedule_mode="once"` + `run_at` 绝对时间戳，跑完自动 enabled=False；
- `origin="session_wakeup"` 标记来源，便于：
  1. 列出/取消时只动**本会话自己**设的唤醒（不误删用户建的盯梢计划）；
  2. 限额（`MAX_PER_SESSION`）——否则模型可以无限排唤醒把自己叫醒，
     等于绕过 max_steps 的守卫。

## 权限

工具归 `interactive` 域（与 todo_write / ask_user 同类）：它**不改用户文件、
不发网络请求**，只是给宿主排一个自己的闹钟。无人值守档 read / read+web 都
包含 interactive，因此盯梢任务也能给自己的下一段排期（这正是「任务链」）。
真正的写/执行能力仍由目标任务自己的权限档决定，不会因本工具而放宽。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from ..core.types import ToolDefinition
from .plugin import ToolPlugin

logger = logging.getLogger("litework.tools.wakeup")

#: 每个会话同时存在的唤醒上限。防止模型无限排闹钟自我唤醒（绕过步数守卫）。
MAX_PER_SESSION = 5

#: 最短唤醒延迟（秒）。太短等于在当前轮次里空转，没有意义。
MIN_DELAY_SECONDS = 30

#: 最长唤醒延迟（秒）：7 天。更远的排期应该用常驻计划，而不是一次性唤醒。
MAX_DELAY_SECONDS = 7 * 86400

#: 本工具创建的计划的来源标记
ORIGIN = "session_wakeup"


def _fmt_local(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    return time.strftime("%m-%d %H:%M:%S", time.localtime(ts))


def _parse_absolute(text: str, now: float) -> float:
    """把绝对时间文本解析成本地时间戳；解析失败返回 0。

    支持三种粒度（缺的部分按「下一个该时刻」补，不交给 strptime 的 1900 默认值）：

    - `YYYY-MM-DD HH:MM[:SS]`（也接受 `T` 分隔）——完整时间，最可靠；
    - `MM-DD HH:MM`——当年，已过则按明年；
    - `HH:MM[:SS]`——今天，已过则明天。

    **刻意不支持自然语言**（「明天早上」）：时区/节假日歧义太大，解析错会
    静默在错误的时间唤醒，比直接报错更糟。
    """
    import re

    now_lt = time.localtime(now)

    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?", text)
    if m:
        y, mo, d, hh, mi, ss = (int(x) if x else 0 for x in m.groups())
        try:
            return time.mktime((y, mo, d, hh, mi, ss, 0, 0, -1))
        except (ValueError, OverflowError):
            return 0.0

    m = re.fullmatch(r"(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
    if m:
        mo, d, hh, mi, ss = (int(x) if x else 0 for x in m.groups())
        try:
            ts = time.mktime((now_lt.tm_year, mo, d, hh, mi, ss, 0, 0, -1))
        except (ValueError, OverflowError):
            return 0.0
        # 已过 → 明年（跨年时月份仍合法，只是年份 +1）
        return ts if ts > now else time.mktime(
            (now_lt.tm_year + 1, mo, d, hh, mi, ss, 0, 0, -1))

    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
    if m:
        hh, mi, ss = (int(x) if x else 0 for x in m.groups())
        try:
            ts = time.mktime((now_lt.tm_year, now_lt.tm_mon, now_lt.tm_mday,
                              hh, mi, ss, 0, 0, -1))
        except (ValueError, OverflowError):
            return 0.0
        return ts if ts > now else ts + 86400

    return 0.0


def _parse_when(args: Dict[str, Any]) -> tuple:
    """把参数解析成 (触发时间戳, 错误信息)。两者恰有一个非空。

    两种写法：
    - `delay_seconds`：相对延迟（模型最常用的「20 分钟后」）；
    - `at`：绝对时间，支持 `YYYY-MM-DD HH:MM[:SS]`（本地时区）或纯时间戳。

    刻意**不**接受自然语言（「明天早上」）——时区/节假日歧义太大，
    解析错会静默在错误的时间唤醒，比直接报错更糟。
    """
    raw_at = args.get("at")
    delay = args.get("delay_seconds")
    now = time.time()

    if raw_at not in (None, ""):
        if isinstance(raw_at, (int, float)):
            ts = float(raw_at)
        else:
            ts = _parse_absolute(str(raw_at).strip(), now)
            if ts <= 0:
                return 0.0, (f"at 无法解析：{raw_at!r}。请用 YYYY-MM-DD HH:MM 格式"
                             "（本地时区），或改用 delay_seconds")
        delta = ts - now
    elif delay not in (None, ""):
        try:
            delta = float(delay)
        except (TypeError, ValueError):
            return 0.0, "delay_seconds 必须是数字（秒）"
    else:
        return 0.0, "需要 delay_seconds（多少秒后）或 at（YYYY-MM-DD HH:MM）之一"

    if delta < MIN_DELAY_SECONDS:
        return 0.0, (f"唤醒延迟至少 {MIN_DELAY_SECONDS} 秒（当前 {int(delta)} 秒）："
                     "更短的等待请直接在本轮内完成，不必排唤醒")
    if delta > MAX_DELAY_SECONDS:
        return 0.0, (f"唤醒延迟最多 {MAX_DELAY_SECONDS // 86400} 天："
                     "更远的排期请让用户建一条常驻的盯梢 / 定时计划")
    return now + delta, ""


def _store(app: Any) -> Any:
    return getattr(app, "schedule_store", None)


def _reschedule(app: Any, schedule_id: str) -> None:
    """通知调度器重算到期（失败不影响计划已落盘这个事实）。"""
    scheduler = getattr(app, "watch_scheduler", None)
    if scheduler is None:
        return
    try:
        scheduler.reschedule(schedule_id)
    except Exception:  # noqa: BLE001 - 下一 tick 会自动带出来
        logger.debug("[wakeup] 调度器重算失败（下次自动生效）", exc_info=True)


def _mine(store: Any, session_id: str) -> List[Any]:
    """本会话设的、仍待触发的唤醒（run 完的 enabled=False，自然不在其中）。"""
    out = []
    for sched in store.all():
        if getattr(sched, "origin", "") != ORIGIN:
            continue
        if (getattr(sched, "session_id", "") or "") != session_id:
            continue
        if not getattr(sched, "enabled", True):
            continue
        out.append(sched)
    out.sort(key=lambda s: getattr(s, "run_at", 0) or 0)
    return out


def make_wakeup_handler(app: Any, session_id: str):
    """构造 schedule_wakeup 工具 handler（绑定**发起会话**的 id）。"""

    async def _handler(args: Dict[str, Any]) -> str:
        action = str(args.get("action") or "set").strip().lower()
        store = _store(app)
        if store is None:
            return "[Error]: 宿主未提供计划存储，无法排唤醒（旧版本或非正常启动）。"
        sid = (session_id or "").strip()
        if not sid:
            return "[Error]: 无活动会话，无法排唤醒。"

        # ---------------------------------------------------------- list
        if action == "list":
            items = _mine(store, sid)
            if not items:
                return "当前会话没有待触发的唤醒。"
            lines = ["| 计划 | 触发时间 | 剩余 | 备注 |", "| --- | --- | --- | --- |"]
            now = time.time()
            for s in items:
                left = max(0, int((getattr(s, "run_at", 0) or 0) - now))
                lines.append(
                    f"| {getattr(s, 'name', '') or s.id} | {_fmt_local(getattr(s, 'run_at', None))} "
                    f"| {left // 60} 分 | {getattr(s, 'prompt', '')[:40]} |")
            return "\n".join(lines)

        # ---------------------------------------------------------- cancel
        if action == "cancel":
            target = str(args.get("schedule_id") or "").strip()
            mine = _mine(store, sid)
            if not target:
                return ("[Error]: 需要 schedule_id（先用 action=list 查看）。"
                        "为安全起见不支持「一次取消全部」。")
            hit = next((s for s in mine if s.id == target), None)
            if hit is None:
                return (f"[Error]: 本会话没有 id={target} 的待触发唤醒。"
                        "（只能取消自己设的唤醒，用户建的盯梢计划不在此列）")
            # 直接删除而不是置 enabled=False：取消就是「这条不存在了」。
            # 置停用会把已取消的唤醒留在计划列表里，与「跑完即删」也不一致。
            if not store.delete(hit.id):
                return f"[Error]: 取消唤醒「{getattr(hit, 'name', '') or hit.id}」失败。"
            _reschedule(app, hit.id)
            return f"已取消并删除唤醒「{getattr(hit, 'name', '') or hit.id}」。"

        if action != "set":
            return f"[Error]: 未知 action={action!r}（支持 set / list / cancel）"

        # ---------------------------------------------------------- set
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return ("[Error]: prompt 不能为空——它是**唤醒后你要做什么**的完整指令。"
                    "请写成不依赖当前对话也能看懂的一段任务描述。")

        mine = _mine(store, sid)
        if len(mine) >= MAX_PER_SESSION:
            names = "、".join(f"{getattr(s, 'name', '') or s.id}" for s in mine)
            return (f"[Error]: 本会话已有 {len(mine)} 个待触发唤醒（上限 {MAX_PER_SESSION}）："
                    f"{names}。请先 action=cancel 取消不再需要的，或复用它。")

        run_at, err = _parse_when(args)
        if err:
            return f"[Error]: {err}"

        from ..watch.schedule_store import Schedule

        name = str(args.get("name") or "").strip() or f"唤醒 {_fmt_local(run_at)}"
        # workspace 必须显式带上：调度器对「计划无 workspace」会回退到**当前**
        # 工作区，而多窗口下那个值可能已经切到别的项目——唤醒后的任务就会在
        # 错误目录里跑。这里钉死发起时的工作区。
        workspace = str(getattr(app, "workspace", "") or "")
        sched = Schedule(
            name=name,
            enabled=True,
            schedule_mode="once",
            run_at=run_at,
            session_id=sid,
            # 结果投递回同一个会话：会话自唤醒天生就是「回到我这里」，
            # 不需要用户再指定主会话。
            main_session_id=sid,
            workspace=workspace,
            prompt=prompt,
            source="",
            # 权限档：唤醒后的任务是**用户任务的延续**，不是无人值守的盯梢。
            # 用 read+web 作为保守默认（能给联网查证，但不给写/执行）——
            # 需要写盘的续跑应由用户在场时继续，或由常驻计划承担。
            permissions="read+web",
            notify=["in_app"],
            catch_up="once",
            origin=ORIGIN,
        )
        try:
            store.upsert(sched)
        except Exception as exc:  # noqa: BLE001 - 写盘失败要如实报错
            logger.warning("[wakeup] 写入唤醒计划失败: %s", exc, exc_info=True)
            return f"[Error]: 写入唤醒计划失败：{type(exc).__name__}: {exc}"
        _reschedule(app, sched.id)

        left = int(run_at - time.time())
        return (
            f"[OK] 已排唤醒「{name}」：约 {left // 60} 分钟后（{_fmt_local(run_at)}）"
            f"在本会话继续执行。\n"
            f"- 计划 id：`{sched.id}`（可用 action=list 查看 / action=cancel 取消）\n"
            f"- 唤醒后的任务：{prompt[:120]}\n"
            f"- 注意：唤醒任务按无人值守权限档 `read+web` 运行（可读可联网，不改文件）；"
            f"应用未运行时会在下次启动后补跑一次。"
        )

    return _handler


def get_wakeup_tool() -> ToolDefinition:
    return ToolDefinition(
        name="schedule_wakeup",
        description=(
            "给自己排一次「定时唤醒」：到点后在本会话自动起一个新任务继续干活。\n"
            "适用：需要等外部条件成熟再继续的场景——CI 跑完、行情开盘、对方回邮件、"
            "部署生效、N 分钟后再查一次结果。\n"
            "不要用它做「短等待」（<30 秒请在本轮内直接完成），也不要用它做「反复轮询」"
            "（那应该建常驻盯梢计划）。\n"
            "prompt 会被写入计划并在唤醒时作为任务指令，必须**自带上下文**——"
            "写成不依赖当前对话也能看懂的一段话（唤醒时会加载本会话历史，但明确写出目标更可靠）。\n"
            "action=set 排唤醒 / list 查看待触发 / cancel 取消（按 schedule_id）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["set", "list", "cancel"],
                    "description": "set 排唤醒（默认）/ list 列出本会话待触发的唤醒 / cancel 取消",
                },
                "delay_seconds": {
                    "type": "number",
                    "description": "相对延迟（秒），至少 30，最多 7 天。与 at 二选一",
                },
                "at": {
                    "type": "string",
                    "description": "绝对时间 YYYY-MM-DD HH:MM（本地时区），或 HH:MM（今天该时刻，已过则明天）。与 delay_seconds 二选一",
                },
                "prompt": {
                    "type": "string",
                    "description": "set 必填：唤醒后要做的事（自带上下文的完整任务描述）",
                },
                "name": {
                    "type": "string",
                    "description": "唤醒的名称（可选，便于 list 里辨认）",
                },
                "schedule_id": {
                    "type": "string",
                    "description": "cancel 必填：要取消的唤醒计划 id（来自 action=list）",
                },
            },
            "required": ["action"],
        },
    )


class WakeupPlugin(ToolPlugin):
    """schedule_wakeup 工具插件：会话给自己排定时唤醒。

    与 QuestionPlugin 同一模式：install() 注册一个**占位 handler**，
    `create_kernel` 拿到真实 kernel 后把 handler 重设成绑定该会话 id 的版本
    （wakeup 必须知道「谁在排唤醒」，否则会把所有会话的唤醒混在一起）。

    与 QuestionPlugin 的区别：这里连占位 handler 都要求 session_id，
    因为「不知道是哪个会话」时排出来的计划没有落点（唤醒后没有会话可回）。
    """

    name = "wakeup-plugin"
    version = "1.0.0"
    description = "定时唤醒：会话给自己排一次「到点后继续干」的闹钟"

    def __init__(self, app: Any, session_id: Optional[str] = None) -> None:
        self._app = app
        self._session_id = session_id or ""

    def get_tools(self) -> List[ToolDefinition]:
        return [get_wakeup_tool()]

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        # 未绑定会话（如启动引导 kernel / 元信息读取）时直接拒绝，
        # 而不是排一条 session_id 为空的计划——那种计划唤醒后无处落地。
        if not self._session_id:
            return ("[Error]: 当前上下文没有活动会话，无法排唤醒。"
                    "请在对话中调用本工具。")
        handler = make_wakeup_handler(self._app, self._session_id)
        return await handler(args)
