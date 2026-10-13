# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""盯梢 / 定时计划的持久化与「触发租约」。

两个独立职责（刻意放一起，便于对照阅读）：

1. **ScheduleStore**：`~/.lite-work/schedules.json` 的读写。原子写（tmp + rename）
   照搬 `tools/shell.py` 的 BackgroundRegistry 做法——写失败一律吞掉，绝不因为
   持久化问题影响调度运行。

2. **租约（lease）**：**每开一个窗口 = 独立 Electron + 独立 Core**（见
   `electron/main.js` 顶部注释），多个 Core 会各自跑调度器。同一计划到点时，
   必须保证只有**一个**实例真的触发，否则会重复跑 Agent、重复花钱、重复发通知。

   实现是纯文件系统原子操作：在 `~/.lite-work/watch_leases/` 下独占创建
   `"<schedule_id>@<slot>.lock"`（`O_CREAT|O_EXCL`）。创建成功的那个进程
   拥有本次触发；其余进程拿到 FileExistsError 直接跳过。
   锁文件带 owner pid 与心跳时间；**过期锁（默认 10 分钟）可被接管**，
   以免持有者崩溃后该计划永久失联。
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("litework.watch.store")

# 触发租约的默认存活期：超过则认为持有者已崩溃，允许他人接管。
# 取 10 分钟 > 审批门 600s，避免「审批还没结束就被别人抢跑」。
DEFAULT_LEASE_TTL = 600.0


def wall_now() -> float:
    """当前墙上时间（秒）。统一出口，便于测试替换。"""
    return time.time()


