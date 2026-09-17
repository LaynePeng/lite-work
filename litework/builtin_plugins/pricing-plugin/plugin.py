# lite-work 定价数据源插件（官方价格 + 分时计费）
#
# 安装后由主程序自动采用；设置 → 综合设置 → 「模型元数据与定价」里可查看各源
# 状态并手动同步（不自动联网）。所有价目表与数据源都在本插件的 pricing.json，
# 改价/改源只改那个文件即可，不必升级主程序；~/.lite-work/plugins/ 同名包覆盖
# 内置副本（社区更新机制）。
#
# 数据源（均为静态文本，无需 JS 渲染）：
# - DeepSeek: 官方定价页英文版（USD，peak / off-peak 两档都列）
# - Kimi:     国际站文档 .md 端点（USD）
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

from litework.core.types import ToolDefinition
from litework.llm.pricing_provider import PricingProvider
from litework.tools.plugin import ToolPlugin

logger = logging.getLogger("litework.plugin.pricing")

_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}


# ------------------------------------------------------------ 页面解析


class _RowCollector(HTMLParser):
    """收集 HTML 表格行 → [[cell_text, ...], ...]。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[List[str]] = []
        self._row: Optional[List[str]] = None
        self._cell: Optional[List[str]] = None

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


_METRIC_INPUT = re.compile(r"INPUT TOKENS.*CACHE MISS", re.I | re.S)
_METRIC_HIT = re.compile(r"INPUT TOKENS.*CACHE HIT", re.I | re.S)
_METRIC_OUTPUT = re.compile(r"OUTPUT TOKENS", re.I)


def parse_deepseek_pricing(html: str) -> Dict[str, Dict[str, Any]]:
    """DeepSeek 定价页 → {模型: {peak: {...}, off_peak: {...}}}。

    表结构（Docusaurus 静态 HTML）：
        MODEL | deepseek-flash(1) | deepseek-v4-pro(2)
        1M INPUT TOKENS (CACHE HIT)  / OFF-PEAK | $0.003 | $0.022
                                     / PEAK     | $0.006 | $0.044
        1M INPUT TOKENS (CACHE MISS) / 1M OUTPUT TOKENS …
    解析失败抛 ValueError（同步接口据此报错，不污染已有缓存）。
    """
    collector = _RowCollector()
    collector.feed(html or "")

    models: List[str] = []
    out: Dict[str, Dict[str, Any]] = {}

    def _metric(text: str) -> Optional[str]:
        if _METRIC_HIT.search(text):
            return "cache_hit_per_mtok"
        if _METRIC_INPUT.search(text):
            return "input_per_mtok"
        if _METRIC_OUTPUT.search(text):
            return "output_per_mtok"
        return None

    metric: Optional[str] = None
    for row in collector.rows:
        cells = [c.strip() for c in row]
        if not models and any(c.upper() == "MODEL" for c in cells):
            models = [re.sub(r"\(\d+\)$", "", c).strip()
                      for c in cells if c.upper() != "MODEL" and c.strip()]
            if models:
                out = {m: {} for m in models}
            continue
        if not models:
            continue
        for c in cells:
            found = _metric(c)
            if found:
                metric = found
                break
        variant = next((c for c in cells if c.upper() in ("PEAK", "OFF-PEAK")), None)
        if variant is None or metric is None:
            continue
        raw = [_parse_money(c) for c in cells if c.startswith("$")]
        prices = [p for p in raw if p is not None]
        slot = "peak" if variant.upper() == "PEAK" else "off_peak"
        for model, price in zip(models, prices):
            out[model].setdefault(slot, {})[metric] = price

    if not out or any(len(v.get("peak") or {}) < 3 for v in out.values()):
        raise ValueError("DeepSeek 定价页解析失败（表格结构可能已变更）")
    # 空闲档缺失时按官方「空闲 = 峰值一半」补齐（正常情况下页面会直接给出）
    for entry in out.values():
        if not entry.get("off_peak"):
            entry["off_peak"] = {k: round(v / 2, 6) for k, v in entry["peak"].items()}
    return out


def _parse_money(text: str) -> Optional[float]:
    m = re.search(r"\$\s*([0-9]+(?:\.[0-9]+)?)", text or "")
    return float(m.group(1)) if m else None


# 兼容 Mintlify .md 端点（`["kimi-k3", "1M tokens", <>{"$"}0.30</>, …]`）与
# 页面 HTML 的模板字符串形式（行内含 children:[…] 方括号，故作行尾前瞻）
_KIMI_ROW = re.compile(
    r"\[\s*[`\"']?(kimi-[A-Za-z0-9.\-]+)[`\"']?\s*,\s*[`\"']?1M tokens[`\"']?\s*,(.*?)\]"
    r"(?=,\s|\s*\]\})",
    re.S,
)


def parse_kimi_pricing(markdown: str) -> Dict[str, Dict[str, Any]]:
    """Kimi 定价页 → {模型: {peak: {...}}}（无分时，三价：命中/未命中/输出）。"""
    out: Dict[str, Dict[str, Any]] = {}
    for m in _KIMI_ROW.finditer(markdown or ""):
        name = m.group(1)
        nums = [float(x) for x in re.findall(r"[0-9]+(?:\.[0-9]+)?", m.group(2))]
        if len(nums) < 3:
            continue
        out[name] = {"peak": {
            "cache_hit_per_mtok": nums[0],
            "input_per_mtok": nums[1],
            "output_per_mtok": nums[2],
        }}
    if not out:
        raise ValueError("Kimi 定价页解析失败（页面结构可能已变更）")
    return out


_PARSERS = {"deepseek": parse_deepseek_pricing, "kimi": parse_kimi_pricing}
_PRICE_KEYS = ("input_per_mtok", "output_per_mtok", "cache_hit_per_mtok")


# ------------------------------------------------------------ 插件


class OfficialPricingPlugin(ToolPlugin, PricingProvider):
    """定价数据源插件：既是工具插件（进插件页 / kernel 装配），又实现 PricingProvider。

    与其它内置插件同一套安装 / 发现 / 覆盖回退机制；get_tools() 为空——不向
    Agent 暴露工具，仅供主程序按模型名计费。
    """

    name = "pricing-plugin"
    version = "1.0.0"
    description = ("官方定价数据源：DeepSeek 分时价（高峰/空闲半价）与 Kimi 国际站价，"
                   "按模型指纹计费（自定义中转也能认出）。价格表在 pricing.json，"
                   "手动同步、不自动联网。")

    def get_tools(self) -> List[ToolDefinition]:
        return []

    def __init__(self) -> None:
        self.config_dir: Optional[str] = None
        self._data: Dict[str, Any] = {}
        self._cache: Optional[Dict[str, Any]] = None
        self._ttl = 7 * 24 * 3600
        self._load_bundled()

    # -------------------------------------------------- 数据装载

    def _data_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "pricing.json")

    def _load_bundled(self) -> None:
        try:
            with open(self._data_path(), "r", encoding="utf-8") as f:
                self._data = json.load(f) or {}
            self._ttl = int(self._data.get("ttl_seconds") or 7 * 24 * 3600)
        except Exception:
            logger.exception("[Pricing] pricing.json 读取失败，定价插件不可用")
            self._data = {}

    def configure(self, config_dir: str) -> None:
        self.config_dir = config_dir
        self._cache = None

    def _cache_path(self) -> Optional[str]:
        if not self.config_dir:
            return None
        return os.path.join(self.config_dir, "pricing_cache.json")

    def _load_cache(self) -> Dict[str, Any]:
        if self._cache is not None:
            return self._cache
        data: Dict[str, Any] = {"sources": {}}
        path = self._cache_path()
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict) and isinstance(raw.get("sources"), dict):
                    data = raw
            except Exception:
                logger.warning("[Pricing] 同步缓存读取失败，改用内置快照")
        self._cache = data
        return data

    def _source_def(self, source_id: str) -> Dict[str, Any]:
        return ((self._data.get("sources") or {}).get(source_id) or {})

    def _source_ids(self) -> List[str]:
        return list((self._data.get("sources") or {}).keys())

    def _models_for(self, source_id: str) -> Tuple[Dict[str, Any], Optional[float], bool]:
        """(模型表, 缓存年龄秒, 是否来自内置快照)。缓存优先，缺失回落快照。"""
        entry = (self._load_cache().get("sources") or {}).get(source_id) or {}
        models = entry.get("models")
        fetched_at = entry.get("fetched_at")
        if isinstance(models, dict) and models and isinstance(fetched_at, (int, float)):
            return models, max(0.0, time.time() - float(fetched_at)), False
        return (self._source_def(source_id).get("models") or {}), None, True

    # -------------------------------------------------- 指纹

    def _fingerprint(self, model_id: str) -> Optional[Tuple[str, str]]:
        base = (model_id or "").rsplit("/", 1)[-1].strip().lower()
        if not base:
            return None
        aliases = self._data.get("aliases") or {}
        canonical = aliases.get(base)
        if canonical:
            for _, key, source in self._iter_fingerprints():
                if key == canonical:
                    return key, source
        for pattern, key, source in self._iter_fingerprints():
            if pattern.search(base):
                return key, source
        return None

    def _iter_fingerprints(self):
        for item in self._data.get("fingerprints") or []:
            if not isinstance(item, (list, tuple)) or len(item) < 3:
                continue
            try:
                yield re.compile(str(item[0]), re.I), str(item[1]), str(item[2])
            except re.error:
                logger.warning("[Pricing] 无效的模型指纹正则: %r", item[0])

    # -------------------------------------------------- 查询

    def lookup(self, model_id: str) -> Optional[Dict[str, Any]]:
        hit = self._fingerprint(model_id)
        if hit is None:
            return None
        key, source_id = hit
        models, age, is_snapshot = self._models_for(source_id)
        entry = models.get(key)
        if not isinstance(entry, dict):
            return None
        peak = entry.get("peak") or {}
        if not peak:
            return None
        sdef = self._source_def(source_id)
        out: Dict[str, Any] = {k: float(peak.get(k, 0) or 0) for k in _PRICE_KEYS}
        out.update({
            "model_key": key,
            "source": ("snapshot:" if is_snapshot else "official:") + source_id,
            "source_label": sdef.get("label") or source_id,
            "source_age_seconds": age,
            "stale": bool(age is not None and age > self._ttl),
            "snapshot": is_snapshot,
        })
        off_peak = entry.get("off_peak")
        if isinstance(off_peak, dict) and off_peak:
            out["off_peak"] = {k: float(off_peak.get(k, 0) or 0) for k in _PRICE_KEYS}
        elif sdef.get("peak_window"):
            # 官方口径：空闲为峰值一半（页面缺档时的兜底）
            out["off_peak"] = {k: round(out[k] / 2, 6) for k in _PRICE_KEYS}
        if isinstance(sdef.get("peak_window"), dict):
            out["peak_window"] = sdef["peak_window"]
        return out

    # -------------------------------------------------- 状态 / 同步

    def sources(self) -> List[Dict[str, Any]]:
        return [{"id": sid, "label": self._source_def(sid).get("label") or sid,
                 "url": self._source_def(sid).get("url") or ""}
                for sid in self._source_ids()]

    def status(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for sid in self._source_ids():
            entry = (self._load_cache().get("sources") or {}).get(sid) or {}
            fetched_at = entry.get("fetched_at")
            age: Optional[float] = None
            if isinstance(fetched_at, (int, float)):
                age = max(0.0, time.time() - float(fetched_at))
            sdef = self._source_def(sid)
            out.append({
                "id": sid,
                "label": sdef.get("label") or sid,
                "url": sdef.get("url") or "",
                "cached": bool(entry.get("models")),
                "models": len(entry.get("models") or {}),
                "fetched_at": fetched_at,
                "age_seconds": age,
                "stale": (age is None) or (age > self._ttl),
                "snapshot_date": sdef.get("snapshot_date"),
            })
        return out

    def sync(self, source_id: str) -> Dict[str, Any]:
        sdef = self._source_def(source_id)
        parser = _PARSERS.get(str(sdef.get("parser") or ""))
        if not sdef or parser is None:
            return {"ok": False, "error": f"未知定价源: {source_id}"}
        started = time.time()
        try:
            import httpx

            resp = httpx.get(sdef["url"], timeout=20, follow_redirects=True,
                             headers=_FETCH_HEADERS)
            if resp.status_code != 200:
                return {"ok": False, "error": f"HTTP {resp.status_code}"}
            models = parser(resp.text)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            logger.warning("[Pricing] %s 同步失败: %s", source_id, exc)
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        data = self._load_cache()
        data.setdefault("sources", {})[source_id] = {
            "fetched_at": time.time(), "url": sdef["url"], "models": models,
        }
        try:
            self._write(data)
        except Exception as exc:
            logger.warning("[Pricing] 同步缓存写入失败: %s", exc)
            return {"ok": False, "error": f"缓存写入失败: {exc}"}
        result = {"ok": True, "models": len(models),
                  "elapsed_ms": int((time.time() - started) * 1000)}
        for st in self.status():
            if st["id"] == source_id:
                result.update(st)
        return result

    def _write(self, data: Dict[str, Any]) -> None:
        path = self._cache_path()
        if not path:
            return
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
