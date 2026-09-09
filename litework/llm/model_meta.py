# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""模型元数据服务（models.dev 同步 + 内置静态表兜底）。

各厂商的 /models 接口不返回上下文长度（OpenAI 官方 issue #587 未实现），
业界标准做法（OpenCode 同款）是使用社区模型元数据库 models.dev：
  https://models.dev/api.json
数据结构为 provider → models → model_id → {limit: {context, output}, ...}，
本模块会将其拍平成 model_id → entry 的索引。

本模块：
1. 启动时尝试拉取 models.dev 数据并缓存到配置目录；
2. 查询时按模型 ID 精确匹配缓存；命中失败再回退内置静态表；
3. 网络失败静默降级，离线可用。
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Dict, Optional

logger = logging.getLogger("litework.modelmeta")

MODELS_DEV_URL = "https://models.dev/api.json"
CACHE_TTL_SECONDS = 7 * 24 * 3600  # 缓存 7 天


def _flatten(data: Dict[str, dict]) -> Dict[str, dict]:
    """把 models.dev 的 provider→models→model_id 结构拍平成 model_id → entry。"""
    flat: Dict[str, dict] = {}
    for provider, meta in (data or {}).items():
        if not isinstance(meta, dict):
            continue
        models = meta.get("models")
        if not isinstance(models, dict):
            continue
        for model_id, entry in models.items():
            if isinstance(entry, dict):
                flat[model_id] = entry
    return flat


class ModelMetaService:
    def __init__(self, cache_path: Optional[str] = None) -> None:
        self.cache_path = cache_path
        self._index: Optional[Dict[str, dict]] = None

    # ------------------------------------------------------------ 加载/刷新

    def refresh(self) -> bool:
        """刷新 models.dev 元数据（缓存未过期时纯本地读盘，不发网络请求）。

        TTL 挡板：磁盘缓存存在且 mtime 距今不足 CACHE_TTL_SECONDS → 直接加载
        缓存并返回，网络零开销；过期或缺失才真正拉取（失败静默降级内置表）。
        """
        if self.cache_path and os.path.exists(self.cache_path):
            try:
                fresh = time.time() - os.path.getmtime(self.cache_path) <= CACHE_TTL_SECONDS
            except OSError:
                fresh = False
            if fresh:
                cached = self._load_cache()
                if cached:
                    self._index = cached
                    return True
        return self._fetch_and_store()

    def _fetch_and_store(self) -> bool:
        """真正拉取 models.dev 全量数据并落盘缓存。失败返回 False。"""
        try:
            import httpx

            resp = httpx.get(MODELS_DEV_URL, timeout=10)
            if resp.status_code != 200:
                logger.warning("[ModelMeta] models.dev 返回 HTTP %s", resp.status_code)
                return False
            data = resp.json()
            if not isinstance(data, dict):
                return False
            index = _flatten(data)
            if not index:
                return False
            self._index = index
            if self.cache_path:
                os.makedirs(os.path.dirname(self.cache_path) or ".", exist_ok=True)
                with open(self.cache_path, "w", encoding="utf-8") as f:
                    json.dump(index, f, ensure_ascii=False)
            logger.info("[ModelMeta] models.dev 同步成功 (%s 个模型)", len(index))
            return True
        except Exception:
            logger.warning("[ModelMeta] models.dev 同步失败，使用内置静态表")
            return False

    def _load_cache(self) -> Optional[Dict[str, dict]]:
        if self._index is not None:
            return self._index
        if not self.cache_path or not os.path.exists(self.cache_path):
            return None
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if time.time() - os.path.getmtime(self.cache_path) > CACHE_TTL_SECONDS:
                return None
            if not isinstance(data, dict):
                return None
            self._index = _flatten(data) if "models" in data else data
            return self._index
        except Exception:
            return None

    # ------------------------------------------------------------ 查询

    def get_context_window(self, model_id: str) -> Optional[int]:
        """按模型 ID 查上下文长度（缓存优先，None 表示未知）。"""
        index = self._load_cache()
        if index:
            entry = index.get(model_id)
            if isinstance(entry, dict):
                limit = entry.get("limit") or {}
                context = limit.get("context") or limit.get("input")
                if isinstance(context, int) and context > 0:
                    return context
        return None

    def get_pricing(self, model_id: str, provider_id: str = "") -> Optional[Dict[str, float]]:
        """按模型查定价（每百万 token 美元）：{input, output, cache_read}。

        models.dev 索引键为 "provider/model"（如 "deepseek/deepseek-v4-flash"），
        而本地配置的模型名通常是裸名——按 全名 → provider/model → 裸名 三级匹配。
        无数据（自定义实例/未知模型）返回 None，调用方回退静态配置价。
        """
        index = self._load_cache()
        if not index or not model_id:
            return None
        candidates = [model_id]
        if provider_id and "/" not in model_id:
            candidates.append(f"{provider_id}/{model_id}")
        for key in candidates:
            entry = index.get(key)
            if not isinstance(entry, dict):
                continue
            cost = entry.get("cost")
            if not isinstance(cost, dict):
                continue
            pricing = {}
            for src, dst in (("input", "input_per_mtok"),
                             ("output", "output_per_mtok"),
                             ("cache_read", "cache_hit_per_mtok")):
                v = cost.get(src)
                if isinstance(v, (int, float)) and v >= 0:
                    pricing[dst] = float(v)
            if "input_per_mtok" in pricing and "output_per_mtok" in pricing:
                return pricing
        return None