def _to_int(value: Any, default: int) -> int:
    """宽容取整：值非法时退回默认。

    计划文件是**用户可手改的**（也是唯一的事实源），一个写错的
    `interval_seconds` 不该让整份文件加载失败、所有计划一起失联。
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n


def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if f > 0 else default


# ---------------------------------------------------------------- 数据模型


@dataclass
class RunRecord:
    """一次触发的结果摘要（运行历史，用于面板展示与排错）。"""

    at: float = field(default_factory=wall_now)
    status: str = "ok"          # ok / changed / unchanged / error / skipped
    detail: str = ""
    task_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"at": self.at, "status": self.status, "detail": self.detail}
        if self.task_id:
            d["task_id"] = self.task_id
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RunRecord":
        return cls(
            at=_to_float(d.get("at")) or wall_now(),
            status=str(d.get("status") or "ok"),
            detail=str(d.get("detail") or ""),
            task_id=(str(d["task_id"]) if d.get("task_id") else None),
        )


@dataclass
class Schedule:
    """一条计划。`source` 为空 = 纯定时任务（到点直接跑 prompt）；
    非空 = 盯梢（先跑探针，指纹变了才唤 Agent）。

    字段以「可安全序列化」为准：一律基础类型 + 列表/字典，`to_dict()` 与
    `from_dict()` 严格互逆（测试覆盖）。
    """

    id: str = field(default_factory=lambda: f"sch_{uuid.uuid4().hex[:10]}")
    name: str = ""
    enabled: bool = True

    # ---- 调度 ----
    # mode: interval / daily / cron
    schedule_mode: str = "interval"
    # interval：秒；daily/cron：与 time_of_day 配合
    interval_seconds: int = 3600
    # daily 用；格式 "HH:MM"
    time_of_day: str = "09:00"
    # 5 段 cron（分 时 日 月 周），仅 mode=cron 时使用
    cron: str = ""

    # ---- 内容 ----
    session_id: Optional[str] = None
    workspace: str = ""
    prompt: str = ""
    source: str = ""                    # 空 = 纯定时；否则为 WatchSource.source_kind
    probe_config: Dict[str, Any] = field(default_factory=dict)

    # ---- 行为 ----
    permissions: str = "read"           # 无人值守权限档：read / read+web
    notify: List[str] = field(default_factory=lambda: ["in_app"])
    catch_up: str = "once"              # once / all / skip

    # ---- 运行时 ----
    last_run: Optional[float] = None
    last_hash: str = ""
    last_status: str = ""
    runs: List[RunRecord] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": bool(self.enabled),
            "schedule_mode": self.schedule_mode,
            "interval_seconds": int(self.interval_seconds),
            "time_of_day": self.time_of_day,
            "cron": self.cron,
            "session_id": self.session_id,
            "workspace": self.workspace,
            "prompt": self.prompt,
            "source": self.source,
            "probe_config": dict(self.probe_config),
            "permissions": self.permissions,
            "notify": list(self.notify),
            "catch_up": self.catch_up,
            "last_run": self.last_run,
            "last_hash": self.last_hash,
            "last_status": self.last_status,
            "runs": [r.to_dict() for r in self.runs],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Schedule":
        runs_raw = d.get("runs")
        runs = [RunRecord.from_dict(r) for r in runs_raw if isinstance(r, dict)] if isinstance(runs_raw, list) else []
        return cls(
            id=str(d.get("id") or f"sch_{uuid.uuid4().hex[:10]}"),
            name=str(d.get("name") or ""),
            enabled=bool(d.get("enabled", True)),
            schedule_mode=str(d.get("schedule_mode") or "interval"),
            interval_seconds=_to_int(d.get("interval_seconds"), 3600),
            time_of_day=str(d.get("time_of_day") or "09:00"),
            cron=str(d.get("cron") or ""),
            session_id=(str(d["session_id"]) if d.get("session_id") else None),
            workspace=str(d.get("workspace") or ""),
            prompt=str(d.get("prompt") or ""),
            source=str(d.get("source") or ""),
            probe_config=(dict(d["probe_config"]) if isinstance(d.get("probe_config"), dict) else {}),
            permissions=str(d.get("permissions") or "read"),
            notify=(list(d["notify"]) if isinstance(d.get("notify"), list) else ["in_app"]),
            catch_up=str(d.get("catch_up") or "once"),
            last_run=_to_float(d.get("last_run")),
            last_hash=str(d.get("last_hash") or ""),
            last_status=str(d.get("last_status") or ""),
            runs=runs[-20:],  # 只留最近 20 次，防文件无限增长
        )


# ---------------------------------------------------------------- 存储


class ScheduleStore:
    """计划文件读写。构造时不触碰磁盘；load() 才读（缺失即空）。"""

    MAX_RUNS = 20

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path
        self._items: Dict[str, Schedule] = {}
        self._loaded = False

    # ---- 载入 ----

    def load(self) -> List[Schedule]:
        self._items = {}
        if not self.path:
            self._loaded = True
            return []
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError) as exc:
            # 坏文件不能让调度器整个哑掉：记日志后按空处理（不覆盖原文件，
            # 便于用户手工抢救）
            logger.warning("读取计划文件失败（按空处理）: %s", exc)
        else:
            raw = data.get("schedules") if isinstance(data, dict) else data
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict):
                        s = Schedule.from_dict(item)
                        self._items[s.id] = s
        self._loaded = True
        return list(self._items.values())

    def _ensure(self) -> None:
        if not self._loaded:
            self.load()

    def all(self) -> List[Schedule]:
        self._ensure()
        return list(self._items.values())

    def get(self, schedule_id: str) -> Optional[Schedule]:
        self._ensure()
        return self._items.get(schedule_id)

    # ---- 写入 ----

    def upsert(self, sched: Schedule) -> Schedule:
        self._ensure()
        self._items[sched.id] = sched
        self.save()
        return sched

    def delete(self, schedule_id: str) -> bool:
        self._ensure()
        if schedule_id not in self._items:
            return False
        del self._items[schedule_id]
        self.save()
        return True

    def record_run(self, schedule_id: str, rec: RunRecord) -> None:
        """记账一次触发（并写盘）。未找到计划时静默返回。"""
        self._ensure()
        sched = self._items.get(schedule_id)
        if sched is None:
            return
        sched.runs.append(rec)
        if len(sched.runs) > self.MAX_RUNS:
            sched.runs = sched.runs[-self.MAX_RUNS:]
        sched.last_run = rec.at
        sched.last_status = rec.status
        self.save()

    def save(self) -> None:
        """原子写：先写同目录临时文件再 os.replace（避免半截 JSON）。"""
        if not self.path:
            return
        payload = {"version": 1, "schedules": [s.to_dict() for s in self._items.values()]}
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = f"{self.path}.{uuid.uuid4().hex[:8]}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:
            # 与 BackgroundRegistry 同约定：持久化失败绝不冒泡影响调度
            logger.warning("写入计划文件失败: %s", exc)

    def journal_payload(self) -> Dict[str, Any]:
        """只读快照（供 /api 展示，与内部结构解耦）。"""
        return {"schedules": [s.to_dict() for s in self.all()]}


# ---------------------------------------------------------------- 触发租约


class TriggerLease:
    """跨进程「本次触发归谁」的判定。

    用法：
        lease = TriggerLease(dir)
        if lease.acquire(schedule.id, slot):   # slot 标识「哪一次到期窗口」
            ...真正触发...
            lease.release(schedule.id, slot)

    slot 必须能区分**同一次到期窗口**：不同进程对同一窗口算出的 slot 必须一致
    （所以由调用方用稳定的时间戳表达，如 floor(due_ts)），否则各抢各的锁，
    照样重复触发。
    """

    def __init__(self, dir_path: Optional[str] = None, ttl: float = DEFAULT_LEASE_TTL) -> None:
        self.dir = dir_path
        self.ttl = ttl

    def _path(self, schedule_id: str, slot: str) -> Optional[str]:
        if not self.dir:
            return None
        safe = "".join(c for c in f"{schedule_id}@{slot}" if c.isalnum() or c in "@._-")
        return os.path.join(self.dir, f"{safe}.lock")

    def acquire(self, schedule_id: str, slot: str) -> bool:
        """抢锁。返回 True = 本次触发归我；False = 别人已抢（或目录不可用）。

        目录未配置（单进程场景）时恒返回 True——此时不存在竞争。
        """
        dir_path = self.dir
        if not dir_path:
            return True
        path = self._path(schedule_id, slot)
        if path is None:
            return True
        payload = json.dumps({"pid": os.getpid(), "at": wall_now()})
        try:
            os.makedirs(dir_path, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            return True
        except FileExistsError:
            # 已有锁：过期则可接管（持有者崩溃的场景）
            if self._expired(path):
                try:
                    os.remove(path)
                    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(payload)
                    logger.warning("接管过期触发租约: %s", path)
                    return True
                except (FileExistsError, OSError):
                    return False
            return False
        except OSError as exc:
            # 拿不到锁目录（权限/磁盘）时**宁可跳过**：重复触发比漏一次更糟
            logger.warning("获取触发租约失败（跳过本次）: %s", exc)
            return False

    def _expired(self, path: str) -> bool:
        try:
            with open(path, encoding="utf-8") as f:
                info = json.load(f)
            at = float(info.get("at") or 0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            at = 0.0
        if at <= 0:
            try:
                at = os.path.getmtime(path)
            except OSError:
                return True
        return (wall_now() - at) > self.ttl

    def release(self, schedule_id: str, slot: str) -> None:
        path = self._path(schedule_id, slot)
        if not path:
            return
        try:
            os.remove(path)
        except OSError:
            pass
