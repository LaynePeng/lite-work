# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""定价数据源插件基类 + 通用分时窗口求值。

**为什么是插件**：价格表、抓取地址、页面结构、分时规则都会变（官方改价、
文档站改版、新增供应商）。这些都不该让用户等主程序发版 —— 所以定价能力
做成社区插件（与 collab-* 同一套安装/更新机制）：

- 插件：价格表 + 数据源 + 抓取解析 + 分时窗口（见
  `litework/builtin_plugins/pricing-plugin/`，可被 ~/.lite-work/plugins/ 同名
  包覆盖更新）；
- 主程序只保留：插件发现/装载、**通用**窗口求值、状态/同步 API 转发、
  以及最后一道 config 回退价。

插件只需实现 `PricingProvider` 的下列方法：

    lookup(model_id) -> Optional[dict]   # 按模型名给价（含可选分时档）
    sources() -> list[dict]             # 数据源清单（id/label/url）
    status() -> list[dict]              # 各源缓存状态（cached/age/stale）
    sync(source_id) -> dict             # 手动同步单个源（不自动联网）

lookup 返回结构（键名与价格字段沿用主程序口径，每 M token）：

    {
      "input_per_mtok": float, "output_per_mtok": float, "cache_hit_per_mtok": float,
      "off_peak": {同上三价} | 缺失,          # 分时供应商的空闲档
      "peak_window": {...},                  # 见 is_off_peak_now
      "model_key": str, "source": "official:deepseek"|"snapshot:deepseek"|...,
      "source_label": str, "source_age_seconds": float|None, "stale": bool,
    }

**数据 vs 策略**：插件只负责报「数据」——价格、来源、**缓存年龄**
（`source_age_seconds` / `status()` 的 `age_seconds`）。「多久算旧、要不要提示
用户同步」是**用户偏好**，判定在主程序侧（`config.pricing_check_ttl_days`，
0=永不过期），并且与 models.dev 共用同一项设置——所以插件可以照常返回 `stale`
（作为它自身的默认口径，供独立使用时参考），主程序会用年龄 + 用户配置复算，
不会直接透出插件那份。插件不必、也不应该去读主程序的 config。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ..core.types import Plugin

logger = logging.getLogger("litework.pricing_provider")


def is_off_peak_now(entry: Optional[Dict[str, Any]],
                    now_utc: Optional[datetime] = None) -> bool:
    """按价格条目里的窗口定义判断「当前是否空闲时段」。

    window（entry["peak_window"]）为**高峰时段**定义，其余时间即空闲：
        {
          "tz_offset_minutes": 0,        # 窗口所用时区相对 UTC 的偏移（分钟）
          "days": [0, 1, 2, 3, 4],       # 生效星期（0=周一，Python weekday 口径）
          "periods": [[60, 240], [360, 600]],   # 分钟区间 [start, end)，左闭右开
        }
    DeepSeek 官方以 UTC 表述（周一至周五 01:00-04:00、06:00-10:00）→ 偏移 0、
    days=[0..4]、periods=[[60,240],[360,600]]。窗口规则是**数据**，改规则只需
    改插件里的 JSON，不必动主程序。

    没有 off_peak 档或没有窗口定义 → 一律返回 False（按基准价计费）。
    """
    if not isinstance(entry, dict):
        return False
    off_peak = entry.get("off_peak")
    if not isinstance(off_peak, dict) or not off_peak:
        return False
    window = entry.get("peak_window")
    if not isinstance(window, dict):
        return False
    try:
        now = now_utc or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        local = now.astimezone(timezone.utc) + timedelta(
            minutes=int(window.get("tz_offset_minutes") or 0))
        days = window.get("days")
        if isinstance(days, list) and days and local.weekday() not in days:
            return True  # 非高峰日 → 空闲
        minutes = local.hour * 60 + local.minute
        for period in window.get("periods") or []:
            if not isinstance(period, (list, tuple)) or len(period) < 2:
                continue
            if int(period[0]) <= minutes < int(period[1]):
                return False  # 处于高峰区间
        return True
    except Exception:
        logger.debug("[Pricing] 分时窗口求值失败，按基准价计费", exc_info=True)
        return False


class PricingProvider(Plugin):
    """定价数据源插件基类（社区包 pricing-* 实现此类）。

    与工具/协作模式插件同一套安装与发现机制（~/.lite-work/plugins/，
    内置副本在 litework/builtin_plugins/）；装好即被主程序采用，
    本地同名包覆盖内置（社区更新）。
    """

    version: str = ""
    description: str = ""
    # 由 AgentApp 在发现「用户目录覆盖同名内置」时置位（与 collab 插件同口径）
    _is_local_override: bool = False

    def configure(self, config_dir: str) -> None:
        """注入配置目录（同步缓存的落盘位置）；主程序装载后调用一次。"""

    def lookup(self, model_id: str) -> Optional[Dict[str, Any]]:
        """按模型名给价（自行判断是否认识该模型）；不认识返回 None。"""
        raise NotImplementedError

    def sources(self) -> List[Dict[str, Any]]:
        """数据源清单，用于 UI 展示同步步骤。"""
        return []

    def status(self) -> List[Dict[str, Any]]:
        """各源状态：{id, label, url, cached, models, age_seconds, stale}。"""
        return []

    def sync(self, source_id: str) -> Dict[str, Any]:
        """手动同步单个源，返回 {ok, error?, models?, ...}（不抛异常）。"""
        raise NotImplementedError
