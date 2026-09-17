# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""模型元数据服务（models.dev 同步 + 内置静态表兜底）。

各厂商的 /models 接口不返回上下文长度（OpenAI 官方 issue #587 未实现），
业界标准做法（OpenCode 同款）是使用社区模型元数据库 models.dev：
  https://models.dev/api.json
数据结构为 provider → models → model_id → {limit: {context, output}, ...}，
本模块会将其拍平成 "provider/model_id" → entry 的索引（同一模型在不同
供应商下价格差异极大，必须按供应商区分）。

本模块：
1. 启动时尝试拉取 models.dev 数据并缓存到配置目录；
2. 查询时按 provider + 模型 ID 精确匹配缓存；命中失败再回退内置静态表；
3. 网络失败静默降级，离线可用。
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("litework.modelmeta")

MODELS_DEV_URL = "https://models.dev/api.json"
CACHE_TTL_SECONDS = 7 * 24 * 3600  # 缓存 7 天


def _flatten(data: Dict[str, Any]) -> Dict[str, dict]:
    """把 models.dev 的 provider→models→model_id 结构拍平成 "provider/model_id" → entry。

    必须以「供应商 + 模型」为键：同一个模型 ID 在 models.dev 里往往有几十家
    供应商在售且价格差异极大（如 deepseek-v4-flash 有 30+ 家，单价从 0 到 0.3
    不等）。早期实现只存裸 model_id，后遍历到的供应商会覆盖前面的，结果定价
    随机取到某个转售商的价（缓存命中价甚至差 20 倍）。

    同时保留裸 model_id 兜底键（首个命中者，顺序稳定）：上下文窗口是「模型级」
    属性（各家一致），未知供应商（自定义中转）也应当能查到；定价则仅在
    provider 精确匹配缺失时退化使用，属粗略参考。

    裸名键只在模型名不含 "/" 时写入：不少供应商用「厂商/模型」形式的 id
    （如 tokengo 的 "deepseek/deepseek-v4-flash"），该 id 本身就是合法裸键，
    会与官方 "deepseek" 供应商的限定键同名——先到先得（setdefault）会把
    官方价/窗口顶掉。
    """
    flat: Dict[str, dict] = {}
    for provider, meta in (data or {}).items():
        if not isinstance(meta, dict):
            continue
        models = meta.get("models")
        if not isinstance(models, dict):
            continue
        for model_id, entry in models.items():
            if isinstance(entry, dict):
                flat.setdefault(f"{provider}/{model_id}", entry)
                if "/" not in model_id:
                    flat.setdefault(model_id, entry)
    return flat


def _candidates(model_id: str, provider_id: str = "") -> list:
    """查找候选键：provider 限定名优先，裸模型名兜底（兼容手写/历史缓存文件）。"""
    if provider_id and "/" not in model_id:
        return [f"{provider_id}/{model_id}", model_id]
    return [model_id]


# 官方厂商 id（models.dev 索引里的第一方供应商）
OFFICIAL_VENDOR_IDS = frozenset({
    "deepseek", "z-ai", "zhipuai", "zhipu", "moonshotai", "moonshot",
    "qwen", "alibaba", "alibaba-cn", "minimax", "bytedance", "volcengine",
    "tencent", "hunyuan", "openai", "anthropic", "google", "xai", "meta",
    "mistral", "cohere", "ai21",
})


def _entry_pricing(entry: Optional[dict]) -> Optional[Dict[str, float]]:
    """从一条 models.dev 记录里提取归一化定价（每 M token）。"""
    if not isinstance(entry, dict):
        return None
    cost = entry.get("cost")
    if not isinstance(cost, dict):
        return None
    pricing: Dict[str, float] = {}
    for src, dst in (("input", "input_per_mtok"),
                     ("output", "output_per_mtok"),
                     ("cache_read", "cache_hit_per_mtok")):
        v = cost.get(src)
        if isinstance(v, (int, float)) and v >= 0:
            pricing[dst] = float(v)
    if "input_per_mtok" in pricing and "output_per_mtok" in pricing:
        return pricing
    return None


def _to_index(data: Dict[str, dict]) -> Dict[str, dict]:
    """缓存文件既可能是「已拍平索引」，也可能是 models.dev 原始结构（provider → models）。

    原始结构先拍平；已拍平索引原样返回（历史/手写缓存可能直接用裸模型名作键，保持兼容）。
    """
    nested = any(
        isinstance(v, dict) and isinstance(v.get("models"), dict)
        for v in (data or {}).values()
    )
    return _flatten(data) if nested else data


class ModelMetaService:
    def __init__(self, cache_path: Optional[str] = None) -> None:
        self.cache_path = cache_path
        self._index: Optional[Dict[str, dict]] = None
        # 模型名 → 官方厂商条目的回退索引（懒构建，cache 重载时置空重建）
        self._official_idx: Optional[Dict[str, dict]] = None

    # ------------------------------------------------------------ 加载/刷新

    def refresh(self, force: bool = False) -> bool:
        """刷新 models.dev 元数据（缓存未过期时纯本地读盘，不发网络请求）。

        TTL 挡板：磁盘缓存存在且 mtime 距今不足 CACHE_TTL_SECONDS → 直接加载
        缓存并返回，网络零开销；过期或缺失才真正拉取（失败静默降级内置表）。

        force=True：跳过 TTL 挡板，强制联网拉取。**用户手动点「同步」必须走这条**
        —— 否则缓存未满 7 天时点了同步也只返回旧缓存，时间戳永远停在旧值
        （表象：「怎么点都显示 6 天前」）。
        """
        if not force and self.cache_path and os.path.exists(self.cache_path):
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
            self._index = _to_index(data)
            self._official_idx = None  # 索引更新 → 模型名回退索引失效重建
            return self._index
        except Exception:
            return None

    def _official_model_index(self) -> Dict[str, dict]:
        """模型名（小写）→ models.dev 官方厂商条目（网关/自定义实例按名回退查价用）。

        自定义中转/网关（custom_*、自建代理）在 models.dev 里没有 provider 条目，
        只能按模型名找第一方厂商的数据。同名模型在索引里有几十家转售（价格任意），
        这里只认**官方厂商段**（deepseek / z-ai / moonshotai / openai …）的键，
        并在多条命中时取「最接近官方」的（键段数最少，如 `z-ai/x` 优于
        `gateway/z-ai/x`）——避免取到转售价。
        """
        cached = getattr(self, "_official_idx", None)
        if cached is not None:
            return cached
        index = self._load_cache() or {}
        best: Dict[str, tuple] = {}  # model_lower -> (段数, entry)
        for key, entry in index.items():
            segs = key.split("/")
            if len(segs) < 2:
                continue
            if not any(s.lower() in OFFICIAL_VENDOR_IDS for s in segs[:-1]):
                continue
            model = segs[-1].strip().lower()
            if not model:
                continue
            n = len(segs)
            cur = best.get(model)
            if cur is None or n < cur[0]:
                best[model] = (n, entry)
        self._official_idx = {m: e for m, (_, e) in best.items()}
        return self._official_idx

    @staticmethod
    def _model_basename(model_id: str) -> str:
        """去掉可能自带的厂商前缀（'deepseek/deepseek-v4.1-flash' → 'deepseek-v4.1-flash'）。"""
        return (model_id or "").rsplit("/", 1)[-1].strip().lower()

    # ------------------------------------------------------------ 查询

    def get_context_window(self, model_id: str, provider_id: str = "") -> Optional[int]:
        """按模型 ID 查上下文长度（缓存优先，None 表示未知）。

        先按 "provider/model_id" 精确匹配，再退化到裸模型名（历史缓存兼容）；
        网关/自定义实例再按模型名匹配官方厂商条目（best-effort）。
        """
        index = self._load_cache()
        if index:
            for key in _candidates(model_id, provider_id):
                entry = index.get(key)
                if isinstance(entry, dict):
                    limit = entry.get("limit") or {}
                    context = limit.get("context") or limit.get("input")
                    if isinstance(context, int) and context > 0:
                        return context
            entry = self._official_model_index().get(self._model_basename(model_id))
            if isinstance(entry, dict):
                limit = entry.get("limit") or {}
                context = limit.get("context") or limit.get("input")
                if isinstance(context, int) and context > 0:
                    return context
        return None

    def get_pricing(self, model_id: str, provider_id: str = "") -> Optional[Dict[str, float]]:
        """按模型查定价（每百万 token 美元）：{input, output, cache_read}。

        models.dev 索引键为 "provider/model"（如 "deepseek/deepseek-v4-flash"），
        而本地配置的模型名通常是裸名——拼成 provider/model 精确匹配。

        网关/自定义实例（custom_*：聚合多家模型的代理）在 models.dev 无 provider
        条目 → 退化按**模型名匹配官方厂商条目**（best-effort，见
        `_official_model_index`），让「切换对话框模型」能反映到计费单价上；
        仍无数据（未知模型）返回 None，调用方回退静态配置价。
        """
        index = self._load_cache()
        if not index or not model_id:
            return None
        for key in _candidates(model_id, provider_id):
            # 裸模型名（不含 "/"）不作为定价依据：models.dev 里同名模型有几十家在售，
            # 取到哪家完全任意（单价 0 ~ 0.3 不等）→ 宁可让调用方用配置回退价。
            if "/" not in key:
                continue
            pricing = _entry_pricing(index.get(key))
            if pricing:
                return pricing
        # provider 精确匹配缺失：按模型名找官方厂商条目（网关/自定义实例的常见情形）
        return _entry_pricing(self._official_model_index().get(self._model_basename(model_id)))

    def status(self) -> Dict[str, Any]:
        """缓存状态（设置页展示同步情况用；只读盘，不发网络请求）。"""
        index = self._load_cache()
        age: Optional[float] = None
        if self.cache_path and os.path.exists(self.cache_path):
            try:
                age = max(0.0, time.time() - os.path.getmtime(self.cache_path))
            except OSError:
                age = None
        return {"cached": bool(index), "models": len(index or {}), "age_seconds": age}